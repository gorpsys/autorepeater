"""Canonical runtime exports, policy and installed graph contracts."""
import copy
import json
import subprocess
from unittest.mock import create_autospec
from pathlib import Path

import pytest

from scripts import runtime_lock as lock


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name='document')
def fixture_document():
    """Use the checked-in resolved graph, not installed development packages."""
    return json.loads((ROOT / 'runtime-lock.json').read_text(encoding='utf-8'))


def test_repository_exports_match_lock(document):
    """All public pip inputs have one source and no TLS bypass."""
    validated = lock.validate_lock(document, ROOT)
    for filename, text in lock.render_requirements(validated).items():
        assert (ROOT / filename).read_text(encoding='utf-8') == text
        assert '--trusted-host' not in text
    assert '--hash=' not in lock.render_requirements(validated)['requirements-dev.txt']
    assert '--hash=' not in lock.render_requirements(validated)['requirements-runtime-pins.txt']


def test_digest_ignores_object_order_but_covers_graph(document):
    """Digest is independent of JSON whitespace/object order, sensitive to facts."""
    assert lock.canonical_digest(document) == lock.canonical_digest(
        dict(reversed(list(document.items()))))
    changed = copy.deepcopy(document)
    changed['packages'][0]['version'] = '999.0'
    assert lock.canonical_digest(changed) != lock.canonical_digest(document)


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(unexpected=True),
    lambda d: d['target'].update(python='3.11'),
    lambda d: d['packages'].pop(),
    lambda d: d['packages'].append(copy.deepcopy(d['packages'][0])),
    lambda d: d['packages'][0].update(version='999'),
    lambda d: d['packages'][0]['artifacts'][0].update(sha256='0' * 64),
    lambda d: d['packages'][0]['artifacts'][0].update(url='https://evil.test/a.whl'),
    lambda d: d['packages'][0]['artifacts'][0].update(filename='../bad.whl'),
    lambda d: d['packages'][0].update(requires=['unknown']),
    lambda d: d['packages'][0].update(requires_dist=[1]),
    lambda d: d.update(report_sha256='bad'),
    lambda d: d['sources'].update(pypi='http://pypi.org/simple'),
])
def test_schema_and_inventory_drift_fail(document, mutation):
    """Bad input cannot silently change target, closure, sources or artifact pins."""
    mutation(document)
    with pytest.raises(lock.LockError):
        lock.validate_lock(document, ROOT)


def installed(document):
    """Explicit pip inspect facts for every runtime distribution."""
    return {'version': '1', 'environment': dict(lock.MARKER_ENVIRONMENT), 'installed': [
        {'metadata': {'name': p['name'], 'version': p['version'],
                      'requires_dist': p['requires_dist']}}
        for p in document['packages']]}


def test_dev_and_cloud_profiles(document):
    """Dev/bootstrap extras are allowed only outside the clean payload profile."""
    facts = installed(document)
    facts['installed'].append({'metadata': {'name': 'pytest', 'version': '9.0'}})
    lock.check_installed_graph(document, facts, profile='development')
    with pytest.raises(lock.LockError, match='extra'):
        lock.check_installed_graph(document, facts, profile='cloud')
    facts['installed'].pop()
    lock.check_installed_graph(document, facts, profile='cloud')


@pytest.mark.parametrize('mutation', [
    lambda f: f['installed'].pop(),
    lambda f: f['installed'][0]['metadata'].update(version='999'),
    lambda f: f['installed'][0]['metadata'].update(requires_dist=['missing>=1']),
    lambda f: f['installed'].append(copy.deepcopy(f['installed'][0])),
    lambda f: f.update(version='2'),
    lambda f: f['environment'].update(python_version='3.11'),
    lambda f: f['environment'].update(platform_machine='aarch64'),
])
def test_installed_missing_version_metadata_and_report_drift(document, mutation):
    """The checker reports conflicts without mutating the interpreter."""
    facts = installed(document)
    mutation(facts)
    with pytest.raises(lock.LockError):
        lock.check_installed_graph(document, facts, profile='development')


def test_no_artifact_attestation_from_version(document):
    """Artifact checks require explicit verified facts; versions prove no source."""
    facts = installed(document)
    hashes = {p['name']: p['artifacts'][0]['sha256'] for p in document['packages']}
    lock.check_installed_graph(document, facts, profile='cloud', artifact_hashes=hashes)
    hashes['t-tech-investments'] = '0' * 64
    with pytest.raises(lock.LockError, match='artifact'):
        lock.check_installed_graph(document, facts, profile='cloud', artifact_hashes=hashes)


def test_sdk_unapproved_artifact(document):
    """No renderer or validator extends the accepted SDK hashes implicitly."""
    sdk = next(p for p in document['packages'] if p['name'] == 't-tech-investments')
    sdk['artifacts'].append(dict(sdk['artifacts'][0], sha256='1' * 64))
    with pytest.raises(lock.LockError):
        lock.validate_lock(document, ROOT)


@pytest.mark.parametrize('tag,expected', [
    ('invalid', False), ('py3-none-any', True), ('py2-none-any', False),
    ('cp312-cp312-manylinux_2_28_x86_64', False),
    ('cp310-abi3-manylinux_2_17_x86_64', True),
    ('cp313-abi3-manylinux_2_17_x86_64', False),
    ('cp312-cp312-manylinux2014_x86_64', True),
    ('cp312-abi3-manylinux_2_17_x86_64', True),
    ('cp312-none-manylinux_2_17_x86_64', True),
    ('cp311-cp311-manylinux_2_17_x86_64', False),
])
def test_cloud_abi_policy(tag, expected):
    """Newer host glibc/CPython tags cannot redefine the cloud ABI."""
    assert lock.compatible_tag(tag) is expected


@pytest.mark.parametrize('contents', ['{', '{"x":1,"x":2}'])
def test_json_invalid_or_duplicate(tmp_path, contents):
    """Parser errors do not become a best effort policy."""
    path = tmp_path / 'policy.json'
    path.write_text(contents, encoding='utf-8')
    with pytest.raises(lock.LockError):
        lock.read_json(path)


def test_json_symlink_missing_size_and_unicode(tmp_path):
    """Bounded JSON input rejects symlinks, oversized and non-UTF8 content."""
    path = tmp_path / 'policy.json'
    with pytest.raises(lock.LockError):
        lock.read_json(path)
    path.write_bytes(b'\xff')
    with pytest.raises(lock.LockError):
        lock.read_json(path)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(lock.LockError):
        lock.read_json(link)
    path.write_bytes(b' ' * (4 * 1024 * 1024 + 1))
    with pytest.raises(lock.LockError):
        lock.read_json(path)


@pytest.mark.parametrize('libc,version', [('musl', '1.2'), ('glibc', '2.16'), ('glibc', '')])
def test_host_rejects_unsupported_libc(monkeypatch, libc, version):
    """An incompatible build host fails before any pip/network call."""
    monkeypatch.setattr(lock.platform, 'libc_ver', lambda: (libc, version))
    with pytest.raises(lock.LockError, match='libc'):
        lock.check_host()


def test_host_python_platform_and_marker_profile(monkeypatch):
    """The marker interpreter is the actual CPython3.12 Linuxx86_64 target."""
    lock.check_host()
    monkeypatch.setattr(lock.sys, 'version_info', (3, 11))
    with pytest.raises(lock.LockError, match='profile'):
        lock.check_host()


def test_pip_configuration_override_is_removed(monkeypatch):
    """Managed pip cannot inherit a credential-bearing index or TLS bypass."""
    monkeypatch.setenv('PIP_INDEX_URL', 'https://private:secret@invalid.test/simple')
    monkeypatch.setenv('PIP_TRUSTED_HOST', 'invalid.test')
    environment = lock.pip_environment()
    assert 'PIP_INDEX_URL' not in environment and 'PIP_TRUSTED_HOST' not in environment
    assert environment['PIP_CONFIG_FILE'] == '/dev/null'


def test_inspect_and_pip_check_use_bounded_supported_commands(document, monkeypatch):
    """Installed checks validate facts and dependency constraints, without deleting extras."""
    run = create_autospec(subprocess.run, spec_set=True)
    run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(installed(document)), ''),
                       subprocess.CompletedProcess([], 0, 'No broken requirements found.', '')]
    monkeypatch.setattr(lock.subprocess, 'run', run)
    lock.inspect_interpreter(document, 'cloud')
    assert [c.args[0][-1] for c in run.call_args_list] == ['--local', 'check']
    assert all(c.kwargs['timeout'] == 60 for c in run.call_args_list)
    assert all('uninstall' not in c.args[0] for c in run.call_args_list)


@pytest.mark.parametrize('response', [
    subprocess.CompletedProcess([], 1, '', 'private conflict'),
    subprocess.CompletedProcess([], 0, '{', ''),
    subprocess.CompletedProcess([], 0, 'x' * (8 * 1024 * 1024 + 1), ''),
    subprocess.TimeoutExpired('private args', 60), OSError('private error'),
])
def test_inspect_conflict_and_failures_are_fatal(document, monkeypatch, response):
    """No false installed success or sensitive raw diagnostic output."""
    run = create_autospec(subprocess.run, spec_set=True)
    if isinstance(response, Exception):
        run.side_effect = response
    else:
        run.return_value = response
    monkeypatch.setattr(lock.subprocess, 'run', run)
    with pytest.raises(lock.LockError) as error:
        lock.inspect_interpreter(document, 'development')
    assert 'private' not in str(error.value)


def test_check_cli_exports_then_optional_installed(document, monkeypatch, capsys):
    """CLI reports canonical digest; installed checking is an explicit option."""
    inspect = create_autospec(lock.inspect_interpreter, spec_set=True)
    monkeypatch.setattr(lock, 'inspect_interpreter', inspect)
    assert lock.main(['--project-root', str(ROOT), '--installed', 'development']) == 0
    inspect.assert_called_once_with(document, 'development')
    assert lock.canonical_digest(document) in capsys.readouterr().out


@pytest.mark.parametrize('failure', [lock.LockError('safe drift'), OSError('private')])
def test_check_cli_failure(monkeypatch, capsys, failure):
    """CLI failures return nonzero and expose only policy-safe reasons."""
    load = create_autospec(lock.load_lock, spec_set=True)
    load.side_effect = failure
    monkeypatch.setattr(lock, 'load_lock', load)
    assert lock.main(['--project-root', str(ROOT)]) == 1
    assert 'private' not in capsys.readouterr().err


def test_digest_covers_report_metadata(document):
    """Requires-Dist cannot drift while retaining the confirmed resolver digest."""
    document['packages'][0]['requires_dist'].append('new>=1')
    document['packages'][0]['requires_dist'].sort()
    with pytest.raises(lock.LockError, match='digest drift'):
        lock.validate_lock(document, ROOT)


def test_invalid_profile_and_missing_artifact_facts(document):
    """Missing hashes never turn version equality into source attestation."""
    with pytest.raises(lock.LockError, match='profile'):
        lock.check_installed_graph(document, installed(document), profile='invalid')
    with pytest.raises(lock.LockError, match='artifact'):
        lock.check_installed_graph(document, installed(document), profile='cloud',
                                   artifact_hashes={})
