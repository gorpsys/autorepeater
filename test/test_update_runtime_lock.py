"""Explicit pip report update, subprocess bounds and source policy."""
import copy
import json
import subprocess
import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from scripts import runtime_lock as lock
from scripts import update_runtime_lock as updater
from scripts import sdk_wheel


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name='report')
def fixture_report():
    """Offline report with explicit metadata and approved inventory artifacts."""
    inventory = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))
    environment = dict(lock.MARKER_ENVIRONMENT, implementation_version='3.12.0',
                       os_name='posix', platform_release='fixture', platform_version='fixture',
                       python_full_version='3.12.0', platform_python_implementation='CPython')
    return {'version': '1', 'pip_version': '26.0', 'environment': environment,
            'install': [
                {'metadata': {'name': p['name'], 'version': p['version'],
                              'requires_dist': (
                                  updater.sdk_requirements(sdk_wheel.load_vendored_wheel(ROOT))
                                  if p['name'] == 't-tech-investments'
                                  else [f'{n}>=0' for n in p['requires']])},
                 'download_info': {'url': (f'file:///private/tmp/{p["artifacts"][0]["filename"]}'
                                           if p['name'] == 't-tech-investments'
                                           else p['artifacts'][0]['url']),
                                   'archive_info': {'hashes': {
                                       'sha256': p['artifacts'][0]['sha256']}}}}
                for p in inventory['entries'] if 'runtime' in p['scopes']]}


def test_report_builds_complete_lock_without_private_paths(report):
    """Pip metadata carries markers verbatim; SDK URL becomes stable repo identity."""
    result = updater.lock_from_report(report, ROOT)
    lock.validate_lock(result, ROOT)
    assert '/private/' not in json.dumps(result)
    assert {p['name'] for p in result['packages']} >= {
        'iprotopy', 'grpcio-tools', 'mypy-protobuf', 'antlr4-python3-runtime', 'setuptools'}


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(version='2'),
    lambda r: r.update(pip_version=None),
    lambda r: r['environment'].update(python_version='3.11'),
    lambda r: r['environment'].update(sys_platform='darwin'),
    lambda r: r['environment'].update(python_full_version='3.13.1'),
    lambda r: r['environment'].update(implementation_version='3.11.1'),
    lambda r: r['environment'].update(platform_python_implementation='PyPy'),
    lambda r: r['install'].pop(),
    lambda r: r['install'].append(copy.deepcopy(r['install'][0])),
    lambda r: r['install'][0]['metadata'].update(version='999'),
    lambda r: r['install'][0]['metadata'].update(requires_dist='bad'),
    lambda r: r['install'][0]['download_info']['archive_info'].update(hashes={}),
    lambda r: r['install'][0]['download_info'].update(url='https://evil.test/a.whl'),
    lambda r: next(p for p in r['install'] if p['metadata']['name'] == 't-tech-investments')
    ['download_info']['archive_info']['hashes'].update(sha256='0' * 64),
    lambda r: next(p for p in r['install'] if p['metadata']['name'] == 't-tech-investments')
    ['metadata'].update(requires_dist=['wrong>=1']),
])
def test_malformed_drift_or_unapproved_report_fails(report, mutation):
    """No best effort lock or implicit SDK approvals on resolver surprises."""
    mutation(report)
    with pytest.raises(lock.LockError):
        updater.lock_from_report(report, ROOT)


def test_resolver_uses_supported_pip_report_and_cloud_target(tmp_path):
    """No pip internals, global T-Bank index, inherited config or unbounded retries."""
    run = create_autospec(subprocess.run, spec_set=True)
    run.return_value = subprocess.CompletedProcess([], 0, '', '')
    updater.run_resolver(ROOT, tmp_path, ROOT / 'vendor/t-tech-investments' /
                         't_tech_investments-1.51.0-py3-none-any.whl', run=run)
    args = run.call_args.args[0]
    assert '--dry-run' in args and '--report' in args and '--ignore-installed' in args
    assert '--only-binary=:all:' in args
    assert args.count('--platform') == 2
    assert '--extra-index-url' not in args
    assert run.call_args.kwargs['timeout'] <= 300
    assert run.call_args.kwargs['env']['PIP_CONFIG_FILE'] == '/dev/null'


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('pip', 300), OSError('private')])
def test_resolver_failure_is_safe(tmp_path, failure):
    """No raw command/stderr exposure and no fabricated success report."""
    run = create_autospec(subprocess.run, spec_set=True)
    run.side_effect = failure
    with pytest.raises(lock.LockError, match='resolver'):
        updater.run_resolver(ROOT, tmp_path, tmp_path / 'sdk.whl', run=run)


def test_resolver_nonzero_is_not_a_report(tmp_path):
    """Solver failures cannot be interpreted as successful incomplete closure."""
    run = create_autospec(subprocess.run, spec_set=True)
    run.return_value = subprocess.CompletedProcess([], 1, '', 'private solver error')
    with pytest.raises(lock.LockError, match='resolver failed'):
        updater.run_resolver(ROOT, tmp_path, tmp_path / 'sdk.whl', run=run)


def test_explicit_report_update_uses_host_gate(report, tmp_path):
    """Offline import accepts only an explicit structured report under actual target."""
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(report), encoding='utf-8')
    assert updater.update(ROOT, report=path) == updater.lock_from_report(report, ROOT)


@pytest.mark.parametrize('vendored', [False, True])
def test_update_selects_validated_owned_sdk_before_resolver(
        report, tmp_path, monkeypatch, vendored):
    """Only the explicit updater resolves; immutable SDK bytes survive until report consumption."""
    acquire = create_autospec(updater.acquire_sdk, spec_set=True)
    wheel = sdk_wheel.load_vendored_wheel(ROOT)
    acquire.return_value = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.EXPLICIT_VENDORED, wheel, None)
    report_path = tmp_path / 'pip-report.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    resolver = create_autospec(updater.run_resolver, spec_set=True)
    owned = []

    def resolve(_root, directory, sdk_path):
        assert sdk_path.parent == directory
        assert sdk_path.read_bytes() == wheel.data
        owned.append(directory)
        return report_path

    resolver.side_effect = resolve
    monkeypatch.setattr(updater, 'acquire_sdk', acquire)
    monkeypatch.setattr(updater, 'run_resolver', resolver)
    assert updater.update(ROOT, vendored=vendored) == updater.lock_from_report(report, ROOT)
    acquire.assert_called_once_with(ROOT, 'vendored' if vendored else 'auto')
    assert not owned[0].exists()


def test_update_resolver_failure_cleans_owned_bytes(monkeypatch):
    """A failed resolver leaves no reusable temporary SDK or fabricated lock."""
    wheel = sdk_wheel.load_vendored_wheel(ROOT)
    acquire = create_autospec(updater.acquire_sdk, spec_set=True)
    acquire.return_value = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.EXPLICIT_VENDORED, wheel, None)
    resolver = create_autospec(updater.run_resolver, spec_set=True)
    resolver.side_effect = lock.LockError('resolver failed')
    monkeypatch.setattr(updater, 'acquire_sdk', acquire)
    monkeypatch.setattr(updater, 'run_resolver', resolver)
    with pytest.raises(lock.LockError):
        updater.update(ROOT)
    assert not resolver.call_args.args[1].exists()


def test_update_cli_writes_only_explicit_root_exports(report, tmp_path, monkeypatch, capsys):
    """CLI output is a reviewable lock, never an installed source receipt."""
    document = updater.lock_from_report(report, ROOT)
    update = create_autospec(updater.update, spec_set=True)
    update.return_value = document
    monkeypatch.setattr(updater, 'update', update)
    assert updater.main(['--project-root', str(tmp_path), '--vendored-sdk']) == 0
    update.assert_called_once_with(tmp_path, report=None, vendored=True)
    assert json.loads((tmp_path / 'runtime-lock.json').read_text(encoding='utf-8')) == document
    assert set(p.name for p in tmp_path.iterdir()) == {
        'runtime-lock.json', *lock.render_requirements(document)}
    assert lock.canonical_digest(document) in capsys.readouterr().out


@pytest.mark.parametrize('failure', [lock.LockError('safe'), OSError('private')])
def test_update_cli_failure_does_not_write_lock(tmp_path, monkeypatch, capsys, failure):
    """No policy/exports appear when validation fails."""
    update = create_autospec(updater.update, spec_set=True)
    update.side_effect = failure
    monkeypatch.setattr(updater, 'update', update)
    assert updater.main(['--project-root', str(tmp_path)]) == 1
    assert not list(tmp_path.iterdir())
    assert 'private' not in capsys.readouterr().err


def repack_wheel(data):
    """Different compression preserves every upstream member and RECORD checksum."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, \
            zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED,
                            compresslevel=1) as output:
        for item in source.infolist():
            output.writestr(item.filename, source.read(item.filename))
    return buffer.getvalue()


def test_approved_exact_sdk_pair_needs_explicit_update(report, monkeypatch):
    """Selection approval alone changes no lock; explicit update preserves both hashes."""
    reserve = sdk_wheel.load_vendored_wheel(ROOT)
    data = repack_wheel(reserve.data)
    sha256 = hashlib.sha256(data).hexdigest()
    assert sha256 != reserve.manifest.sha256
    approval = sdk_wheel.HashApproval('1.51.0', reserve.manifest.sha256, sha256,
                                      'test-human', '2026-10-12T00:00:00Z',
                                      'https://example.test/explicit-approval')
    approved_reserve = replace(reserve, manifest=replace(reserve.manifest, approvals=(approval,)))
    official = sdk_wheel.validate_wheel(
        data, replace(approved_reserve.manifest, sha256=sha256, size=len(data)),
        reserve.license_bytes)
    load = create_autospec(sdk_wheel.load_vendored_wheel, spec_set=True)
    load.return_value = approved_reserve
    monkeypatch.setattr(updater, 'load_vendored_wheel', load)
    monkeypatch.setattr(lock, 'load_vendored_wheel', load)
    sdk_item = next(p for p in report['install'] if p['metadata']['name'] == sdk_wheel.NAME)
    sdk_item['download_info']['archive_info']['hashes']['sha256'] = sha256
    with pytest.raises(lock.LockError):
        updater.lock_from_report(report, ROOT)
    document = updater.lock_from_report(report, ROOT, sdk=official)
    artifacts = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)['artifacts']
    assert [a['sha256'] for a in artifacts] == [reserve.manifest.sha256, sha256]
    assert artifacts[1]['url'] == 'tbank:sha256:' + sha256
    assert load.return_value.manifest.approvals == (approval,)
    load.return_value = reserve
    with pytest.raises(lock.LockError):
        lock.validate_lock(document, ROOT)
