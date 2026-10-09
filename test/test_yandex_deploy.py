"""Cloud publication uses OIDC, exact package bytes and preserved environment, never trades."""
import csv
from copy import deepcopy
import subprocess
from unittest.mock import create_autospec
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import yandex_deploy as deploy
from scripts.deploy_support import DeployError

SHA = 'a' * 40


def settings(tmp_path):
    """User-approved cloud identities are metadata, not long-lived credentials."""
    return deploy.Settings(
        'b1gdgrp5bnth11phova8', 'ajela8pd8l3bjeupa04s', 'autorepeater-deploy',
        'python-function', tmp_path / 'package.zip',
        deploy.FunctionSettings('python312', 'handler.handler', '256MB', '60s',
                                'ajelpbq7as5bpe499962'),
        deploy.SecretBinding('e6qc2ghhip6925lllhm7', 'e6qqshm7lkthgotpdqf6', 't_token', 't_token'))


def current():
    """yc json-rest returns protobuf JSON field names and string-valued int64 memory."""
    return {'id': 'oldversion', 'functionId': 'functionid', 'status': 'ACTIVE',
            'environment': {'ALGORITM': 'COMPOSITE', 'SRC_ACCOUNT': 'BALANCED',
                            'DST_ACCOUNT': '00123', 'SPECIAL': 'comma,equals=quote"'},
            'secrets': [], 'resources': {'memory': '268435456'},
            'runtime': 'python312', 'entrypoint': 'handler.handler',
            'executionTimeout': '60s', 'serviceAccountId': 'ajelpbq7as5bpe499962'}


def test_new_version_uses_bucket_and_keeps_environment_and_lockbox(tmp_path):
    """Package transport does not silently reset function configuration."""
    cfg = settings(tmp_path)
    args = deploy.version_arguments(cfg, current(), 'sha/package.zip', 'f' * 64, SHA)
    assert '--source-path' not in args
    assert args[args.index('--memory') + 1] == '256MB'
    assert args[args.index('--execution-timeout') + 1] == '60s'
    assert args[args.index('--service-account-id') + 1] == cfg.function.service_account_id
    assert args[args.index('--package-object-name') + 1] == 'sha/package.zip'
    assert args[args.index('--package-sha256') + 1] == 'f' * 64
    encoded = args[args.index('--environment') + 1]
    decoded = dict(value.split('=', 1) for value in next(csv.reader([encoded])))
    assert decoded == current()['environment']
    secret = args[args.index('--secret') + 1]
    assert 'environment-variable=t_token,id=e6qc2ghhip6925lllhm7,' in secret
    assert '--async' not in args


@pytest.mark.parametrize('damage', [
    'environment', 'plaintext-token', 'inactive', 'mount', 'bad-secret',
])
def test_unsafe_current_configuration_fails_before_upload(tmp_path, damage):
    """Unsafe inherited configuration fails before any cloud mutation."""
    data = current()
    if damage == 'environment':
        data['environment'] = {'INVALID': 123}
    elif damage == 'plaintext-token':
        data['environment']['t_token'] = 'must-not-be-copied'
    elif damage == 'inactive':
        data['status'] = 'CREATING'
    elif damage == 'mount':
        data['mounts'] = [{'name': 'unsupported-special-mount'}]
    else:
        data['secrets'] = [{'id': 'secret'}]
    with pytest.raises(DeployError):
        deploy.version_arguments(settings(tmp_path), data, 'package.zip', 'f' * 64, SHA)


def test_unrelated_secrets_network_and_log_options_are_preserved(tmp_path):
    """Only the application's required token reference is replaced."""
    data = current()
    data.update(connectivity={'networkId': 'networkid'}, concurrency='2',
                logOptions={'logGroupId': 'loggroup', 'minLevel': 'INFO'},
                secrets=[{'id': 'othersecret', 'versionId': 'otherversion',
                          'key': 'otherkey', 'environmentVariable': 'OTHER'}])
    args = deploy.version_arguments(settings(tmp_path), data, 'package.zip', 'f' * 64, SHA)
    assert args[args.index('--network-id') + 1] == 'networkid'
    assert args[args.index('--concurrency') + 1] == '2'
    assert args[args.index('--log-group-id') + 1] == 'loggroup'
    assert args.count('--secret') == 2


def test_verification_requires_active_matching_version_and_preserved_fields(tmp_path):
    """Readback proves the new active version retains required configuration."""
    cfg = settings(tmp_path)
    before = current()
    after = deepcopy(before)
    after['id'] = 'newversion'
    after['secrets'] = [cfg.secret.as_rest()]
    after['description'] = f'GitHub master {SHA}; package sha256 ' + 'f' * 64
    deploy.verify_version(cfg, before, after, sha=SHA, digest='f' * 64)
    for key, bad in [('id', 'oldversion'), ('status', 'CREATING'),
                     ('serviceAccountId', 'other'), ('environment', {}), ('secrets', [])]:
        damaged = dict(after, **{key: bad})
        with pytest.raises(DeployError):
            deploy.verify_version(cfg, before, damaged, sha=SHA, digest='f' * 64)


def test_publication_does_not_upload_until_metadata_validated(tmp_path):
    """Metadata validation must precede upload and version creation."""
    cfg = settings(tmp_path)
    cfg.archive.write_bytes(b'ZIP content')
    yc = create_autospec(deploy.YandexCLI, instance=True, spec_set=True)
    yc.call.return_value = dict(current(), environment={'t_token': 'plaintext'})
    with pytest.raises(DeployError):
        deploy.publish(cfg, yc, SHA, '123', '1')
    assert yc.call.call_count == 1


def test_successful_publication_is_single_upload_create_and_readback(tmp_path):
    """One publication verifies its identity without invoking real trading."""
    cfg = settings(tmp_path)
    cfg.archive.write_bytes(b'ZIP content')
    yc = create_autospec(deploy.YandexCLI, instance=True, spec_set=True)
    before = current()
    digest = deploy.package_digest(cfg.archive)
    after = dict(before, id='newversion', secrets=[cfg.secret.as_rest()],
                 description=f'GitHub master {SHA}; package sha256 {digest}')
    yc.call.side_effect = [before, {}, after, after]
    result = deploy.publish(cfg, yc, SHA, '123', '2')
    assert result == 'newversion'
    calls = yc.call.call_args_list
    assert len(calls) == 4
    assert calls[0].args[0][:4] == ['serverless', 'function', 'version', 'get-by-tag']
    assert calls[1].args[0][:3] == ['storage', 's3api', 'put-object']
    assert f'functions/{SHA}/123-2.zip' in calls[1].args[0]
    assert calls[2].args[0][:4] == ['serverless', 'function', 'version', 'create']
    assert calls[3].args[0][:4] == ['serverless', 'function', 'version', 'get-by-tag']
    assert not any('invoke' in call.args[0] for call in calls)


def test_missing_package_never_calls_cloud(tmp_path):
    """Missing build artifacts cannot initiate publication."""
    yc = create_autospec(deploy.YandexCLI, instance=True, spec_set=True)
    with pytest.raises(DeployError):
        deploy.publish(settings(tmp_path), yc, SHA, '123', '1')
    yc.call.assert_not_called()


def deployment_environment(tmp_path):
    """Explicit metadata and synthetic credentials, not production secret values."""
    return {
        'FOLDER_ID': 'folder', 'SA_ID': 'deploysa', 'BUCKET': 'bucket',
        'FUNCTION_NAME': 'python-function', 'ZIP_PATH': str(tmp_path / 'package.zip'),
        'FUNCTION_RUNTIME': 'python312', 'FUNCTION_ENTRYPOINT': 'handler.handler',
        'FUNCTION_MEMORY': '256MB', 'FUNCTION_TIMEOUT': '60s', 'FUNCTION_SA_ID': 'runtimesa',
        'LOCKBOX_SECRET_ID': 'secret', 'LOCKBOX_VERSION_ID': 'version',
        'LOCKBOX_KEY': 't_token', 'LOCKBOX_ENVIRONMENT_VARIABLE': 't_token',
        'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.actions.githubusercontent.com/token?a=b',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-request-token',
        'GITHUB_REF': 'refs/heads/master', 'GITHUB_REPOSITORY': 'gorpsys/autorepeater',
        'DEPLOY_SHA': SHA, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
        'RUNNER_TEMP': str(tmp_path),
    }


def test_settings_preserve_explicit_cloud_identities(tmp_path):
    """The deployment SA and function runtime SA are intentionally distinct."""
    cfg = deploy.settings_from_environment(deployment_environment(tmp_path))
    assert cfg.service_account_id == 'deploysa'
    assert cfg.function.service_account_id == 'runtimesa'
    assert cfg.archive == tmp_path / 'package.zip'


@pytest.mark.parametrize('key,value', [
    ('SA_ID', ''), ('SA_ID', 'injected,value'), ('FUNCTION_MEMORY', '128MB'),
    ('FUNCTION_TIMEOUT', '30s'), ('FUNCTION_RUNTIME', 'python311'),
])
def test_invalid_settings_have_no_production_fallback(tmp_path, key, value):
    """Missing identities and unapproved resources fail rather than use defaults."""
    env = dict(deployment_environment(tmp_path), **{key: value})
    with pytest.raises(DeployError):
        deploy.settings_from_environment(env)


def test_oidc_exchange_uses_sa_audience_and_masks_both_tokens(monkeypatch, tmp_path, capsys):
    """Both tokens remain in process; request audience is replaced, not duplicated."""
    env = deployment_environment(tmp_path)
    env['ACTIONS_ID_TOKEN_REQUEST_URL'] += '&audience=old'
    reader = create_autospec(deploy.request_json, spec_set=True, side_effect=[
        {'value': 'synthetic-oidc'},
        {'access_token': 'synthetic-iam', 'expires_in': 3600, 'token_type': 'Bearer'},
    ])
    monkeypatch.setattr(deploy, 'request_json', reader)
    assert deploy.exchange_token(env, 'deploysa') == 'synthetic-iam'
    first, second = [call.args[0] for call in reader.call_args_list]
    assert parse_qs(urlsplit(first.full_url).query) == {'a': ['b'], 'audience': ['deploysa']}
    assert first.get_header('Authorization') == 'Bearer synthetic-request-token'
    assert second.full_url == 'https://auth.yandex.cloud/oauth/token'
    assert second.get_method() == 'POST'
    assert parse_qs(second.data.decode()) == {
        'grant_type': ['urn:ietf:params:oauth:grant-type:token-exchange'],
        'requested_token_type': ['urn:ietf:params:oauth:token-type:access_token'],
        'audience': ['deploysa'], 'subject_token': ['synthetic-oidc'],
        'subject_token_type': ['urn:ietf:params:oauth:token-type:id_token'],
    }
    assert capsys.readouterr().out == (
        '::add-mask::synthetic-oidc\n::add-mask::synthetic-iam\n')


@pytest.mark.parametrize('url', [
    'http://example.actions.githubusercontent.com/token', 'https://evil.example/token',
    'https://actions.githubusercontent.com.evil/token',
    'https://user@example.actions.githubusercontent.com/token',
    'https://example.actions.githubusercontent.com:444/token',
])
def test_untrusted_oidc_origin_is_rejected_before_request(monkeypatch, tmp_path, url):
    """GitHub request credentials cannot be sent to another origin."""
    reader = create_autospec(deploy.request_json, spec_set=True)
    monkeypatch.setattr(deploy, 'request_json', reader)
    env = dict(deployment_environment(tmp_path), ACTIONS_ID_TOKEN_REQUEST_URL=url)
    with pytest.raises(DeployError):
        deploy.exchange_token(env, 'deploysa')
    reader.assert_not_called()


@pytest.mark.parametrize('response', [
    {'token_type': 'Other', 'expires_in': 3600},
    {'token_type': 'Bearer', 'expires_in': True},
    {'token_type': 'Bearer', 'expires_in': '3600'},
    {'token_type': 'Bearer', 'expires_in': 0},
    {'token_type': 'Bearer', 'expires_in': 3600, 'access_token': ''},
])
def test_invalid_iam_response_never_reaches_cli(monkeypatch, tmp_path, response):
    """Malformed exchange responses cannot authorize cloud commands."""
    reader = create_autospec(
        deploy.request_json, spec_set=True,
        side_effect=[{'value': 'synthetic-oidc'}, response])
    monkeypatch.setattr(deploy, 'request_json', reader)
    with pytest.raises(DeployError):
        deploy.exchange_token(deployment_environment(tmp_path), 'deploysa')


def test_masking_escapes_workflow_command_characters(capsys):
    """Percent signs must be escaped for the runner masking command."""
    assert deploy.mask_token('synthetic%token') == 'synthetic%token'
    assert capsys.readouterr().out == '::add-mask::synthetic%25token\n'


@pytest.mark.parametrize('failure', [None, 'os', 'timeout', 'exit', 'size', 'json', 'shape'])
def test_cli_is_bounded_private_and_does_not_repeat_mutations(monkeypatch, tmp_path, failure):
    """yc uses an isolated home, finite timeouts and sanitized failure reporting."""
    result = subprocess.CompletedProcess(['yc'], 0, b'{"id":"version"}', b'private stderr')
    runner = create_autospec(subprocess.run, spec_set=True, return_value=result)
    if failure == 'os':
        runner.side_effect = OSError('synthetic-iam')
    elif failure == 'timeout':
        runner.side_effect = subprocess.TimeoutExpired('synthetic-iam', 30)
    elif failure == 'exit':
        result.returncode = 1
    elif failure == 'size':
        result.stdout = b'x' * (deploy.MAX_RESPONSE_BYTES + 1)
    elif failure == 'json':
        result.stdout = b'synthetic-iam'
    elif failure == 'shape':
        result.stdout = b'[]'
    monkeypatch.setattr(deploy.subprocess, 'run', runner)
    home = tmp_path / 'private-home'
    client = deploy.YandexCLI(settings(tmp_path), 'synthetic-iam', home)
    if failure:
        with pytest.raises(DeployError) as error:
            client.call(['serverless', 'function', 'version', 'create'], 'create', timeout=90)
        assert 'synthetic-iam' not in str(error.value)
    else:
        assert client.call(['serverless', 'function', 'version', 'create'],
                           'create', timeout=90) == {'id': 'version'}
    assert runner.call_count == 1
    command = runner.call_args.args[0]
    assert '--retry' in command and command[command.index('--retry') + 1] == '0'
    assert command[command.index('--timeout') + 1] == '90s'
    assert runner.call_args.kwargs['timeout'] == 105
    assert runner.call_args.kwargs['env']['HOME'] == str(home)
    assert runner.call_args.kwargs['env']['YC_TOKEN'] == 'synthetic-iam'
    assert 'synthetic-iam' not in command


@pytest.mark.parametrize('kind', ['empty', 'symlink', 'parent-symlink'])
def test_untrusted_package_path_cannot_be_published(tmp_path, kind):
    """Cloud artifacts must be regular nonempty files through trusted path components."""
    path = tmp_path / 'package.zip'
    path.write_bytes(b'' if kind == 'empty' else b'ZIP')
    if kind == 'symlink':
        link = tmp_path / 'link.zip'
        link.symlink_to(path)
        path = link
    elif kind == 'parent-symlink':
        folder = tmp_path / 'linked'
        folder.symlink_to(tmp_path, target_is_directory=True)
        path = folder / path.name
    with pytest.raises(DeployError):
        deploy.package_digest(path)


@pytest.mark.parametrize('data', [
    {'secrets': {}}, {'secrets': [{'id': 'x'}]},
    {'concurrency': 'invalid'}, {'tmpfsSize': False},
    {'connectivity': {'subnetId': 'legacy'}},
    {'metadataOptions': {'unknown': 'ENABLED'}},
    {'metadataOptions': {'gceHttpEndpoint': 'UNKNOWN'}},
    {'logOptions': {'disabled': 'false'}},
    {'logOptions': {'logGroupId': 'group', 'folderId': 'folder'}},
    {'logOptions': {'minLevel': 'UNKNOWN'}},
])
def test_invalid_preserved_fields_fail_before_upload(tmp_path, data):
    """Unsupported inherited fields must not be silently omitted."""
    with pytest.raises(DeployError):
        deploy.version_arguments(settings(tmp_path), dict(current(), **data), 'key', 'f' * 64, SHA)


def test_preservation_handles_all_supported_optional_fields(tmp_path):
    """Named accounts, tmpfs, metadata and logging settings survive publication."""
    data = dict(current(), environment={}, tmpfsSize='1048576',
                namedServiceAccounts={'alias': 'account'},
                metadataOptions={'awsV1HttpEndpoint': 'DISABLED', 'gceHttpEndpoint': 'ENABLED'},
                logOptions={'disabled': True, 'folderId': 'folder'})
    args = deploy.version_arguments(settings(tmp_path), data, 'key', 'f' * 64, SHA)
    assert '--environment' not in args
    assert args[args.index('--tmpfs-size') + 1] == '1048576'
    assert args[args.index('--add-service-account') + 1] == 'alias=alias,id=account'
    assert args[args.index('--metadata-options') + 1] == (
        'aws-v1-http-endpoint=disabled,gce-http-endpoint=enabled')
    assert '--no-logging' in args and '--log-folder-id' in args


def test_duplicate_secret_names_and_changed_operational_settings_are_rejected(tmp_path):
    """Readback must validate actual provider facts, not synthesize missing bindings."""
    cfg = settings(tmp_path)
    data = dict(current(), secrets=[cfg.secret.as_rest()] * 2)
    with pytest.raises(DeployError, match='duplicate'):
        deploy.read_secret_bindings(data)
    after = dict(current(), id='new', secrets=[cfg.secret.as_rest()], concurrency='2',
                 description=f'GitHub master {SHA}; package sha256 ' + 'f' * 64)
    with pytest.raises(DeployError, match='operational'):
        deploy.verify_version(cfg, current(), after, sha=SHA, digest='f' * 64)


@pytest.mark.parametrize('run_id,attempt', [('0', '1'), ('1', '0'), ('invalid', '1')])
def test_run_identity_is_checked_before_cloud(tmp_path, run_id, attempt):
    """Object names cannot collide through invalid workflow run identities."""
    client = create_autospec(deploy.YandexCLI, instance=True, spec_set=True)
    with pytest.raises(DeployError):
        deploy.publish(settings(tmp_path), client, SHA, run_id, attempt)
    client.call.assert_not_called()


def test_publication_rejects_different_latest_version(tmp_path):
    """A concurrent external publisher cannot be mistaken for this successful deployment."""
    cfg = settings(tmp_path)
    cfg.archive.write_bytes(b'ZIP')
    client = create_autospec(deploy.YandexCLI, instance=True, spec_set=True)
    client.call.side_effect = [current(), {}, {'id': 'created'}, {'id': 'other'}]
    with pytest.raises(DeployError, match='differs'):
        deploy.publish(cfg, client, SHA, '123', '1')


@pytest.mark.parametrize('mode', ['success', 'branch', 'repository', 'failure', 'invalid'])
def test_main_is_master_only_and_safely_reports_failures(monkeypatch, tmp_path, capsys, mode):
    """Entrypoint never invokes real trading or obtains credentials for untrusted refs."""
    env = deployment_environment(tmp_path)
    (tmp_path / 'package.zip').write_bytes(b'ZIP')
    if mode == 'branch':
        env['GITHUB_REF'] = 'refs/heads/feature'
    if mode == 'repository':
        env['GITHUB_REPOSITORY'] = 'fork/repo'
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    exchange = create_autospec(deploy.exchange_token, spec_set=True, return_value='synthetic-iam')
    publish = create_autospec(deploy.publish, spec_set=True, return_value='version')
    if mode == 'failure':
        publish.side_effect = DeployError('safe publication failure')
    if mode == 'invalid':
        exchange.side_effect = ValueError('private provider details')
    monkeypatch.setattr(deploy, 'exchange_token', exchange)
    monkeypatch.setattr(deploy, 'publish', publish)
    assert deploy.main() == (0 if mode == 'success' else 1)
    if mode in ('branch', 'repository'):
        exchange.assert_not_called()
    if mode == 'success':
        assert publish.call_args.args[2:] == (SHA, '123', '1')
    error = capsys.readouterr().err
    assert 'synthetic-iam' not in error and 'private provider details' not in error
