"""Offline payload contracts; package and process ports never access the network."""
from copy import deepcopy
from email.parser import BytesParser
from io import BytesIO
import importlib.machinery
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec
from zipfile import ZipFile

import pytest

from scripts import cloud_dependencies as cloud
from scripts import install_dependencies as installer
from scripts import package_wheels, runtime_lock, sdk_wheel


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('field,value', [
    ('system', 'darwin'), ('machine', 'aarch64'), ('implementation', 'pypy'),
    ('version', (3, 11)), ('libc', ('musl', '1.2')), ('libc', ('glibc', '2.16')),
    ('libc', ('glibc', 'broken')),
])
def test_platform_rejects_before_network(monkeypatch, field, value):
    """Unsupported host facts must precede any acquisition effects."""
    facts = {'system': 'linux', 'machine': 'x86_64', 'implementation': 'cpython',
             'version': (3, 12), 'libc': ('glibc', '2.17')}
    facts[field] = value
    monkeypatch.setattr(cloud.sys, 'platform', facts['system'])
    monkeypatch.setattr(cloud.sys, 'implementation', SimpleNamespace(name=facts['implementation']))
    monkeypatch.setattr(cloud.sys, 'version_info', facts['version'])
    monkeypatch.setattr(cloud.platform, 'machine', lambda: facts['machine'])
    monkeypatch.setattr(cloud.platform, 'libc_ver', lambda: facts['libc'])
    acquire = create_autospec(sdk_wheel.acquire_sdk, spec_set=True)
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', acquire)
    with pytest.raises(cloud.PayloadError, match='unsupported'):
        cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    acquire.assert_not_called()


@pytest.mark.parametrize('machine', ['x86_64', 'AMD64'])
def test_platform_accepts_cloud_baseline(monkeypatch, machine):
    """Aliases for x86_64 share the same minimum glibc baseline."""
    monkeypatch.setattr(cloud.platform, 'machine', lambda: machine)
    monkeypatch.setattr(cloud.platform, 'libc_ver', lambda: ('glibc', '2.17'))
    cloud.check_platform()


@pytest.fixture(name='pipeline')
def fixture_pipeline(monkeypatch):
    # pylint: disable=too-many-locals
    """Model all locked packages and preserved wheel files in an empty target."""
    document = runtime_lock.load_lock(ROOT)
    selected = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.EXPLICIT_VENDORED,
        sdk_wheel.load_vendored_wheel(ROOT), None)
    acquire = create_autospec(sdk_wheel.acquire_sdk, spec_set=True, return_value=selected)
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', acquire)
    hashes = {p['name']: p['artifacts'][0]['sha256'] for p in document['packages']}
    contents = {}
    for package in document['packages']:
        directory = package['name'].replace('-', '_') + '-' + package['version'] + '.dist-info/'
        metadata = ('Metadata-Version: 2.1\nName: ' + package['name'] + '\nVersion: '
                    + package['version'] + '\nLicense: MIT\n'
                    + ('Author: Ruslan Sirazhetdinov\n' if package['name'] == 'iprotopy' else '')
                    + ''.join('Requires-Dist: ' + r + '\n' for r in package['requires_dist'])
                    + '\n').encode()
        contents[directory + 'METADATA'] = metadata
        contents[package['name'].replace('-', '_') + '/data.txt'] = b'runtime data'
        if package['name'] != 'iprotopy':
            contents[directory + 'LICENSE'] = b'Original licence text'
    contents['certifi/core.py'] = b'# upstream source\n'
    contents['certifi/cacert.pem'] = b'original certificates'
    locked = create_autospec(package_wheels.locked_files, spec_set=True,
                             return_value=package_wheels.file_hashes(dict(contents)))
    monkeypatch.setattr(package_wheels, 'locked_files', locked)
    paths = []

    def prepare(root, lock, wheelhouse, budget, sdk):
        assert root == ROOT and lock == document and sdk == selected
        assert 0 < budget.remaining() <= 300
        paths.append(wheelhouse)
        for package in lock['packages']:
            (wheelhouse / package['artifacts'][0]['filename']).write_bytes(b'owned wheel')
        return hashes

    prepare_port = create_autospec(installer.prepare_wheels, spec_set=True, side_effect=prepare)
    monkeypatch.setattr(installer, 'prepare_wheels', prepare_port)
    monkeypatch.setattr(package_wheels, 'payload_files', create_autospec(
        package_wheels.payload_files, spec_set=True, return_value=contents))

    def pip(arguments, root, budget):
        assert root == ROOT and 0 < budget.remaining() <= 300
        if arguments[0] == 'download':
            source = Path(arguments[arguments.index('--find-links') + 1])
            destination = Path(arguments[arguments.index('--dest') + 1])
            paths.append(destination)
            for path in source.glob('*.whl'):
                (destination / path.name).write_bytes(path.read_bytes())
        elif arguments[0] == 'install':
            target = Path(arguments[arguments.index('--target') + 1])
            paths.append(target)
            assert not list(target.iterdir())
            for name, data in contents.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        else:
            assert arguments[:2] == ['inspect', '--path']
            return json.dumps({'version': '1', 'environment': runtime_lock.MARKER_ENVIRONMENT,
                               'installed': [{'metadata': {
                                   'name': p['name'], 'version': p['version'],
                                   'requires_dist': p['requires_dist']}}
                                             for p in document['packages']]})
        return ''

    pip_port = create_autospec(installer.pip, spec_set=True, side_effect=pip)
    monkeypatch.setattr(installer, 'pip', pip_port)
    smoke = create_autospec(cloud.smoke_payload, spec_set=True)
    monkeypatch.setattr(cloud, 'smoke_payload', smoke)
    return SimpleNamespace(document=document, acquire=acquire, prepare=prepare_port,
                           pip=pip_port, smoke=smoke, hashes=hashes, paths=paths,
                           contents=contents, locked=locked)


@pytest.mark.parametrize('vendored', [False, True])
def test_full_root_payload_locked_two_stages_and_notice_scope(pipeline, vendored):
    """The full graph, original metadata, chosen notices and acquisition facts survive."""
    app = {'main.py': b'# app', 'handler.py': b'# handler', 'autorepeater/configs/a.json': b'{}'}
    entries = cloud.prepare_payload(ROOT, app, use_vendored_sdk=vendored)
    pipeline.acquire.assert_called_once_with(ROOT, 'vendored' if vendored else 'auto')
    pipeline.prepare.assert_called_once()
    calls = [c.args[0] for c in pipeline.pip.call_args_list]
    assert [a[0] for a in calls] == ['download', 'install', 'inspect']
    download, install, inspect = calls
    assert '--no-index' in download and '--require-hashes' in download
    assert '--only-binary=:all:' in download and '--no-deps' in download
    assert download.count('--platform') == 2
    assert 'manylinux2014_x86_64' in download and 'manylinux_2_17_x86_64' in download
    assert '--python-version' in download and '3.12' in download
    assert '--ignore-installed' in install and '--no-compile' in install
    assert '--no-index' in install and '--require-hashes' in install
    assert '--target' in install and inspect[1] == '--path'
    assert all('--timeout' in a and '--retries' in a for a in calls[:2])
    assert 'requirements.txt' not in entries and not any(n.endswith('.whl') for n in entries)
    assert not any(n.startswith(('pytest', 'pip/', '.venv/', '__pycache__/')) for n in entries)
    for name in ('grpcio-tools', 'mypy-protobuf', 'antlr4-python3-runtime', 'setuptools'):
        package = next(p for p in pipeline.document['packages'] if p['name'] == name)
        assert name.replace('-', '_') + '-' + package['version'] + '.dist-info/METADATA' in entries
    manifest = json.loads(entries[cloud.MANIFEST])
    assert manifest['lock_sha256'] == runtime_lock.canonical_digest(pipeline.document)
    assert manifest['artifact_hashes'] == pipeline.hashes
    assert manifest['sdk_artifact_sha256'] == sdk_wheel.SDK_HASH
    assert {p['name']: p['artifact']['sha256'] for p in manifest['packages']} == pipeline.hashes
    iprotopy = next(p for p in manifest['packages'] if p['name'] == 'iprotopy')
    assert iprotopy['license']['fulfilled'] is False
    assert iprotopy['license']['standalone_license_present'] is False
    assert iprotopy['license']['policy'] == 'user_notice_only'
    notice = entries['THIRD_PARTY_NOTICES/iprotopy'].decode()
    assert notice == ('Author: Ruslan Sirazhetdinov\nVersion: 0.3.0\nSource: '
                      'https://github.com/irusland/iprotopy/tree/'
                      '031ae0a6536f07964c9802dfe099efeb701e1c25\n')
    assert b'MIT' in entries['iprotopy-0.3.0.dist-info/METADATA']
    assert b'Permission is hereby granted' not in entries['THIRD_PARTY_NOTICES/iprotopy']
    assert b'Source form' in entries['THIRD_PARTY_NOTICES/certifi']
    assert entries['certifi/core.py'] == b'# upstream source\n'
    pipeline.smoke.assert_called_once()
    staged = pipeline.smoke.call_args.args[0]
    assert staged in pipeline.paths
    assert all(not p.exists() for p in pipeline.paths)


@pytest.mark.parametrize('name', [
    'main.py', 'handler.py', 'autorepeater/evil.py', 'requirements.txt',
    'THIRD_PARTY_NOTICES/x', cloud.MANIFEST, '.env', '.idea/file', '../escape',
])
def test_dependency_collision_precedes_pip(pipeline, name):
    """Unsafe dependency roots are rejected before installing any bytes."""
    pipeline.contents[name] = b'unsafe'
    with pytest.raises(cloud.PayloadError, match='path|collision|reserved'):
        cloud.prepare_payload(ROOT, {'main.py': b'original', 'handler.py': b''})
    pipeline.pip.assert_not_called()
    pipeline.smoke.assert_not_called()
    assert all(not p.exists() for p in pipeline.paths)


@pytest.mark.parametrize('stage', ['download', 'install', 'inspect', 'smoke'])
def test_pipeline_failure_cleans_every_stage(pipeline, stage):
    """No failed download/install/validation leaves a published payload or staging."""
    if stage == 'smoke':
        pipeline.smoke.side_effect = cloud.PayloadError('payload import failure')
    else:
        original = pipeline.pip.side_effect

        def failure(arguments, root, budget):
            if arguments[0] == stage:
                raise installer.InstallError('pip failed')
            return original(arguments, root, budget)

        pipeline.pip.side_effect = failure
    with pytest.raises((cloud.PayloadError, installer.InstallError)):
        cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    assert all(not p.exists() for p in pipeline.paths)


@pytest.mark.parametrize('damage', ['missing', 'extra', 'metadata', 'data', 'symlink', 'cache'])
def test_payload_validation_detects_install_damage(pipeline, damage):
    """The actual target must preserve every original runtime file and exact closure."""
    original = pipeline.pip.side_effect

    def tamper(arguments, root, budget):
        result = original(arguments, root, budget)
        if arguments[0] == 'install':
            target = Path(arguments[arguments.index('--target') + 1])
            if damage == 'missing':
                (target / 'iprotopy-0.3.0.dist-info/METADATA').unlink()
            elif damage == 'extra':
                path = target / 'pytest-1.0.dist-info/METADATA'
                path.parent.mkdir()
                path.write_text('Name: pytest\nVersion: 1.0\n')
            elif damage == 'metadata':
                (target / 'iprotopy-0.3.0.dist-info/METADATA').write_bytes(b'changed')
            elif damage == 'data':
                (target / 'certifi/cacert.pem').write_bytes(b'changed')
            elif damage == 'symlink':
                (target / 'escape').symlink_to(ROOT, target_is_directory=True)
            else:
                path = target / '__pycache__/x.pyc'
                path.parent.mkdir()
                path.write_bytes(b'cache')
        return result

    pipeline.pip.side_effect = tamper
    with pytest.raises(cloud.PayloadError):
        cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    pipeline.smoke.assert_not_called()
    assert all(not p.exists() for p in pipeline.paths)


def damaged_report(report, damage):
    """Create a concrete bad inspect DTO for independent graph/JSON checks."""
    if damage == 'invalid':
        return 'not JSON: credential secret'
    value = json.loads(report)
    if damage == 'missing':
        value['installed'].pop()
    elif damage == 'version':
        value['installed'][0]['metadata']['version'] = '9.0.0'
    elif damage == 'extra':
        value['installed'].append({'metadata': {'name': 'pip', 'version': '1.0'}})
    return json.dumps(value)


@pytest.mark.parametrize('damage', [
    'invalid', 'missing', 'extra', 'version', 'hashes', 'download-missing', 'download-changed',
])
def test_stage_reports_and_artifacts_must_match_lock(pipeline, damage):
    """No inspect/receipt/roundtrip failure can be published as a valid payload."""
    if damage == 'hashes':
        pipeline.hashes['grpcio'] = '1' * 64
    elif damage == 'download-changed':
        def changed(document, wheelhouse):
            assert document == pipeline.document
            if wheelhouse.name.startswith('cloud-download-'):
                return {**pipeline.contents, 'changed.py': b'changed'}
            return pipeline.contents

        package_wheels.payload_files.side_effect = changed
    else:
        original = pipeline.pip.side_effect

        def changed(arguments, root, budget):
            result = original(arguments, root, budget)
            if arguments[0] == 'download' and damage == 'download-missing':
                path = Path(arguments[arguments.index('--dest') + 1])
                next(path.glob('*.whl')).unlink()
            if arguments[0] == 'inspect':
                return damaged_report(result, damage)
            return result

        pipeline.pip.side_effect = changed
    with pytest.raises((cloud.PayloadError, runtime_lock.LockError, installer.InstallError)):
        cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    pipeline.smoke.assert_not_called()
    assert all(not p.exists() for p in pipeline.paths)


@pytest.mark.parametrize('damage', [
    'license', 'certifi-source', 'iprotopy-author', 'unknown-license', 'unfulfilled-license',
])
def test_licence_gate_preserves_user_scope_and_fails_other_gaps(pipeline, damage, monkeypatch):
    """The exact notice-only choice does not certify other unknown licence obligations."""
    if damage == 'license':
        del pipeline.contents['grpcio-1.84.0.dist-info/LICENSE']
    elif damage == 'certifi-source':
        del pipeline.contents['certifi/core.py']
    elif damage == 'iprotopy-author':
        name = 'iprotopy-0.3.0.dist-info/METADATA'
        pipeline.contents[name] = pipeline.contents[name].replace(b'Ruslan Sirazhetdinov', b'other')
    else:
        inventory = deepcopy(runtime_lock.runtime_inventory(ROOT))
        licence = inventory['grpcio']['license']
        if damage == 'unknown-license':
            licence['status'] = 'unknown'
        else:
            licence['fulfilled'] = False
        monkeypatch.setattr(runtime_lock, 'runtime_inventory', create_autospec(
            runtime_lock.runtime_inventory, spec_set=True, return_value=inventory))
    with pytest.raises(cloud.PayloadError, match='licence|source form|notice-only'):
        cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    pipeline.smoke.assert_not_called()


@pytest.mark.parametrize('reason', [
    sdk_wheel.Reason.DNS, sdk_wheel.Reason.HTTP_404, sdk_wheel.Reason.TIMEOUT,
])
def test_cloud_reuses_real_package_acquisition_no_reserve_failure(monkeypatch, reason):
    """GitHub-backed wheels have no automatic source builds or hidden SDK reselection."""
    selected = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.DNS, sdk_wheel.load_vendored_wheel(ROOT), None)
    sdk = create_autospec(sdk_wheel.acquire_sdk, spec_set=True, return_value=selected)
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', sdk)
    port = create_autospec(package_wheels.Transport, instance=True, spec_set=True)
    port.fetch.side_effect = sdk_wheel.AvailabilityError(reason)
    monkeypatch.setattr(package_wheels, 'WorkerTransport', lambda: port)
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    with pytest.raises(package_wheels.PackageError, match='reserve unavailable'):
        cloud.prepare_payload(ROOT, {})
    assert port.fetch.call_count == 1
    assert port.fetch.call_args.args[0].name == 'antlr4-python3-runtime'
    sdk.assert_called_once_with(ROOT, 'auto')
    pip.assert_not_called()


def test_cloud_two_stage_deadlines_do_not_reset_per_package(pipeline, monkeypatch):
    """Acquisition shares one deadline, while target installation has its own bound."""
    budgets = []
    original = installer.Budget

    def budget(seconds):
        value = original(seconds)
        budgets.append(value)
        return value

    monkeypatch.setattr(installer, 'Budget', create_autospec(
        installer.Budget, spec_set=True, side_effect=budget))
    cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    assert len(budgets) == 2 and all(b.seconds == 300 for b in budgets)
    assert pipeline.prepare.call_args.args[3] is budgets[0]
    assert pipeline.pip.call_args_list[0].args[2] is budgets[0]
    assert all(c.args[2] is budgets[1] for c in pipeline.pip.call_args_list[1:])


def test_bundled_setuptools_metadata_is_preserved_without_extra_distribution(pipeline):
    """Vendored nested dist-info is runtime data, not an installed top-level package."""
    name = 'setuptools/_vendor/packaging-99.0.dist-info/METADATA'
    pipeline.contents[name] = b'Name: packaging\nVersion: 99.0\n'
    pipeline.contents['setuptools/_vendor/packaging-99.0.dist-info/LICENSE'] = b'original MIT'
    pipeline.locked.return_value = package_wheels.file_hashes(dict(pipeline.contents))
    files = cloud.prepare_payload(ROOT, {'main.py': b'', 'handler.py': b''})
    assert files[name] == pipeline.contents[name]
    manifest = json.loads(files[cloud.MANIFEST])
    expected = next(p for p in pipeline.document['packages'] if p['name'] == 'packaging')
    assert next(p for p in manifest['packages'] if p['name'] == 'packaging')['version'] == (
        expected['version'])


def test_all_offline_helpers_import_under_stdlib_only_other_cwd(tmp_path):
    """The real complete helper set needs no SDK, packaging or development tools at import."""
    code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
            'from scripts import cloud_dependencies; '
            'assert not any(n.startswith(("t_tech", "packaging", "pytest")) for n in sys.modules)')
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code], cwd=tmp_path,
                            env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=10,
                            check=False)
    assert result.returncode == 0, result.stderr


def test_unapproved_sdk_artifact_fails_before_packages(pipeline):
    """Even a verified selected SDK cannot exceed the canonical artifact approvals."""
    wheel = pipeline.acquire.return_value.wheel
    bad = deepcopy(wheel.manifest)
    object.__setattr__(bad, 'sha256', '1' * 64)
    pipeline.acquire.return_value = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.EXPLICIT_VENDORED,
        sdk_wheel.ValidatedWheel(wheel.data, wheel.license_bytes, bad), None)
    with pytest.raises(runtime_lock.LockError, match='outside runtime lock'):
        cloud.prepare_payload(ROOT, {})
    pipeline.prepare.assert_not_called()


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'os', 'invalid', 'origin', 'native'])
def test_smoke_process_failures_are_safe(tmp_path, monkeypatch, failure):
    """Missing/native/origin/process failures expose no arbitrary child output."""
    run = create_autospec(subprocess.run, spec_set=True)
    run.return_value = subprocess.CompletedProcess([], 0, b'{"status":"ok"}', b'')
    if failure == 'timeout':
        run.side_effect = subprocess.TimeoutExpired([], 1)
    elif failure == 'os':
        run.side_effect = OSError('credential secret')
    elif failure == 'nonzero':
        run.return_value = subprocess.CompletedProcess([], 1, b'', b'credential secret')
    else:
        run.return_value = subprocess.CompletedProcess([], 0, failure.encode(), b'')
    monkeypatch.setattr(cloud.subprocess, 'run', run)
    with pytest.raises(cloud.PayloadError) as error:
        cloud.smoke_payload(tmp_path)
    assert 'credential' not in str(error.value)
    command = run.call_args.args[0]
    assert command[:3] == [sys.executable, '-I', '-S']
    assert run.call_args.kwargs['cwd'] == tmp_path
    assert 0 < run.call_args.kwargs['timeout'] <= 60
    assert 'PYTHONPATH' not in run.call_args.kwargs['env']


def test_real_isolated_smoke_no_site_packages_and_no_rpc(tmp_path):
    """Real isolated imports only use the payload, stdlib and namespace paths."""
    for name in cloud.SMOKE_IMPORTS:
        parts = name.split('.')
        for i in range(1, len(parts)):
            (tmp_path / Path(*parts[:i])).mkdir(parents=True, exist_ok=True)
        path = tmp_path / Path(*parts).with_suffix('.py')
        if any(n.startswith(name + '.') for n in cloud.SMOKE_IMPORTS):
            path = tmp_path / Path(*parts) / '__init__.py'
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'from pathlib import Path\nMARKER = "owned"\n')
    cloud.smoke_payload(tmp_path)
    assert not list(tmp_path.rglob('__pycache__'))


@pytest.mark.parametrize('damage', ['missing', 'native', 'origin', 'namespace', 'rpc', 'timeout'])
def test_real_smoke_detects_damage(tmp_path, damage):
    """Actual isolated processes fail on incomplete imports, RPC and timeouts."""
    for name in cloud.SMOKE_IMPORTS:
        path = tmp_path / Path(*name.split('.')).with_suffix('.py')
        if any(n.startswith(name + '.') for n in cloud.SMOKE_IMPORTS):
            path = tmp_path / Path(*name.split('.')) / '__init__.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    target = tmp_path / 'handler.py'
    if damage == 'missing':
        target.unlink()
    elif damage == 'native':
        path = tmp_path / 'grpc_tools' / (
            '_protoc_compiler' + importlib.machinery.EXTENSION_SUFFIXES[0])
        path.write_bytes(b'invalid native shared object')
    elif damage == 'origin':
        target.write_text('__file__ = "/tmp/foreign/handler.py"\n')
    elif damage == 'namespace':
        target.write_text('import google\ngoogle.__path__.append("/tmp/foreign")\n')
    elif damage == 'rpc':
        target.write_text('import socket\nsocket.create_connection(("127.0.0.1", 80))\n')
    else:
        target.write_text('import time\ntime.sleep(5)\n')
    with pytest.raises(cloud.PayloadError):
        cloud.smoke_payload(tmp_path, timeout=.15 if damage == 'timeout' else 5)


def test_sdk_metadata_not_rewritten_in_wheel():
    """The SDK keeps the mandatory iprotopy dependency declaration unchanged."""
    wheel = sdk_wheel.load_vendored_wheel(ROOT)
    with ZipFile(BytesIO(wheel.data)) as archive:
        metadata = BytesParser().parsebytes(archive.read(
            't_tech_investments-1.51.0.dist-info/METADATA'))
    assert 'iprotopy' in '\n'.join(metadata.get_all('Requires-Dist'))
