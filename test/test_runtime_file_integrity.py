"""Independent file projections and real local installation facts, without network."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import importlib.machinery
import base64
import csv
from hashlib import sha256
from io import BytesIO, StringIO
import json
import marshal
from pathlib import Path
import py_compile
import subprocess
import sys
from types import CodeType
from unittest.mock import create_autospec
from zipfile import ZipFile
from test.test_sdk_wheel import wheel, licence

import pytest

from scripts import install_dependencies as installer
from scripts import package_wheels as wheels
from scripts import runtime_lock, sdk_wheel


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name='projection')
def fixture_projection(tmp_path):
    """Use real SDK bytes and RECORD; the temporary policy is independent of payload mutations."""
    package = next(p for p in runtime_lock.load_lock(ROOT)['packages']
                   if p['name'] == sdk_wheel.NAME)
    document = {'packages': [package]}
    data = sdk_wheel.load_vendored_wheel(ROOT).data
    wheelhouse = tmp_path / 'wheels'
    wheelhouse.mkdir()
    (wheelhouse / sdk_wheel.FILENAME).write_bytes(data)
    policy = wheels.file_lock(document, wheelhouse)
    (tmp_path / wheels.FILE_LOCK).write_bytes(runtime_lock.canonical_bytes(policy))
    files = wheels.payload_files(document, wheelhouse)
    hashes = {sdk_wheel.NAME: sdk_wheel.SDK_HASH}
    return document, hashes, policy, files


def test_independent_projection_matches_only_verified_wheel(projection, tmp_path):
    """Exact hashes, metadata, data and original licence are all included."""
    document, hashes, _, files = projection
    assert wheels.locked_files(tmp_path, document, hashes) == wheels.file_hashes(files)
    wheels.verify_file_hashes(files, wheels.locked_files(tmp_path, document, hashes))
    files['t_tech/invest/__init__.py'] += b'# changed'
    with pytest.raises(wheels.PackageError, match='^runtime file content drift$'):
        wheels.verify_file_hashes(files, wheels.locked_files(tmp_path, document, hashes))


@pytest.mark.parametrize('damage', [
        'schema', 'digest', 'closure', 'name', 'type', 'hash', 'size', 'path', 'selected',
])
def test_file_lock_drift_rejected(projection, tmp_path, damage):
    """A projection cannot be silently moved to another graph, artifact or unsafe path."""
    document, hashes, policy, _ = projection
    policy = deepcopy(policy)
    entry = policy['artifacts'][sdk_wheel.SDK_HASH]
    filename = next(iter(entry['files']))
    if damage == 'schema':
        policy['schema_version'] = True
    elif damage == 'digest':
        policy['lock_sha256'] = '0' * 64
    elif damage == 'closure':
        policy['artifacts'].clear()
    elif damage == 'name':
        entry['name'] = 'other'
    elif damage == 'type':
        entry['files'] = []
    elif damage == 'hash':
        entry['files'][filename]['sha256'] = 'invalid'
    elif damage == 'size':
        entry['files'][filename]['size'] = True
    elif damage == 'path':
        entry['files']['../outside'] = entry['files'].pop(filename)
    else:
        hashes[sdk_wheel.NAME] = '0' * 64
    path = tmp_path / wheels.FILE_LOCK
    path.write_bytes(runtime_lock.canonical_bytes(policy))
    with pytest.raises((wheels.PackageError, runtime_lock.LockError)):
        wheels.locked_files(tmp_path, document, hashes)


def test_file_lock_symlink_rejected(projection, tmp_path):
    """Independent trust policy cannot be redirected through another file."""
    document, hashes, policy, _ = projection
    target = tmp_path / 'policy.json'
    target.write_bytes(runtime_lock.canonical_bytes(policy))
    path = tmp_path / wheels.FILE_LOCK
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(runtime_lock.LockError):
        wheels.locked_files(tmp_path, document, hashes)


@pytest.mark.parametrize('damage', ['code', 'data', 'license', 'missing', 'extra'])
def test_receipt_real_distribution_same_version_replacement(
        projection, tmp_path, monkeypatch, damage):
    # pylint: disable=too-many-locals
    """Distribution metadata stays identical while real installed files or membership change."""
    document, _, _, files = projection
    site = tmp_path / 'lib'
    for name, data in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    distribution = installer.importlib.metadata.Distribution.at(site / sdk_wheel.DIST_INFO)
    port = create_autospec(installer.importlib.metadata.distributions, spec_set=True,
                           return_value=[distribution])
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', port)
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    installed, members, payload = installer.runtime_snapshot(document)
    assert payload == files
    facts = installer.receipt(document, 'vendored', {sdk_wheel.NAME: sdk_wheel.SDK_HASH})
    facts.update(schema_version=2, installed_files=wheels.file_hashes(installed),
                 installed_members=members)
    path = tmp_path / installer.RECEIPT
    installer.write_receipt(path, facts)
    installer.check_receipt(path, document)
    assert distribution.version == '1.51.0'
    original_requires = distribution.requires
    target = site / {
        'code': 't_tech/invest/__init__.py', 'data': 't_tech/invest/schemas.py',
        'license': sdk_wheel.LICENSE_PATH, 'missing': sdk_wheel.LICENSE_PATH,
        'extra': sdk_wheel.DIST_INFO + 'RECORD',
    }[damage]
    if damage == 'missing':
        target.unlink()
    elif damage == 'extra':
        target.write_bytes(target.read_bytes() + b't_tech/extra.py,,\n')
        (site / 't_tech/extra.py').write_bytes(b'extra source')
    else:
        target.write_bytes(b'replacement bytes')
    assert distribution.version == '1.51.0' and distribution.requires == original_requires
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


@pytest.mark.parametrize('forged_expected', [False, True])
def test_attestation_requires_both_owned_bytes_and_independent_lock(
        projection, tmp_path, monkeypatch, forged_expected):
    """A successful receipt requires both transition evidence and reviewed file identity."""
    document, hashes, _, payload = projection
    files = {'lib/' + name: data for name, data in payload.items()}
    members = {sdk_wheel.NAME: sorted(files)}
    snapshot = create_autospec(installer.runtime_snapshot, spec_set=True,
                               return_value=(files, members, payload))
    monkeypatch.setattr(installer, 'runtime_snapshot', snapshot)
    facts = installer.receipt(document, 'vendored', hashes)
    success = installer.attest_environment(tmp_path, document, facts, dict(payload))
    assert success['schema_version'] == 2 and success['installed_members'] == members
    assert success['installed_files'] == wheels.file_hashes(files)
    expected = dict(payload)
    payload['t_tech/invest/__init__.py'] += b'changed source'
    if forged_expected:
        expected = dict(payload)
    with pytest.raises(wheels.PackageError, match='^runtime file content drift$'):
        installer.attest_environment(tmp_path, document, facts, expected)


@pytest.mark.parametrize('damage', [
    'schema', 'float-schema', 'members', 'extra-field', 'file-shape', 'size', 'hash',
])
def test_receipt_file_evidence_fails_closed(projection, tmp_path, damage):
    """Malformed file facts or membership do not turn a schema2 receipt into approval."""
    document, hashes, _, _ = projection
    facts = installer.receipt(document, 'vendored', hashes)
    (tmp_path / 'sdk.py').write_bytes(b'original')
    facts.update(schema_version=2, installed_files=wheels.file_hashes({'sdk.py': b'original'}),
                 installed_members={sdk_wheel.NAME: ['sdk.py']})
    if damage == 'schema':
        facts['schema_version'] = True
    elif damage == 'float-schema':
        facts['schema_version'] = 2.0
    elif damage == 'members':
        facts['installed_members'][sdk_wheel.NAME] = []
    elif damage == 'extra-field':
        facts['unverified'] = True
    elif damage == 'file-shape':
        facts['installed_files']['sdk.py'] = {}
    else:
        facts['installed_files']['sdk.py'][damage] = True if damage == 'size' else 'invalid'
    path = tmp_path / installer.RECEIPT
    installer.write_receipt(path, facts)
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


def test_old_artifact_only_receipt_requires_verified_setup(projection, tmp_path):
    """A previous schema has no installed byte evidence and cannot authorize checks."""
    document, hashes, _, _ = projection
    path = tmp_path / installer.RECEIPT
    installer.write_receipt(path, installer.receipt(document, 'vendored', hashes))
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


@pytest.mark.parametrize('report', [
    'bad JSON', '{}', '{"installed":[{"metadata":{}}]}',
    '{"installed":[{"metadata":{"name":"legacy","version":"1.0 @ /foreign"}}]}',
])
def test_invalid_preflight_metadata_precedes_solver(tmp_path, monkeypatch, report):
    """Invalid installed facts never reach an installer invocation."""
    port = create_autospec(installer.pip, spec_set=True, return_value=report)
    monkeypatch.setattr(installer, 'pip', port)
    with pytest.raises((installer.InstallError, runtime_lock.LockError)):
        installer.preflight(tmp_path, tmp_path, installer.Budget(30), [])
    port.assert_called_once()


def test_explicit_file_lock_generation_only_acquires_verified_bytes(
        projection, tmp_path, monkeypatch):
    """Generation uses normal acquisition policy and owned wheels, with no installation effects."""
    document, _, expected, _ = projection
    selected = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.TIMEOUT,
        sdk_wheel.load_vendored_wheel(ROOT), None)
    monkeypatch.setattr(runtime_lock, 'check_host', create_autospec(
        runtime_lock.check_host, spec_set=True))
    monkeypatch.setattr(runtime_lock, 'load_lock', create_autospec(
        runtime_lock.load_lock, spec_set=True, return_value=document))
    acquire = create_autospec(sdk_wheel.acquire_sdk, spec_set=True, return_value=selected)
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', acquire)

    def prepare(_root, _document, wheelhouse, budget, sdk):
        assert sdk is selected and 0 < budget.remaining() <= 300
        (wheelhouse / sdk_wheel.FILENAME).write_bytes(sdk.wheel.data)

    monkeypatch.setattr(installer, 'prepare_wheels', create_autospec(
        installer.prepare_wheels, spec_set=True, side_effect=prepare))
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    assert installer.update_file_lock(tmp_path) == expected
    acquire.assert_called_once_with(tmp_path)
    pip.assert_not_called()


def test_explicit_file_lock_cli_writes_projection_without_receipt(
        projection, tmp_path, monkeypatch, capsys):
    """The explicit metadata output is never described as an installed environment."""
    _, _, policy, _ = projection
    update = create_autospec(installer.update_file_lock, spec_set=True, return_value=policy)
    install = create_autospec(installer.install, spec_set=True)
    monkeypatch.setattr(installer, 'update_file_lock', update)
    monkeypatch.setattr(installer, 'install', install)
    assert installer.main(['--project-root', str(tmp_path), '--write-file-lock']) == 0
    assert json.loads((tmp_path / wheels.FILE_LOCK).read_bytes()) == policy
    assert not (tmp_path / installer.RECEIPT).exists()
    install.assert_not_called()
    assert 'verified runtime file lock written' in capsys.readouterr().out


@pytest.mark.parametrize('damage', ['symlink', 'foreign', 'missing-members', 'duplicate'])
def test_runtime_snapshot_fails_closed(projection, tmp_path, monkeypatch, damage):
    """Installed RECORD cannot point at foreign or redirected files."""
    document, _, _, _ = projection
    distribution = create_autospec(installer.importlib.metadata.Distribution,
                                   instance=True, spec_set=True)
    distribution.metadata = {'Name': sdk_wheel.NAME}
    distribution.files = [installer.importlib.metadata.PackagePath('sdk.py')]
    original = tmp_path / 'original.py'
    original.write_bytes(b'original')
    path = tmp_path / 'sdk.py'
    path.write_bytes(b'source')
    if damage == 'symlink':
        path.unlink()
        path.symlink_to(original)
    elif damage == 'foreign':
        monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path / 'other'))
    elif damage == 'missing-members':
        distribution.files = None
    distribution.locate_file.return_value = path
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', create_autospec(
        installer.importlib.metadata.distributions, spec_set=True,
        return_value=[distribution, distribution] if damage == 'duplicate' else [distribution]))
    if damage != 'foreign':
        monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    with pytest.raises(installer.InstallError, match='installed file'):
        installer.runtime_snapshot(document)


def test_projection_generation_revalidates_each_transition(projection, tmp_path, monkeypatch):
    """Each payload projection expands its immutable wheel only once per validation."""
    document, _, _, _ = projection
    original = wheels._contents  # pylint: disable=protected-access
    port = create_autospec(original, spec_set=True, wraps=original)
    port.side_effect = original
    monkeypatch.setattr(wheels, '_contents', port)
    wheels.payload_files(document, tmp_path / 'wheels')
    assert port.call_count == 1


@pytest.fixture(name='installed_sdk')
def fixture_installed_sdk(projection, tmp_path, monkeypatch):
    """A real namespace SDK installation and receipt, discovered outside the checkout."""
    document, hashes, _, payload = projection
    site = tmp_path / 'lib/python3.12/site-packages'
    for name, data in payload.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    distribution = installer.importlib.metadata.Distribution.at(site / sdk_wheel.DIST_INFO)
    distributions = [distribution]
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', create_autospec(
        installer.importlib.metadata.distributions, spec_set=True,
        side_effect=lambda: iter(distributions)))
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(runtime_lock, 'load_lock', create_autospec(
        runtime_lock.load_lock, spec_set=True, return_value=document))
    monkeypatch.setattr(runtime_lock, 'check_installed_graph', create_autospec(
        runtime_lock.check_installed_graph, spec_set=True))
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    facts = installer.attest_environment(
        tmp_path, document, installer.receipt(document, 'vendored', hashes), payload)
    installer.write_receipt(tmp_path / installer.RECEIPT, facts)
    installer.check_environment(tmp_path)
    pip.reset_mock()
    return site, distributions, (site / sdk_wheel.DIST_INFO / 'RECORD').read_bytes(), pip


@pytest.mark.parametrize('name', [
    't_tech/__init__.py', 't_tech/invest/services/__init__.py',
    't_tech/invest/unknown.py', 't_tech/unknown/__init__.py',
    't_tech/invest/services.pyc',
    't_tech/invest/__pycache__/unknown.cpython-312.pyc',
    't_tech/invest/__pycache__/services.cpython-311.pyc',
    't_tech/invest/__pycache__/injected.py',
    't_tech/invest/extra.dat', sdk_wheel.DIST_INFO + 'unrecorded.py',
    't_tech.py', 't_tech.pyc',
])
def test_unrecorded_installed_additions_fail_guard(installed_sdk, tmp_path, name):
    """New import roots/modules/data cannot hide behind an unchanged RECORD and receipt."""
    site, _, record, pip = installed_sdk
    path = site / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'unverified executable or data')
    assert (site / sdk_wheel.DIST_INFO / 'RECORD').read_bytes() == record
    if name in ('t_tech/__init__.py', 't_tech/invest/services/__init__.py'):
        search = site if name == 't_tech/__init__.py' else site / 't_tech/invest'
        module = 't_tech' if search == site else 'services'
        spec = importlib.machinery.PathFinder.find_spec(module, [str(search)])
        assert spec.origin == str(path)
    with pytest.raises(installer.InstallError, match='installed file'):
        installer.check_environment(tmp_path)
    pip.assert_not_called()


@pytest.mark.parametrize('name', ['t_tech/foreign', 't_tech/invest/__pycache__'])
def test_unrecorded_symlink_directory_is_not_traversed(installed_sdk, tmp_path, name):
    """Even a cache or an empty unrecorded directory cannot redirect directory traversal."""
    site, _, _, pip = installed_sdk
    outside = tmp_path / 'outside'
    outside.mkdir()
    (site / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(installer.InstallError, match='installed file'):
        installer.check_environment(tmp_path)
    pip.assert_not_called()


def test_generated_caches_match_their_runtime_source(installed_sdk, tmp_path):
    """Normal interpreter caches remain allowed without trusting unrelated .pyc names."""
    site, _, _, pip = installed_sdk
    source = site / 't_tech/invest/services.py'
    for optimization in ('', '1', '2'):
        cache = Path(importlib.util.cache_from_source(str(source), optimization=optimization))
        cache.parent.mkdir(exist_ok=True)
        py_compile.compile(str(source), cfile=str(cache), optimize=int(optimization or 0),
                           doraise=True)
    installer.check_environment(tmp_path)
    assert pip.call_count == 1


@pytest.mark.parametrize('operation', ['guard', 'attest'])
def test_timestamp_preserving_cache_replacement_fails_guard(
        installed_sdk, projection, tmp_path, operation):
    # pylint: disable=too-many-locals
    """Real CPython executes a substituted cache despite unchanged source/RECORD/receipt."""
    site, _, record, pip = installed_sdk
    source = site / 't_tech/invest/services.py'
    original = source.read_bytes()
    receipt_path = tmp_path / installer.RECEIPT
    receipt = receipt_path.read_bytes()
    cache = Path(py_compile.compile(str(source), doraise=True))
    header = cache.read_bytes()[:16]
    assert header[4:8] == b'\0' * 4
    replacement = compile(b'value = "replacement executed"\n', str(source), 'exec',
                          dont_inherit=True)
    cache.write_bytes(header + marshal.dumps(replacement))
    # This isolated demonstration executes only the harmless fixture, outside validation.
    result = subprocess.run([sys.executable, '-S', '-c',
                             'import importlib.machinery, types, sys; '
                             'm = types.ModuleType("cache_probe"); '
                             'importlib.machinery.SourceFileLoader(m.__name__, sys.argv[1])'
                             '.exec_module(m); print(m.value)', str(source)],
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == 'replacement executed'
    assert source.read_bytes() == original
    assert (site / sdk_wheel.DIST_INFO / 'RECORD').read_bytes() == record
    assert receipt_path.read_bytes() == receipt
    with pytest.raises(installer.InstallError, match='^runtime cache content drift$'):
        if operation == 'guard':
            installer.check_environment(tmp_path)
        else:
            document, hashes, _, payload = projection
            installer.attest_environment(tmp_path, document,
                                         installer.receipt(document, 'vendored', hashes), payload)
    assert receipt_path.read_bytes() == receipt
    pip.assert_not_called()


@pytest.mark.parametrize('optimization', [0, 1, 2])
def test_exact_import_produced_cache_bytes(tmp_path, monkeypatch, optimization):
    """Native imports handle encoding/newlines, future flags and nested code at all opt levels."""
    source = tmp_path / 'native.py'
    data = (b'# coding: latin-1\r\nfrom __future__ import annotations\r\n'
            b'"""caf\xe9"""\r\ndef outer(x: Unknown):\r\n'
            b'    """nested doc"""\r\n    assert x\r\n'
            b'    def inner(): return x, 1, 1.0, True, "caf\xe9"\r\n'
            b'    return inner\r\n')
    source.write_bytes(data)
    command = [sys.executable, '-I', '-S',
               *(['-' + 'O' * optimization] if optimization else []),
               '-c', 'import importlib.machinery, types, sys; '
               'm = types.ModuleType("native"); '
               'importlib.machinery.SourceFileLoader(m.__name__, sys.argv[1]).exec_module(m)',
               str(source)]
    result = subprocess.run(command, capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    cache = Path(importlib.util.cache_from_source(
        str(source), optimization=str(optimization or '')))
    original = cache.read_bytes()
    for operation in ('load', 'loads'):
        port = create_autospec(getattr(marshal, operation), spec_set=True,
                               side_effect=AssertionError('untrusted deserialization'))
        monkeypatch.setattr(marshal, operation, port)
    installer._check_runtime_caches(tmp_path, {'native.py': data})  # pylint: disable=protected-access
    assert cache.read_bytes() == original
    marshal.load.assert_not_called()
    marshal.loads.assert_not_called()


@pytest.mark.parametrize('mode', list(py_compile.PycInvalidationMode))
@pytest.mark.parametrize('optimization', [0, 1, 2])
def test_standard_cache_invalidation_modes(tmp_path, mode, optimization):
    """Timestamp, checked-hash and unchecked-hash caches all require authentic executable bytes."""
    source = tmp_path / 'module.py'
    data = b'"""module doc"""\ndef f():\n    assert True\n    return 1\n'
    source.write_bytes(data)
    py_compile.compile(str(source), optimize=optimization, invalidation_mode=mode,
                       doraise=True)
    installer._check_runtime_caches(tmp_path, {'module.py': data})  # pylint: disable=protected-access


@pytest.mark.parametrize('damage', [
    'magic', 'flags', 'timestamp', 'size', 'checked-hash', 'unchecked-hash',
    'truncated', 'trailing', 'non-code', 'constant-type', 'nested-filename', 'filename',
    'optimization',
])
def test_cache_header_and_executable_bytes_are_not_trust(tmp_path, damage):
    """Opaque bytes bind constants, nested filenames and tail bytes to trusted compilation."""
    source = tmp_path / 'module.py'
    data = b'def f():\n    return 1\n'
    source.write_bytes(data)
    mode = (py_compile.PycInvalidationMode.CHECKED_HASH if damage == 'checked-hash'
            else py_compile.PycInvalidationMode.UNCHECKED_HASH if damage == 'unchecked-hash'
            else py_compile.PycInvalidationMode.TIMESTAMP)
    cache = Path(py_compile.compile(str(source), invalidation_mode=mode,
                                    optimize=1 if damage == 'optimization' else 0, doraise=True))
    original = cache.read_bytes()
    replacement = bytearray(original)
    offsets = {'magic': 0, 'flags': 4, 'timestamp': 8, 'size': 12,
               'checked-hash': 8, 'unchecked-hash': 8}
    if damage in offsets:
        replacement[offsets[damage]] ^= 0x80
    elif damage == 'truncated':
        replacement = replacement[:10]
    elif damage == 'trailing':
        replacement += b'\0'
    elif damage == 'non-code':
        replacement = original[:16] + marshal.dumps(None)
    else:
        code = compile(data, str(source), 'exec', dont_inherit=True)
        if damage == 'optimization':
            # opt-0 and opt-1 differ in their assertion code, even with the same source header.
            source.write_bytes(b'assert True\ndef f():\n    return 1\n')
            data = source.read_bytes()
            py_compile.compile(str(source), cfile=str(cache), optimize=1, doraise=True)
            original = cache.read_bytes()
            code = compile(data, str(source), 'exec', dont_inherit=True, optimize=0)
        elif damage == 'filename':
            code = code.replace(co_filename='other.py')
        else:
            nested = next(c for c in code.co_consts if isinstance(c, CodeType))
            nested = (nested.replace(co_filename='other.py') if damage == 'nested-filename'
                      else nested.replace(co_consts=(None, True)))
            code = code.replace(co_consts=tuple(nested if isinstance(c, CodeType) else c
                                                for c in code.co_consts))
        replacement = original[:16] + marshal.dumps(code)
    cache.write_bytes(replacement)
    with pytest.raises(installer.InstallError,
                       match='^runtime cache (content drift|unavailable or invalid)$'):
        installer._check_runtime_caches(tmp_path, {'module.py': data})  # pylint: disable=protected-access


@pytest.mark.parametrize('damage', ['source-size', 'compiled-size', 'deadline', 'read', 'syntax'])
def test_cache_validation_limits_and_safe_errors(tmp_path, monkeypatch, damage):
    """Bound trusted compilation and opaque reads, with safe failures before imports/pip."""
    source = tmp_path / 'module.py'
    data = b'value = 1\n'
    source.write_bytes(data)
    py_compile.compile(str(source), doraise=True)
    if damage in ('source-size', 'compiled-size'):
        monkeypatch.setattr(installer, 'MAX_OUTPUT', len(data) - (damage == 'source-size'))
    elif damage == 'deadline':
        monkeypatch.setattr(installer.time, 'monotonic', create_autospec(
            installer.time.monotonic, spec_set=True, side_effect=[0, 31]))
    elif damage == 'syntax':
        data = b'invalid syntax !'
    else:
        monkeypatch.setattr(sdk_wheel, '_read_file', create_autospec(
            sdk_wheel._read_file, spec_set=True,  # pylint: disable=protected-access
            side_effect=OSError('private data')))
    with pytest.raises(installer.InstallError, match='^(runtime cache|setup deadline)') as error:
        installer._check_runtime_caches(tmp_path, {'module.py': data})  # pylint: disable=protected-access
    assert 'private data' not in str(error.value)


@pytest.mark.parametrize('forged_receipt', [False, True])
def test_source_authentication_precedes_cache_compilation(
        installed_sdk, tmp_path, monkeypatch, forged_receipt):
    """An attacker cannot make the guard compile replaced source by also changing a cache."""
    site, _, _, pip = installed_sdk
    source = site / 't_tech/invest/services.py'
    py_compile.compile(str(source), doraise=True)
    source.write_bytes(b'replacement = True\n')
    if forged_receipt:
        path = tmp_path / installer.RECEIPT
        facts = json.loads(path.read_bytes())
        facts['installed_files'].update(wheels.file_hashes({
            source.relative_to(tmp_path).as_posix(): source.read_bytes()}))
        path.write_bytes(runtime_lock.canonical_bytes(facts))
    compiler = create_autospec(compile, spec_set=True, side_effect=AssertionError('compile'))
    monkeypatch.setattr(installer, 'compile', compiler, raising=False)
    error = wheels.PackageError if forged_receipt else installer.InstallError
    reason = ('^runtime file content drift$' if forged_receipt
              else '^runtime receipt missing or stale; run verified setup$')
    with pytest.raises(error, match=reason):
        installer.check_environment(tmp_path)
    compiler.assert_not_called()
    pip.assert_not_called()


@pytest.mark.parametrize('mode', ['dev-namespace', 'unrecorded', 'root-init', 'sdk-extension'])
def test_development_namespace_ownership(tmp_path, monkeypatch, mode):
    # pylint: disable=too-many-locals
    """A separate recorded dev namespace branch is valid; SDK extensions remain sealed."""
    site = tmp_path / 'lib'
    runtime = 't_tech' if mode == 'sdk-extension' else 'google'
    runtime_files = {runtime + '/runtime/__init__.py': b'original',
                     'runtime-1.0.dist-info/METADATA': b'Name: runtime\nVersion: 1.0\n'}
    for name, data in runtime_files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    record_name = 'runtime-1.0.dist-info/RECORD'
    runtime_files[record_name] = (
        ''.join(name + ',,\n' for name in runtime_files) + record_name + ',,\n').encode()
    (site / record_name).write_bytes(runtime_files[record_name])
    distribution = installer.importlib.metadata.Distribution.at(site / 'runtime-1.0.dist-info')
    dev_name = runtime + ('/__init__.py' if mode == 'root-init' else '/development/__init__.py')
    dev_path = site / dev_name
    dev_path.parent.mkdir(parents=True, exist_ok=True)
    dev_path.write_bytes(b'development package')
    dev_info = site / 'dev-1.0.dist-info'
    dev_info.mkdir()
    (dev_info / 'METADATA').write_text('Name: dev\nVersion: 1.0\n', encoding='ascii')
    (dev_info / 'RECORD').write_text(
        'dev-1.0.dist-info/METADATA,,\n' + ('' if mode == 'unrecorded' else dev_name + ',,\n'),
        encoding='ascii')
    dev = installer.importlib.metadata.Distribution.at(dev_info)
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', create_autospec(
        installer.importlib.metadata.distributions, spec_set=True,
        return_value=[distribution, dev]))
    document = {'packages': [{'name': 'runtime'}]}
    if mode == 'dev-namespace':
        cache = Path(importlib.util.cache_from_source(str(dev_path)))
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(b'generated dev cache')
        _, _, payload = installer.runtime_snapshot(document)
        assert payload == runtime_files
    else:
        with pytest.raises(installer.InstallError, match='installed file'):
            installer.runtime_snapshot(document)


def test_file_lock_projects_distinct_same_filename_artifacts(tmp_path, monkeypatch):
    # pylint: disable=too-many-locals
    """Test-only validated approval pair must produce both exact projections without overwrite."""
    reserve_data, reserve_policy = wheel()
    index_data, index_policy = wheel({'t_tech/invest/test_variant.py': b'approved fixture\n'})
    approval = sdk_wheel.HashApproval(
        '1.51.0', reserve_policy.sha256, index_policy.sha256, 'fixture',
        '2026-10-13T00:00:00Z', 'https://github.com/example/fixture/issues/1')
    reserve_policy = replace(reserve_policy, approvals=(approval,))
    reserve = sdk_wheel.validate_wheel(reserve_data, reserve_policy, licence())
    official = sdk_wheel.validate_wheel(index_data, index_policy, licence())
    sdk_wheel.check_approval(reserve_policy, index_policy.sha256)
    original = next(p for p in runtime_lock.load_lock(ROOT)['packages']
                    if p['name'] == sdk_wheel.NAME)
    artifacts = [dict(original['artifacts'][0], sha256=p.sha256, size=p.size)
                 for p in (reserve_policy, index_policy)]
    artifacts[1]['url'] = 'tbank:sha256:' + index_policy.sha256
    document = {'packages': [{**original, 'requires_dist': [], 'artifacts': artifacts}]}
    selected = sdk_wheel.Selection(
        sdk_wheel.Source.OFFICIAL, sdk_wheel.Reason.OFFICIAL_AVAILABLE, official,
        index_policy.sha256)
    monkeypatch.setattr(runtime_lock, 'load_lock', create_autospec(
        runtime_lock.load_lock, spec_set=True, return_value=document))
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', create_autospec(
        sdk_wheel.acquire_sdk, spec_set=True, return_value=selected))
    monkeypatch.setattr(sdk_wheel, 'load_vendored_wheel', create_autospec(
        sdk_wheel.load_vendored_wheel, spec_set=True, return_value=reserve))
    # Current generator's preparation is real for the SDK and has no network dependencies.
    monkeypatch.setattr(installer, 'validate_inventory', create_autospec(
        installer.validate_inventory, spec_set=True, return_value=[]))
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor/inventory.json').write_text('{}', encoding='ascii')
    result = installer.update_file_lock(tmp_path)
    assert set(result['artifacts']) == {reserve_policy.sha256, index_policy.sha256}
    for policy, data in ((reserve_policy, reserve_data), (index_policy, index_data)):
        with ZipFile(BytesIO(data)) as archive:
            expected = {name: archive.read(name) for name in archive.namelist()}
        assert result['artifacts'][policy.sha256]['files'] == wheels.file_hashes(expected)


@pytest.mark.parametrize('name', [
    '__pycache__/module.cpython-311.pyc', '__pycache__/module.py', 'module/__init__.py',
])
def test_top_level_module_cache_and_dev_shadowing_fail(tmp_path, monkeypatch, name):
    """Foreign RECORD and cache directories cannot replace a verified top-level module."""
    site = tmp_path / 'lib'
    site.mkdir()
    (site / 'module.py').write_bytes(b'original')
    distribution = create_autospec(installer.importlib.metadata.Distribution,
                                   instance=True, spec_set=True)
    distribution.metadata = {'Name': 'runtime'}
    distribution.files = [installer.importlib.metadata.PackagePath('module.py')]
    distribution.locate_file.side_effect = lambda p: site / p
    dev = create_autospec(installer.importlib.metadata.Distribution, instance=True, spec_set=True)
    dev.metadata = {'Name': 'dev'}
    dev.files = [installer.importlib.metadata.PackagePath(name)]
    dev.locate_file.side_effect = lambda p: site / p
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', create_autospec(
        installer.importlib.metadata.distributions, spec_set=True,
        return_value=[distribution, dev]))
    path = site / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'unverified')
    with pytest.raises(installer.InstallError, match='installed file'):
        installer.runtime_snapshot({'packages': [{'name': 'runtime'}]})


@pytest.mark.parametrize('damage', ['file-symlink', 'depth', 'count', 'directory-count'])
def test_actual_directory_enumeration_bounds_and_types(
        installed_sdk, tmp_path, monkeypatch, damage):
    """Unrecorded directories are bounded, and even unrecorded file links cannot redirect reads."""
    site, _, _, pip = installed_sdk
    if damage == 'file-symlink':
        (site / 't_tech/unrecorded.py').symlink_to(site / 't_tech/invest/services.py')
    elif damage == 'depth':
        (site / 't_tech' / Path(*(['nested'] * 65))).mkdir(parents=True)
    elif damage == 'directory-count':
        for index in range(250):
            (site / 't_tech' / f'empty{index}').mkdir()
        monkeypatch.setattr(installer, 'MAX_INSTALLED_ENTRIES', 200)
    else:
        monkeypatch.setattr(installer, 'MAX_INSTALLED_ENTRIES', 5)
    with pytest.raises(installer.InstallError, match='installed file'):
        installer.check_environment(tmp_path)
    pip.assert_not_called()


@pytest.mark.parametrize('damage', ['symlink', 'parent'])
def test_enumeration_descriptor_rejects_redirected_roots(tmp_path, damage):
    """Directory opening cannot follow a root link or traverse parent components."""
    path = tmp_path / 'directory'
    path.mkdir()
    if damage == 'symlink':
        link = tmp_path / 'redirect'
        link.symlink_to(path, target_is_directory=True)
        path = link
    else:
        path = path / '..'
    with pytest.raises((OSError, installer.InstallError)):
        installer._directory_descriptor(path)  # pylint: disable=protected-access


@pytest.mark.parametrize('mode', ['other-root', 'entrypoint', 'excessive'])
def test_development_records_cannot_expand_runtime_enumeration(
        installed_sdk, tmp_path, monkeypatch, mode):
    """Separate development installs/entrypoints are preserved; oversized records fail closed."""
    site, distributions, _, pip = installed_sdk
    dev = create_autospec(installer.importlib.metadata.Distribution, instance=True, spec_set=True)
    dev.metadata = {'Name': 'dev'}
    dev_root = tmp_path / 'other' if mode == 'other-root' else site
    dev.locate_file.side_effect = lambda p: dev_root / p
    dev.files = [installer.importlib.metadata.PackagePath('../../../bin/development-tool')]
    distributions.append(dev)
    if mode == 'excessive':
        dev.files *= 201
        monkeypatch.setattr(installer, 'MAX_INSTALLED_ENTRIES', 200)
        with pytest.raises(installer.InstallError, match='installed file enumeration limits'):
            installer.check_environment(tmp_path)
        pip.assert_not_called()
    else:
        installer.check_environment(tmp_path)
        assert pip.call_count == 1


def local_wheel(name, version, requires=()):
    """Build test-only universal wheels with complete RECORD and explicit dependencies."""
    data, _ = wheel()
    result = BytesIO()
    old = sdk_wheel.DIST_INFO
    new = f'{name}-{version}.dist-info/'
    contents = {}
    with ZipFile(BytesIO(data)) as source, ZipFile(result, 'w') as target:
        for path in source.namelist():
            if path == old + 'RECORD':
                continue
            payload = source.read(path)
            path = path.replace(old, new).replace('t_tech/invest/', name + '/')
            if path == new + 'METADATA':
                payload = (f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'
                           + ''.join('Requires-Dist: ' + r + '\n' for r in requires)
                           + '\n').encode()
            target.writestr(path, payload)
            contents[path] = payload
        output = StringIO(newline='')
        writer = csv.writer(output, lineterminator='\n')
        for path, payload in contents.items():
            digest = base64.urlsafe_b64encode(sha256(payload).digest()).decode().rstrip('=')
            writer.writerow((path, 'sha256=' + digest, str(len(payload))))
        writer.writerow((new + 'RECORD', '', ''))
        target.writestr(new + 'RECORD', output.getvalue())
    return result.getvalue()


def test_actual_pip_rejects_proposed_reverse_dependency_conflict(tmp_path, monkeypatch):
    # pylint: disable=too-many-locals
    """Real offline pip accepts the old graph and rejects locked upgrades before effects."""
    environment = tmp_path / 'venv'
    result = subprocess.run([sys.executable, '-m', 'venv', str(environment)],
                            capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    python = str(environment / 'bin/python')
    wheelhouse = tmp_path / 'wheels'
    wheelhouse.mkdir()
    old = wheelhouse / 'requests-2.33.0-py3-none-any.whl'
    legacy = wheelhouse / 'legacy_client-1.0.0-py3-none-any.whl'
    old.write_bytes(local_wheel('requests', '2.33.0'))
    legacy.write_bytes(local_wheel('legacy_client', '1.0.0', ['requests<2.34']))
    command = [python, '-m', 'pip', '--isolated', '--disable-pip-version-check']
    installed = subprocess.run([*command, 'install', '--no-index', '--no-deps',
                                str(old), str(legacy)],
                               capture_output=True, timeout=30, check=False)
    assert installed.returncode == 0, installed.stderr
    checked = subprocess.run([*command, 'check'], capture_output=True, timeout=30, check=False)
    assert checked.returncode == 0, checked.stderr
    old.unlink()
    legacy.unlink()
    proposed = wheelhouse / 'requests-2.34.2-py3-none-any.whl'
    proposed.write_bytes(local_wheel('requests', '2.34.2'))

    def pip(arguments, root, budget):
        result = subprocess.run([*command, *arguments], cwd=root,
                                env=runtime_lock.pip_environment(), capture_output=True,
                                text=True, timeout=min(30, budget.remaining()), check=False)
        if result.returncode:
            raise installer.InstallError('proposed graph conflict')
        return result.stdout

    monkeypatch.setattr(installer, 'pip', create_autospec(
        installer.pip, spec_set=True, side_effect=pip))
    with pytest.raises(installer.InstallError, match='^proposed graph conflict$'):
        installer.preflight(tmp_path, wheelhouse, installer.Budget(30), [])
    inspected = subprocess.run([*command, 'inspect', '--local'], capture_output=True,
                               text=True, timeout=30, check=False)
    report = json.loads(inspected.stdout)
    requests = next(item for item in report['installed'] if item['metadata']['name'] == 'requests')
    assert requests['metadata']['version'] == '2.33.0'
