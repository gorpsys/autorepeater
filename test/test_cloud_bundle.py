"""Ready bundles are checked offline in pytest; real package requests belong to Make."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import create_autospec
from zipfile import ZipFile, ZipInfo

import pytest

from scripts import check_cloud_bundle as checker
from scripts import cloud_dependencies as cloud
from scripts import install_dependencies as installer
from scripts import runtime_lock, sdk_wheel


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name='bundles')
def bundle_fixture(tmp_path, monkeypatch):
    # pylint: disable=too-many-locals
    """Model a full locked runtime with original metadata and explicit package reports."""
    document = runtime_lock.load_lock(ROOT)
    app = checker.application_files(ROOT)
    wheel = sdk_wheel.load_vendored_wheel(ROOT)
    prefix = 'vendor/t-tech-investments/'
    vendored = {**app, 'requirements.txt': (
        '--index-url https://pypi.org/simple\n./' + prefix + wheel.manifest.identity.filename
        + ' --hash=sha256:' + wheel.manifest.sha256 + '\n-r requirements-runtime.txt\n'
    ).encode(), 'requirements-runtime.txt': runtime_lock.render_requirements(document)[
        'requirements-runtime.txt'].encode(), prefix + wheel.manifest.identity.filename: wheel.data,
        prefix + 'LICENSE': wheel.license_bytes,
        prefix + 'manifest.json': runtime_lock.canonical_bytes(runtime_lock.read_json(
            ROOT / prefix / 'manifest.json'))}
    payload = {}
    report = {'version': '1', 'install': []}
    for package in document['packages']:
        directory = package['name'].replace('-', '_') + '-' + package['version'] + '.dist-info/'
        payload[directory + 'METADATA'] = (
            'Name: ' + package['name'] + '\nVersion: ' + package['version'] + '\nLicense: MIT\n'
            + ('Author: Ruslan Sirazhetdinov\n' if package['name'] == 'iprotopy' else '')
            + ''.join('Requires-Dist: ' + r + '\n' for r in package['requires_dist']) + '\n'
        ).encode()
        if package['name'] != 'iprotopy':
            payload[directory + 'LICENSE'] = b'original licence'
        artifact = package['artifacts'][0]
        url = (tmp_path / 'unpacked' / prefix / artifact['filename']).as_uri() if (
            package['name'] == sdk_wheel.NAME) else artifact['url']
        report['install'].append({'metadata': {
            'name': package['name'], 'version': package['version'],
            'requires_dist': package['requires_dist']}, 'download_info': {
                'url': url, 'archive_info': {'hashes': {'sha256': artifact['sha256']}}}})
    payload['certifi/core.py'] = b'# source'
    payload['certifi/cacert.pem'] = b'certificates'
    payload['requests/sessions.py'] = b'def unused():\n    return True\n'
    payload['t_tech_investments-1.51.0.dist-info/licenses/LICENSE'] = wheel.license_bytes
    notices, packages = cloud.payload_notices(document, ROOT, payload)
    hashes = {p['name']: p['artifacts'][0]['sha256'] for p in document['packages']}
    locked = create_autospec(cloud.package_wheels.locked_files, spec_set=True,
                             return_value=cloud.package_wheels.file_hashes(dict(payload)))
    monkeypatch.setattr(cloud.package_wheels, 'locked_files', locked)
    for facts in packages:
        facts['artifact'] = next(p['artifacts'][0] for p in document['packages']
                                 if p['name'] == facts['name'])
    offline = {**app, **payload, **notices, cloud.MANIFEST: runtime_lock.canonical_bytes({
        **installer.receipt(document, 'vendored', hashes),
        'sdk_artifact_sha256': sdk_wheel.SDK_HASH,
        'packages': packages, 'payload_files': cloud.package_wheels.file_hashes(payload)})}
    smoke = create_autospec(cloud.smoke_payload, spec_set=True)
    monkeypatch.setattr(cloud, 'smoke_payload', smoke)
    report['environment'] = runtime_lock.MARKER_ENVIRONMENT
    return {'document': document, 'app': app, 'vendored': vendored, 'offline': offline,
            'payload': payload, 'report': report, 'smoke': smoke}


def write_bundle(path, files):
    """Write a fixture ZIP, with no install or external calls."""
    with ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_bundle_rejects_original_nul_name_before_normalization(tmp_path):
    """Mutate both ZIP headers: zipfile accepts CRC/bytes after truncating the stored name."""
    path = write_bundle(tmp_path / 'nul.zip', {'handler.py!suffix': b'original handler'})
    path.write_bytes(path.read_bytes().replace(b'handler.py!suffix', b'handler.py\x00suffix'))
    with ZipFile(path) as archive:
        member = archive.infolist()[0]
        assert member.filename == 'handler.py'
        assert member.orig_filename == 'handler.py\x00suffix'
        assert archive.read(member) == b'original handler'
        assert archive.testzip() is None
    with pytest.raises(checker.BundleError, match='^unsafe bundle member$'):
        checker.read_bundle(path)


@pytest.mark.parametrize('mode', ['vendored', 'offline'])
def test_bundle_checks_reuse_payload_smoke_and_clean_staging(bundles, tmp_path, monkeypatch, mode):
    """Offline has no pip call; vendored installs archive requirements in a private target."""
    paths = []

    def pip(arguments, root, budget):
        assert 0 < budget.remaining() <= 300
        assert arguments[0] == 'install'
        assert '--require-hashes' in arguments and '--ignore-installed' in arguments
        assert '--no-compile' in arguments and '--no-cache-dir' in arguments
        assert '--only-binary=:all:' in arguments
        assert arguments[arguments.index('--index-url') + 1] == 'https://pypi.org/simple'
        assert arguments[arguments.index('--timeout') + 1] == '15'
        assert arguments[arguments.index('--retries') + 1] == '0'
        assert arguments[arguments.index('-r') + 1] == 'requirements.txt'
        assert not any('tbank' in arg or 'extra-index' in arg for arg in arguments)
        target = Path(arguments[arguments.index('--target') + 1])
        paths.extend([root, target])
        assert not list(target.iterdir())
        for name, data in bundles['payload'].items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        report = deepcopy(bundles['report'])
        sdk = next(item for item in report['install'] if item['metadata']['name'] == sdk_wheel.NAME)
        sdk['download_info']['url'] = (root / 'vendor/t-tech-investments'
                                       / sdk_wheel.FILENAME).as_uri()
        report['environment'] = runtime_lock.MARKER_ENVIRONMENT
        Path(arguments[arguments.index('--report') + 1]).write_text(
            json.dumps(report), encoding='utf-8')
        return ''

    port = create_autospec(installer.pip, spec_set=True, side_effect=pip)
    monkeypatch.setattr(installer, 'pip', port)
    path = write_bundle(tmp_path / 'bundle.zip', bundles[mode])
    checker.check_bundle(ROOT, path, mode)
    assert port.call_count == (1 if mode == 'vendored' else 0)
    bundles['smoke'].assert_called_once()
    target = bundles['smoke'].call_args.args[0]
    assert bundles['smoke'].call_args.kwargs['timeout'] <= 60
    assert not target.exists() and all(not p.exists() for p in paths)


@pytest.mark.parametrize('damage', [
    'missing', 'app', 'requirements', 'wheel', 'license', 'manifest', 'extra', 'remote-sdk',
])
def test_bad_vendored_bundle_fails_before_install(bundles, tmp_path, monkeypatch, damage):
    """The exact requirements chain and SDK bytes are checked before package requests."""
    files = dict(bundles['vendored'])
    if damage == 'missing':
        del files['requirements-runtime.txt']
    elif damage == 'app':
        files['handler.py'] = b'changed'
    elif damage in ('requirements', 'remote-sdk'):
        files['requirements.txt'] += b'--extra-index-url https://opensource.tbank.ru/\n'
    elif damage == 'extra':
        files['scripts/escape.py'] = b''
    else:
        suffix = {'wheel': sdk_wheel.FILENAME, 'license': 'LICENSE',
                  'manifest': 'manifest.json'}
        files['vendor/t-tech-investments/' + suffix[damage]] = b'changed'
    port = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', port)
    with pytest.raises(checker.BundleError):
        checker.check_bundle(ROOT, write_bundle(tmp_path / 'bundle.zip', files), 'vendored')
    port.assert_not_called()
    bundles['smoke'].assert_not_called()


@pytest.mark.parametrize('damage', [
    'missing', 'extra', 'version', 'requirements', 'manifest',
    'hash', 'notice', 'license', 'wheel', 'data',
])
def test_bad_offline_bundle_has_no_installer_or_network(bundles, tmp_path, monkeypatch, damage):
    """Graph, receipt and notice drift cannot be hidden by local imports."""
    files = dict(bundles['offline'])
    if damage == 'missing':
        del files['iprotopy-0.3.0.dist-info/METADATA']
    elif damage == 'extra':
        files['pytest-1.0.dist-info/METADATA'] = b'Name: pytest\nVersion: 1.0\n'
    elif damage == 'version':
        files['iprotopy-0.3.0.dist-info/METADATA'] = b'Name: iprotopy\nVersion: 0.1\n'
    elif damage == 'requirements':
        files['requirements.txt'] = b'install something'
    elif damage == 'notice':
        files['THIRD_PARTY_NOTICES/iprotopy'] += b'MIT invented text'
    elif damage == 'license':
        del files['grpcio-1.84.0.dist-info/LICENSE']
    elif damage == 'wheel':
        files['other.whl'] = b'wheelhouse'
    elif damage == 'data':
        del files['certifi/core.py']
    else:
        manifest = json.loads(files[cloud.MANIFEST])
        if damage == 'hash':
            manifest['artifact_hashes']['iprotopy'] = '0' * 64
        else:
            manifest['lock_sha256'] = '0' * 64
        files[cloud.MANIFEST] = json.dumps(manifest).encode()
    port = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', port)
    with pytest.raises((checker.BundleError, cloud.PayloadError, runtime_lock.LockError,
                        installer.InstallError)):
        checker.check_bundle(ROOT, write_bundle(tmp_path / 'bundle.zip', files), 'offline')
    port.assert_not_called()
    bundles['smoke'].assert_not_called()


def test_missing_licence_identifies_exact_package_without_file_contents(bundles):
    """A real packaging failure names the locked identity, without provider/file details."""
    files = dict(bundles['payload'])
    files['grpcio-1.84.0.dist-info/LICENSE'] = b''
    with pytest.raises(cloud.PayloadError) as error:
        cloud.payload_notices(bundles['document'], ROOT, files)
    assert str(error.value) == 'missing payload licence materials: grpcio==1.84.0'


@pytest.mark.parametrize('name', [
    'requests/sessions.py', 'certifi/cacert.pem',
    't_tech_investments-1.51.0.dist-info/licenses/LICENSE',
])
@pytest.mark.parametrize('rewrite_manifest', [False, True])
def test_offline_existing_bytes_are_authenticated(
        bundles, tmp_path, monkeypatch, name, rewrite_manifest):
    """Valid CRC and even a rewritten payload declaration cannot authorize new bytes."""
    baseline = write_bundle(tmp_path / 'baseline.zip', bundles['offline'])
    checker.check_bundle(ROOT, baseline, 'offline')
    bundles['smoke'].reset_mock()
    files = dict(bundles['offline'])
    files[name] = b'changed but nonempty'
    if rewrite_manifest:
        manifest = json.loads(files[cloud.MANIFEST])
        manifest['payload_files'] = cloud.package_wheels.file_hashes({
            key: value for key, value in files.items() if key in bundles['payload']})
        files[cloud.MANIFEST] = runtime_lock.canonical_bytes(manifest)
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    with pytest.raises((checker.BundleError, cloud.package_wheels.PackageError),
                       match='file|content'):
        checker.check_bundle(ROOT, write_bundle(tmp_path / 'damaged.zip', files), 'offline')
    pip.assert_not_called()
    bundles['smoke'].assert_not_called()


@pytest.mark.parametrize('name,version,filename', [
    ('antlr4-python3-runtime', '4.13.2', 'LICENSE.txt'), ('grpcio-tools', '1.81.1', 'LICENSE'),
])
def test_missing_wheel_licence_retains_exact_upstream_text(bundles, name, version, filename):
    """Supplement the actual wheel omission with immutable release licence bytes."""
    files = dict(bundles['payload'])
    del files[f'{name.replace("-", "_")}-{version}.dist-info/LICENSE']
    notices, packages = cloud.payload_notices(bundles['document'], ROOT, files)
    licence = (ROOT / 'third_party' / f'{name}-{version}' / filename).read_bytes()
    assert notices['THIRD_PARTY_NOTICES/' + name].endswith(licence)
    facts = next(p for p in packages if p['name'] == name)
    assert facts['license']['standalone_license_present'] is False
    assert facts['license_files'] == ['THIRD_PARTY_NOTICES/' + name]


@pytest.mark.parametrize('damage', [
    'missing', 'hash', 'symlink', 'identity', 'version', 'expression', 'repository', 'evidence',
])
@pytest.mark.parametrize('identity', [
    ('antlr4-python3-runtime', '4.13.2', 'LICENSE.txt'), ('grpcio-tools', '1.81.1', 'LICENSE'),
])
def test_supplement_cannot_bypass_licence_integrity(
        bundles, tmp_path, monkeypatch, damage, identity):
    # pylint: disable=too-many-locals
    """An absent, changed or redirected licence/source never certifies the payload."""
    name, version, filename = identity
    files = dict(bundles['payload'])
    del files[f'{name.replace("-", "_")}-{version}.dist-info/LICENSE']
    root = tmp_path
    path = root / 'third_party' / f'{name}-{version}' / filename
    path.parent.mkdir(parents=True)
    original = ROOT / 'third_party' / f'{name}-{version}' / filename
    if damage not in ('missing', 'symlink'):
        path.write_bytes(original.read_bytes())
    inventory = deepcopy(runtime_lock.runtime_inventory(ROOT))
    port = create_autospec(runtime_lock.runtime_inventory, spec_set=True, return_value=inventory)
    monkeypatch.setattr(runtime_lock, 'runtime_inventory', port)
    # Both supplements are needed for a successful baseline in this independent root.
    for other, policy in cloud.SUPPLEMENTAL_LICENSES.items():
        other_path = root / 'third_party' / (other + '-' + policy['version']) / policy['filename']
        if other != name:
            other_path.parent.mkdir(parents=True, exist_ok=True)
            other_path.write_bytes((ROOT / other_path.relative_to(root)).read_bytes())
    if damage not in ('missing', 'symlink'):
        cloud.payload_notices(bundles['document'], root, files)
    if damage == 'hash':
        path.write_bytes(b'invented licence')
    elif damage == 'symlink':
        path.symlink_to(original)
    document = deepcopy(bundles['document'])
    changes = {'identity': ('source', 'commit', '0' * 40),
               'expression': ('license', 'expression', 'MIT'),
               'repository': ('source', 'url', 'https://github.com/other/project'),
               'evidence': ('license', 'evidence', ['unverified declaration'])}
    if damage == 'version':
        next(p for p in document['packages'] if p['name'] == name)['version'] = '0'
    elif damage in changes:
        group, field, value = changes[damage]
        inventory[name][group][field] = value
    reason = ('supplemental licence source identity drift' if damage in (
        'identity', 'version', 'expression', 'repository', 'evidence') else
        'supplemental licence hash drift' if damage == 'hash' else
        'supplemental licence file unavailable or unsafe')
    with pytest.raises(cloud.PayloadError, match='^' + reason + '$'):
        cloud.payload_notices(document, root, files)


@pytest.mark.parametrize('name', [
    '../escape', '/absolute', 'a/../b', 'a\\b', 'a:b', 'a//b',
    'scripts/x.py', 'a/__pycache__/x.py',
])
def test_unsafe_zip_never_extracts(tmp_path, name):
    """Reject path escapes and forbidden roots without extracting or inspecting them."""
    path = write_bundle(tmp_path / 'bundle.zip', {name: b'unsafe'})
    with pytest.raises(checker.BundleError):
        checker.read_bundle(path)
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('damage', ['duplicate', 'symlink', 'collision', 'size', 'count', 'crc'])
def test_archive_limits_types_and_crc(tmp_path, monkeypatch, damage):
    """ZIP bounds precede decompression; duplicate and file/directory conflicts fail closed."""
    path = tmp_path / 'bundle.zip'
    with ZipFile(path, 'w') as archive:
        info = ZipInfo('data')
        if damage == 'symlink':
            info.external_attr = 0o120777 << 16
        archive.writestr(info, b'data')
        if damage == 'duplicate':
            with pytest.warns(UserWarning):
                archive.writestr('data', b'other')
        if damage == 'collision':
            archive.writestr('data/file', b'other')
    if damage == 'size':
        monkeypatch.setattr(checker, 'MAX_BYTES', 1)
    if damage == 'count':
        monkeypatch.setattr(checker, 'MAX_ENTRIES', 0)
    if damage == 'crc':
        data = path.read_bytes().replace(b'dataPK', b'failPK', 1)
        path.write_bytes(data)
    with pytest.raises(checker.BundleError):
        checker.read_bundle(path)


def test_stdlib_script_module_runpy_other_cwd(tmp_path):
    """All checker entrypoints are usable without site packages or inherited PYTHONPATH."""
    script = ROOT / 'scripts/check_cloud_bundle.py'
    commands = [[sys.executable, '-I', '-S', str(script), '--help'],
                [sys.executable, '-I', '-S', '-c',
                 f'import runpy,sys;sys.argv=[{str(script)!r},"--help"];'
                 f'runpy.run_path({str(script)!r},run_name="__main__")']]
    for command in commands:
        result = subprocess.run(command, cwd=tmp_path, env={'PATH': '/usr/bin:/bin'},
                                check=False, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert b'--expected-sha' in result.stdout


@pytest.mark.parametrize('damage', [
    'missing', 'extra', 'version', 'hash', 'source', 'duplicate', 'format', 'json',
])
def test_vendored_report_rejects_artifact_metadata_and_source_drift(bundles, tmp_path, damage):
    """pip report hashes, URLs and installed closure must match the canonical lock."""
    report = deepcopy(bundles['report'])
    if damage == 'missing':
        report['install'].pop()
    elif damage == 'extra':
        report['install'][0]['metadata']['name'] = 'unexpected'
    elif damage == 'version':
        report['install'][0]['metadata']['version'] = '0'
    elif damage == 'hash':
        report['install'][0]['download_info']['archive_info']['hashes']['sha256'] = '0' * 64
    elif damage == 'source':
        report['install'][0]['download_info']['url'] = 'https://opensource.tbank.ru/wheel'
    elif damage == 'duplicate':
        report['install'].append(report['install'][0])
    else:
        report['version'] = 'unknown'
    path = tmp_path / 'report.json'
    path.write_text('invalid' if damage == 'json' else json.dumps(report), encoding='utf-8')
    with pytest.raises((checker.BundleError, runtime_lock.LockError)):
        checker.verify_install_report(bundles['document'], path, tmp_path / 'unpacked')


@pytest.mark.parametrize('failure', ['install', 'timeout', 'import', 'origin', 'native'])
def test_ready_bundle_failure_cleans_staging(bundles, tmp_path, monkeypatch, failure):
    """Subprocess failures never produce a package success or leave unpacked staging."""
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    mode = 'vendored' if failure == 'install' else 'offline'
    if failure == 'install':
        pip.side_effect = installer.InstallError('pip failed')
    else:
        bundles['smoke'].side_effect = cloud.PayloadError('payload import ' + failure)
    with pytest.raises((cloud.PayloadError, installer.InstallError)):
        checker.check_bundle(ROOT, write_bundle(tmp_path / 'bundle.zip', bundles[mode]), mode)
    if mode == 'offline':
        pip.assert_not_called()
        assert not bundles['smoke'].call_args.args[0].exists()
    else:
        assert not pip.call_args.args[1].exists()


@pytest.mark.parametrize('failure', ['', 'build', 'vendored', 'offline', 'sha', 'git'])
def test_package_goal_exact_sha_two_modes_fail_closed_and_cleanup(tmp_path, monkeypatch, failure):
    """The real Make entrypoint checks both ZIPs and prints evidence only on full success."""
    paths = []

    def build(root, path, **flags):
        assert root == ROOT and flags['use_vendored_sdk'] is True
        assert flags['offline_archive'] == (path.name == 'offline.zip')
        paths.append(path)
        if failure == 'build':
            raise cloud.PayloadError('build failed')
        return path

    def verify(root, path, mode):
        assert root == ROOT and path.name == mode + '.zip'
        if failure == mode:
            raise checker.BundleError('bundle failed')

    build_port = create_autospec(checker.builder.build_archive, spec_set=True, side_effect=build)
    verify_port = create_autospec(checker.check_bundle, spec_set=True, side_effect=verify)
    monkeypatch.setattr(checker.builder, 'build_archive', build_port)
    monkeypatch.setattr(checker, 'check_bundle', verify_port)
    run = create_autospec(subprocess.run, spec_set=True, return_value=subprocess.CompletedProcess(
        [], 1 if failure == 'git' else 0, 'a' * 40 + '\n', 'credential-bearing error'))
    monkeypatch.setattr(checker.subprocess, 'run', run)
    expected = 'b' * 40 if failure == 'sha' else 'a' * 40
    if failure:
        with pytest.raises((checker.BundleError, cloud.PayloadError)):
            checker.check_packages(ROOT, expected)
    else:
        assert checker.check_packages(ROOT, expected) == {
            'status': 'ok', 'checkout_sha': 'a' * 40,
            'lock_sha256': runtime_lock.canonical_digest(runtime_lock.load_lock(ROOT))}
        assert [c.args[2] for c in verify_port.call_args_list] == ['vendored', 'offline']
    if failure in ('sha', 'git'):
        build_port.assert_not_called()
    assert all(not p.parent.exists() for p in paths)
    assert run.call_args.kwargs['timeout'] == 10
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('error', [
    checker.BundleError('invalid bundle'), OSError('sensitive detail'),
    subprocess.TimeoutExpired(['sensitive'], 10), json.JSONDecodeError('sensitive', '', 0),
])
def test_cli_safe_failure_does_not_emit_success(monkeypatch, capsys, error):
    """Child/provider details and success evidence never leak on a failed package stage."""
    port = create_autospec(checker.check_packages, spec_set=True, side_effect=error)
    monkeypatch.setattr(checker, 'check_packages', port)
    assert checker.main(['--project-root', str(ROOT), '--expected-sha', 'a' * 40]) == 1
    output = capsys.readouterr()
    assert not output.out and 'cloud package smoke failed' in output.err
    assert 'sensitive' not in output.err


def test_cli_success_prints_verified_checkout_and_lock(monkeypatch, capsys):
    """The JSON summary is returned only after the whole checker succeeds."""
    facts = {'status': 'ok', 'checkout_sha': 'a' * 40, 'lock_sha256': 'b' * 64}
    port = create_autospec(checker.check_packages, spec_set=True, return_value=facts)
    monkeypatch.setattr(checker, 'check_packages', port)
    assert checker.main(['--project-root', str(ROOT)]) == 0
    assert json.loads(capsys.readouterr().out) == facts
