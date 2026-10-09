"""Publish a checked cloud ZIP with short-lived OIDC credentials; never invoke trading."""
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from scripts.deploy_support import (
    MAX_RESPONSE_BYTES, DeployError, commit_sha, record, request_json, text,
)


def identifier(value: object, label: str) -> str:
    """Cloud IDs and property values cannot inject another CLI property."""
    result = text(value, label)
    if re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', result) is None:
        raise DeployError(f'invalid {label}')
    return result


@dataclass(frozen=True)
class FunctionSettings:
    """The explicit execution settings approved for this application."""
    runtime: str
    entrypoint: str
    memory: str
    timeout: str
    service_account_id: str


@dataclass(frozen=True)
class SecretBinding:
    """Only Lockbox references, never a secret payload."""
    secret_id: str
    version_id: str
    key: str
    environment_variable: str

    def as_rest(self) -> dict[str, str]:
        """Match the public protobuf JSON schema used by yc json-rest."""
        return {'id': self.secret_id, 'versionId': self.version_id, 'key': self.key,
                'environmentVariable': self.environment_variable}


@dataclass(frozen=True)
class Settings:
    """Deployment identity is distinct from the function's runtime identity."""
    folder_id: str
    service_account_id: str
    bucket: str
    function_name: str
    archive: Path
    function: FunctionSettings
    secret: SecretBinding


def settings_from_environment(environment: dict[str, str]) -> Settings:
    """A missing deployment field is an error, not a production fallback."""
    def value(key: str) -> str:
        return identifier(environment.get(key), key)
    function = FunctionSettings(value('FUNCTION_RUNTIME'), value('FUNCTION_ENTRYPOINT'),
                                value('FUNCTION_MEMORY'), value('FUNCTION_TIMEOUT'),
                                value('FUNCTION_SA_ID'))
    if (function.runtime, function.entrypoint, function.memory, function.timeout) != (
            'python312', 'handler.handler', '256MB', '60s'):
        raise DeployError('unexpected function execution settings')
    return Settings(value('FOLDER_ID'), value('SA_ID'), value('BUCKET'), value('FUNCTION_NAME'),
                    Path(text(environment.get('ZIP_PATH'), 'ZIP_PATH')), function,
                    SecretBinding(value('LOCKBOX_SECRET_ID'), value('LOCKBOX_VERSION_ID'),
                                  value('LOCKBOX_KEY'), value('LOCKBOX_ENVIRONMENT_VARIABLE')))


def mask_token(value: object) -> str:
    """Use the runner's masking protocol before a token can reach any child process."""
    token = text(value, 'token')
    print('::add-mask::' + token.replace('%', '%25'), flush=True)
    return token


def exchange_token(environment: dict[str, str], service_account_id: str) -> str:
    """GitHub targets the federation AUD; Yandex exchange targets the deployment SA."""
    url = urlsplit(text(environment.get('ACTIONS_ID_TOKEN_REQUEST_URL'), 'OIDC request URL'))
    if url.scheme != 'https' or not url.hostname or not (
            url.hostname.endswith('.actions.githubusercontent.com')):
        raise DeployError('untrusted OIDC request URL')
    if url.username or url.password or url.port not in (None, 443):
        raise DeployError('untrusted OIDC request URL')
    iam_audience = identifier(service_account_id, 'deployment service account')
    oidc_audience = text(environment.get('GITHUB_OIDC_AUDIENCE'), 'GitHub OIDC audience')
    query = [(key, value) for key, value in parse_qsl(url.query) if key != 'audience']
    request_url = urlunsplit(url._replace(query=urlencode([*query, ('audience', oidc_audience)])))
    request_token = text(environment.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN'), 'OIDC request token')
    oidc_request = Request(request_url, headers={'Authorization': f'Bearer {request_token}'})
    oidc = mask_token(credential_response(oidc_request, 'GitHub OIDC').get('value'))
    body = urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                      'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
                      'audience': iam_audience, 'subject_token': oidc,
                      'subject_token_type': 'urn:ietf:params:oauth:token-type:id_token'}).encode()
    request = Request('https://auth.yandex.cloud/oauth/token', data=body,
                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
    response = credential_response(request, 'Yandex IAM exchange')
    expires = response.get('expires_in')
    if response.get('token_type') != 'Bearer' or isinstance(expires, bool) or (
            not isinstance(expires, int) or expires <= 0):
        raise DeployError('invalid IAM token response')
    return mask_token(response.get('access_token'))


def credential_response(request: Request, stage: str) -> dict[str, object]:
    """Name the failed boundary without exposing its URL, headers or provider body."""
    try:
        return record(request_json(request))
    except DeployError as error:
        raise DeployError(f'{stage} failed: {error}') from None


class YandexCLI:  # pylint: disable=too-few-public-methods
    """Capture provider output privately, with finite command/RPC bounds and no profile keys."""

    def __init__(self, settings: Settings, token: str, home: Path):
        self.settings = settings
        self.token = text(token, 'IAM token')
        self.home = home

    def call(self, arguments: list[str], label: str, *, timeout: int = 30) -> dict[str, object]:
        """No retries of a mutation whose remote result could be unknown."""
        self.home.mkdir(parents=True, exist_ok=True)
        command = ['yc', *arguments, '--folder-id', self.settings.folder_id,
                   '--format', 'json-rest', '--timeout', f'{timeout}s', '--retry', '0',
                   '--no-user-output', '--no-browser']
        environment = dict(os.environ, HOME=str(self.home), YC_TOKEN=self.token)
        try:
            result = subprocess.run(command, env=environment, capture_output=True,
                                    timeout=timeout + 15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError(f'cloud command unavailable or timed out: {label}') from None
        if result.returncode:
            raise DeployError(f'cloud command failed: {label}; exit={result.returncode}')
        if len(result.stdout) > MAX_RESPONSE_BYTES:
            raise DeployError(f'cloud response exceeds size limit: {label}')
        try:
            return record(json.loads(result.stdout))
        except (ValueError, UnicodeError):
            raise DeployError(f'invalid cloud JSON: {label}') from None


def package_digest(path: Path) -> str:
    """Hash exact uploaded bytes; the server verifies the same SHA-256 for the package."""
    if not path.is_file() or path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents):
        raise DeployError('cloud ZIP missing or symlinked')
    if path.stat().st_size == 0:
        raise DeployError('cloud ZIP is empty')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def environment_variables(current: dict[str, object]) -> dict[str, str]:
    """Preserve full string values using CSV for the CLI map, not shell interpolation."""
    values = record(current.get('environment', {}))
    if any(re.fullmatch('[A-Za-z][A-Za-z0-9_]*', key) is None or not isinstance(value, str)
           for key, value in values.items()):
        raise DeployError('invalid function environment')
    if 't_token' in values:
        raise DeployError('plaintext t_token conflicts with Lockbox binding')
    return {key: str(value) for key, value in values.items()}


def read_secret_bindings(current: dict[str, object]) -> list[dict[str, str]]:
    """Actual provider facts, before any required binding is inserted or replaced."""
    values = current.get('secrets', [])
    if not isinstance(values, list):
        raise DeployError('invalid function secret bindings')
    result = []
    seen = set()
    for item in values:
        data = record(item)
        fields = ('id', 'versionId', 'key', 'environmentVariable')
        binding = {key: identifier(data.get(key), 'secret reference') for key in fields}
        name = binding['environmentVariable']
        if name in seen:
            raise DeployError('duplicate secret environment variable')
        seen.add(name)
        result.append(binding)
    return result


def secret_bindings(settings: Settings, current: dict[str, object]) -> list[dict[str, str]]:
    """Replace only the configured token reference; other secret bindings remain intact."""
    result = [binding for binding in read_secret_bindings(current)
              if binding['environmentVariable'] != settings.secret.environment_variable]
    result.append({key: identifier(value, 'secret reference')
                   for key, value in settings.secret.as_rest().items()})
    return result


def preserved_arguments(current: dict[str, object]) -> list[str]:
    """Keep operational settings; refuse unsupported configuration before publication."""
    unsupported = ('mounts', 'storageMounts', 'asyncInvocationConfig')
    if any(current.get(key) for key in unsupported):
        raise DeployError('configured mounts/async invocation need explicit deployment support')
    args: list[str] = []
    for key, flag in (('concurrency', '--concurrency'), ('tmpfsSize', '--tmpfs-size')):
        if key in current:
            value = text(current[key], key)
            if re.fullmatch('[0-9]+', value) is None:
                raise DeployError(f'invalid {key}')
            args.extend((flag, value))
    connectivity = record(current.get('connectivity', {}))
    if connectivity.get('networkId'):
        args.extend(('--network-id', identifier(connectivity['networkId'], 'network ID')))
    if connectivity.get('subnetId'):
        raise DeployError('legacy subnet configuration needs explicit deployment support')
    for alias, account in record(current.get('namedServiceAccounts', {})).items():
        args.extend(('--add-service-account', f'alias={identifier(alias, "account alias")},'
                     f'id={identifier(account, "named service account")}'))
    options = record(current.get('metadataOptions', {}))
    if options:
        fields = {'awsV1HttpEndpoint': 'aws-v1-http-endpoint',
                  'gceHttpEndpoint': 'gce-http-endpoint'}
        if any(key not in fields or value not in ('ENABLED', 'DISABLED')
               for key, value in options.items()):
            raise DeployError('invalid metadata endpoint options')
        args.extend(('--metadata-options', ','.join(
            f'{fields[key]}={str(value).lower()}' for key, value in options.items())))
    args.extend(logging_arguments(record(current.get('logOptions', {}))))
    return args


def logging_arguments(options: dict[str, object]) -> list[str]:
    """Preserve the destination, level and explicit disabling of cloud logs."""
    args: list[str] = []
    disabled = options.get('disabled', False)
    if not isinstance(disabled, bool) or (options.get('logGroupId') and options.get('folderId')):
        raise DeployError('invalid cloud log options')
    if disabled:
        args.append('--no-logging')
    for key, flag in (('logGroupId', '--log-group-id'), ('folderId', '--log-folder-id')):
        if options.get(key):
            args.extend((flag, identifier(options[key], 'log destination')))
    level = options.get('minLevel')
    if level not in (None, '', 'LEVEL_UNSPECIFIED'):
        if level not in ('TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'):
            raise DeployError('invalid cloud minimum log level')
        args.extend(('--min-log-level', str(level).lower()))
    return args


def version_arguments(settings: Settings, current: dict[str, object], object_key: str,
                      digest: str, sha: str) -> list[str]:
    """Validate preservation before the first mutating command (upload)."""
    if current.get('status') != 'ACTIVE':
        raise DeployError('current function version is not ACTIVE')
    identifier(current.get('functionId'), 'current function ID')
    identifier(current.get('id'), 'current version ID')
    values = environment_variables(current)
    bindings = secret_bindings(settings, current)
    function = settings.function
    args = ['serverless', 'function', 'version', 'create',
            '--function-name', settings.function_name,
            '--runtime', function.runtime, '--entrypoint', function.entrypoint,
            '--memory', function.memory, '--execution-timeout', function.timeout,
            '--service-account-id', function.service_account_id,
            '--package-bucket-name', settings.bucket, '--package-object-name', object_key,
            '--package-sha256', digest, '--description',
            f'GitHub master {sha}; package sha256 {digest}']
    if values:
        output = io.StringIO()
        csv.writer(output, lineterminator='\n').writerow(
            [f'{key}={value}' for key, value in values.items()])
        args.extend(('--environment', output.getvalue().removesuffix('\n')))
    for binding in bindings:
        args.extend(('--secret', ','.join(f'{key}={binding[field]}' for key, field in (
            ('environment-variable', 'environmentVariable'), ('id', 'id'),
            ('version-id', 'versionId'), ('key', 'key')))))
    return [*args, *preserved_arguments(current)]


def verify_version(settings: Settings, before: dict[str, object], after: dict[str, object],
                   *, sha: str, digest: str) -> None:
    """Read back $latest without invoking the function or exposing environment values."""
    if identifier(after.get('id'), 'new version ID') == before['id']:
        raise DeployError('latest function version was not updated')
    expected = {'status': 'ACTIVE', 'functionId': before['functionId'],
                'runtime': settings.function.runtime, 'entrypoint': settings.function.entrypoint,
                'executionTimeout': settings.function.timeout,
                'serviceAccountId': settings.function.service_account_id,
                'description': f'GitHub master {sha}; package sha256 {digest}'}
    if any(after.get(key) != value for key, value in expected.items()) or (
            record(after.get('resources')).get('memory') != str(256 * 1024 ** 2)):
        raise DeployError('new function version identity or execution settings mismatch')
    expected_secrets = {binding['environmentVariable']: binding
                        for binding in secret_bindings(settings, before)}
    actual_secrets = {binding['environmentVariable']: binding
                      for binding in read_secret_bindings(after)}
    if (environment_variables(after) != environment_variables(before)
            or actual_secrets != expected_secrets):
        raise DeployError('new version environment or Lockbox binding mismatch')
    for key, default in (('concurrency', '0'), ('tmpfsSize', '0'), ('connectivity', {}),
                         ('namedServiceAccounts', {}), ('metadataOptions', {}), ('logOptions', {})):
        if before.get(key, default) != after.get(key, default):
            raise DeployError(f'new version operational settings mismatch: {key}')


def publish(settings: Settings, yc: YandexCLI, sha: str, run_id: str, attempt: str) -> str:
    """Immutable object name plus SHA-256 prevents cross-run ZIP substitution."""
    sha = commit_sha(sha)
    if re.fullmatch('[1-9][0-9]*', run_id) is None or re.fullmatch('[1-9][0-9]*', attempt) is None:
        raise DeployError('invalid deployment run identity')
    digest = package_digest(settings.archive)
    get_latest = ['serverless', 'function', 'version', 'get-by-tag',
                  '--function-name', settings.function_name, '--tag', '$latest']
    before = yc.call(get_latest, 'read current version')
    key = f'functions/{sha}/{run_id}-{attempt}.zip'
    arguments = version_arguments(settings, before, key, digest, sha)
    print(f'Publishing {sha}; package sha256={digest}; object={key}', flush=True)
    yc.call(['storage', 's3api', 'put-object', '--body', str(settings.archive),
             '--bucket', settings.bucket, '--key', key], 'upload package', timeout=120)
    created = yc.call(arguments, 'create function version', timeout=900)
    version_id = identifier(created.get('id'), 'created version ID')
    after = yc.call(get_latest, 'verify latest version')
    if after.get('id') != version_id:
        raise DeployError('latest version differs from the created version')
    verify_version(settings, before, after, sha=sha, digest=digest)
    print(f'Deployed ACTIVE version {version_id} for master {sha}; '
          'runtime settings verified', flush=True)
    return version_id


def main() -> int:
    """Credentials stay in this process and child environment, never in a yc profile."""
    try:
        if os.environ.get('GITHUB_REF') != 'refs/heads/master' or (
                os.environ.get('GITHUB_REPOSITORY') != 'gorpsys/autorepeater'):
            raise DeployError('production deployment requires repository master')
        settings = settings_from_environment(dict(os.environ))
        sha = commit_sha(os.environ.get('DEPLOY_SHA'))
        package_digest(settings.archive)
        token = exchange_token(dict(os.environ), settings.service_account_id)
        home = Path(text(os.environ.get('RUNNER_TEMP'), 'RUNNER_TEMP')) / 'yc-deploy-home'
        publish(settings, YandexCLI(settings, token, home), sha,
                text(os.environ.get('GITHUB_RUN_ID'), 'run ID'),
                text(os.environ.get('GITHUB_RUN_ATTEMPT'), 'run attempt'))
    except DeployError as error:
        print(f'Deployment stopped: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Deployment configuration or package invalid', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
