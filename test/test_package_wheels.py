"""Offline acquisition facts, integrity and per-package reserve policy."""
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from urllib.error import HTTPError, URLError
from unittest.mock import create_autospec
from test.test_sdk_wheel import wheel, DIST

import pytest

from scripts import package_wheels as wheels
from scripts.sdk_wheel import AvailabilityError, Reason, Response
from scripts.runtime_lock import load_lock


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name='artifact')
def artifact_fixture():
    """Use the real SDK bytes as an arbitrary wheel for generic validation."""
    data = (ROOT / 'vendor/t-tech-investments' /
            't_tech_investments-1.51.0-py3-none-any.whl').read_bytes()
    return wheels.Artifact('t-tech-investments', '1.51.0',
                           't_tech_investments-1.51.0-py3-none-any.whl',
                           hashlib.sha256(data).hexdigest(), len(data), 'py3-none-any',
                           'https://files.pythonhosted.org/packages/example/sdk.whl'), data


def test_pypi_first_owns_validated_bytes(artifact, tmp_path):
    """A available artifact never reads the reserve or changes its identity."""
    item, data = artifact
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.return_value = Response(item.url, 200, 'application/octet-stream', data)
    result = wheels.acquire(item, tmp_path, [], port, timeout=12)
    assert result == data
    port.fetch.assert_called_once_with(item, 12)


@pytest.mark.parametrize('reason', [
    Reason.DNS, Reason.NETWORK, Reason.TIMEOUT, Reason.HTTP_401, Reason.HTTP_403,
    Reason.HTTP_404, Reason.HTTP_429, Reason.HTTP_5XX])
def test_availability_without_reserve_fails(artifact, tmp_path, reason):
    """Excluded projects have no automatic source build or downgrade."""
    item, _ = artifact
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.side_effect = AvailabilityError(reason)
    with pytest.raises(wheels.PackageError, match='reserve unavailable'):
        wheels.acquire(item, tmp_path, [], port, timeout=12)


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500, 503, 599])
def test_status_is_typed_before_install(artifact, tmp_path, status):
    """HTTP availability is distinct from malformed bytes and install stderr."""
    item, _ = artifact
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.return_value = Response(item.url, status, '', b'')
    with pytest.raises(wheels.PackageError, match='reserve unavailable'):
        wheels.acquire(item, tmp_path, [], port, timeout=12)


@pytest.mark.parametrize('mutation', ['hash', 'size', 'name', 'version', 'tag', 'url'])
def test_integrity_never_falls_back(artifact, tmp_path, monkeypatch, mutation):
    """A policy mismatch remains fatal even if a reserve is available."""
    item, data = artifact
    changes = {'hash': {'sha256': '0' * 64}, 'size': {'size': len(data) + 1},
               'name': {'name': 'other'}, 'version': {'version': '9.0.0'},
               'tag': {'tag': 'cp313-cp313-manylinux2014_x86_64'},
               'url': {'url': 'https://user:secret@evil.test/sdk.whl'}}
    item = replace(item, **changes[mutation])
    entries = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))['entries']
    original, _ = artifact
    assert wheels.reserve(original, ROOT, entries) == data
    reserve = create_autospec(wheels.reserve, spec_set=True, return_value=data)
    monkeypatch.setattr(wheels, 'reserve', reserve)
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.return_value = Response(item.url, 200, '', data)
    reasons = {'hash': 'package hash mismatch', 'size': 'package size mismatch',
               'name': 'invalid package filename/tag', 'version': 'invalid package filename/tag',
               'tag': 'invalid package filename/tag', 'url': 'invalid package source'}
    with pytest.raises(wheels.PackageError, match='^' + reasons[mutation] + '$'):
        wheels.acquire(item, tmp_path, entries, port, timeout=12)
    reserve.assert_not_called()


def test_corrupted_response_never_uses_valid_reserve(artifact, monkeypatch):
    """Preserve artifact policy and provide an eligible fallback, then corrupt only bytes."""
    item, data = artifact
    entries = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))['entries']
    assert wheels.reserve(item, ROOT, entries) == data
    reserve = create_autospec(wheels.reserve, spec_set=True, return_value=data)
    monkeypatch.setattr(wheels, 'reserve', reserve)
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.return_value = Response(item.url, 200, '', data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(wheels.PackageError, match='^package hash mismatch$'):
        wheels.acquire(item, ROOT, entries, port, timeout=12)
    reserve.assert_not_called()


def test_runtime_selection_matches_canonical_lock():
    """No runtime filename/hash/version can be inferred from latest releases."""
    document = load_lock(ROOT)
    artifacts = wheels.runtime_artifacts(document)
    assert {a.name for a in artifacts} == {p['name'] for p in document['packages']}
    for item in artifacts:
        package = next(p for p in document['packages'] if p['name'] == item.name)
        assert item.version == package['version']
        assert item.sha256 == package['artifacts'][0]['sha256']


def test_payload_revalidates_owned_artifact_and_keeps_license(artifact, tmp_path):
    """The installed-file view owns source/data/metadata/licence bytes."""
    item, data = artifact
    (tmp_path / item.filename).write_bytes(data)
    value = {'filename': item.filename, 'sha256': item.sha256, 'size': item.size,
             'tag': item.tag, 'url': item.url}
    document = {'packages': [{'name': item.name, 'version': item.version,
                              'artifacts': [value, {**value, 'sha256': '1' * 64}]}]}
    files = wheels.payload_files(document, tmp_path)
    assert 't_tech/invest/__init__.py' in files
    assert b'Copyright 2023 Tinkoff' in files[
        't_tech_investments-1.51.0.dist-info/licenses/LICENSE']


@pytest.mark.parametrize('damage', ['missing', 'hash', 'symlink', 'collision'])
def test_payload_staged_artifact_failure(artifact, tmp_path, damage):
    """Missing/corrupted/stolen wheels and inter-package collisions are fatal."""
    item, data = artifact
    path = tmp_path / item.filename
    if damage != 'missing':
        path.write_bytes(data if damage != 'hash' else b'changed')
    if damage == 'symlink':
        path.unlink()
        path.symlink_to(ROOT / 'vendor/t-tech-investments' / item.filename)
    package = {'name': item.name, 'version': item.version, 'artifacts': [
        {'filename': item.filename, 'sha256': item.sha256, 'size': item.size,
         'tag': item.tag, 'url': item.url}]}
    document = {'packages': [package, package] if damage == 'collision' else [package]}
    with pytest.raises((wheels.PackageError, ValueError)):
        wheels.payload_files(document, tmp_path)


@pytest.mark.parametrize('scheme', ['purelib', 'platlib', 'data', 'headers', 'scripts'])
def test_payload_wheel_scheme_mapping(artifact, tmp_path, scheme):
    """Accepted wheel spread paths stay relative; unsupported schemes fail explicitly."""
    item, _ = artifact
    path = 't_tech_investments-1.51.0.data/' + scheme + '/extra/data.txt'
    data, policy = wheel({path: b'original runtime data'})
    (tmp_path / item.filename).write_bytes(data)
    package = {'name': item.name, 'version': item.version, 'artifacts': [
        {'filename': item.filename, 'sha256': policy.sha256, 'size': len(data),
         'tag': item.tag, 'url': item.url}]}
    if scheme in ('headers', 'scripts'):
        with pytest.raises(wheels.PackageError, match='install scheme'):
            wheels.payload_files({'packages': [package]}, tmp_path)
    else:
        files = wheels.payload_files({'packages': [package]}, tmp_path)
        assert files['extra/data.txt'] == b'original runtime data'


@pytest.mark.parametrize('reason', [
    Reason.DNS, Reason.NETWORK, Reason.TIMEOUT, Reason.HTTP_401, Reason.HTTP_403,
    Reason.HTTP_404, Reason.HTTP_429, Reason.HTTP_5XX])
def test_eligible_exact_reserve_warns(artifact, reason, caplog):
    """Licence/bytes stay verified; the package's precise reserve alone is selected."""
    item, data = artifact
    entries = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))['entries']
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.side_effect = AvailabilityError(reason)
    result = wheels.acquire(item, ROOT, entries, port, timeout=10)
    assert result == data
    assert reason.value in caplog.text and item.version in caplog.text
    assert 'vendor/t-tech-investments/' in caplog.text


@pytest.mark.parametrize('issue', ['excluded', 'version', 'hash', 'missing', 'licence', 'symlink'])
def test_reserve_rejects_unusable_candidates(artifact, tmp_path, issue):
    """Missing/licence-invalid reserves do not trigger older/latest or a source build."""
    item, data = artifact
    entries = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))['entries']
    entry = next(e for e in entries if e['name'] == item.name)
    root = ROOT
    if issue == 'excluded':
        entry['storage'] = 'excluded'
    elif issue == 'version':
        entry['version'] = '1.50.0'
    elif issue == 'hash':
        item = replace(item, sha256='0' * 64)
    elif issue == 'missing':
        root = tmp_path
    elif issue == 'licence':
        entry['artifacts'][1]['sha256'] = '0' * 64
    else:
        root = tmp_path
        directory = root / 'vendor/t-tech-investments'
        directory.mkdir(parents=True)
        (directory / item.filename).write_bytes(data)
        (directory / 'LICENSE').symlink_to(ROOT / 'vendor/t-tech-investments/LICENSE')
    with pytest.raises(wheels.PackageError):
        wheels.reserve(item, root, entries)


@pytest.mark.parametrize('mutation', [
    {'files': {DIST + 'METADATA': b'Name: other\nVersion: 1.51.0\n'}},
    {'files': {DIST + 'METADATA': b'Name: t-tech-investments\nVersion: 0.1.0\n'}},
    {'files': {DIST + 'WHEEL': b'Wheel-Version: 2.0\nTag: py3-none-any\n'}},
    {'files': {DIST + 'WHEEL': b'Wheel-Version: 1.0\nTag: cp313-none-any\n'}},
    {'files': {'../escape': b'bad'}},
    {'files': {'/absolute': b'bad'}},
    {'files': {'x\\escape': b'bad'}},
    {'files': {'x': b'file', 'x/child': b'collision'}},
    {'record': b'bad\n'},
    {'record': b'x,sha256=bad,1\n'},
    {'record': (DIST + 'RECORD,hash,2\n').encode()},
    {'record': (DIST + 'RECORD,,\n').encode()},
    {'extra': [('t_tech/invest/__init__.py', b'duplicate')]},
])
def test_zip_metadata_and_record_corruption_are_fatal(artifact, mutation):
    """Correct outer hash does not authorize malformed wheel contents."""
    item, _ = artifact
    data, policy = wheel(**mutation)
    item = replace(item, size=len(data), sha256=policy.sha256)
    with pytest.raises(wheels.PackageError):
        wheels.validate_bytes(item, data)


def test_non_zip_is_fatal(artifact):
    """The hash can match invalid data without making it an installable artifact."""
    item, _ = artifact
    data = b'not a ZIP'
    item = replace(item, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(wheels.PackageError, match='structure'):
        wheels.validate_bytes(item, data)


@pytest.mark.parametrize('status', [201, 301, 302, 400, 405])
def test_unapproved_status_fails_without_fallback(artifact, status):
    """Redirect and protocol failures remain fatal."""
    item, _ = artifact
    port = create_autospec(wheels.Transport, instance=True, spec_set=True)
    port.fetch.return_value = Response(item.url, status, '', b'')
    with pytest.raises(wheels.PackageError, match='status'):
        wheels.acquire(item, ROOT, [], port, timeout=10)


def test_development_uses_reviewed_inventory_and_keeps_runtime_pins():
    """Development artifacts are the exact reviewed seed, excluding locked runtime."""
    entries = json.loads((ROOT / 'vendor/inventory.json').read_text(encoding='utf-8'))['entries']
    runtime = {p['name'] for p in load_lock(ROOT)['packages']}
    artifacts = wheels.development_artifacts(entries, runtime)
    assert artifacts and not runtime.intersection(a.name for a in artifacts)
    for item in artifacts:
        entry = next(e for e in entries if e['name'] == item.name)
        assert item.version == entry['version']
        wheels.validate_artifact(item)
    entries[0]['artifacts'] = []
    with pytest.raises(wheels.PackageError):
        wheels.artifact_for(entries[0])


def test_request_worker_exact_timeout_owned_bytes(artifact, monkeypatch):
    """A process owns its temporary output; no byte/size cap from the SDK leaks here."""
    item, data = artifact

    def response(_arguments, **kwargs):
        Path(kwargs['cwd'], 'body').write_bytes(data)
        return subprocess.CompletedProcess([], 0, b'{"status":200}', b'')
    run = create_autospec(subprocess.run, spec_set=True, side_effect=response)
    monkeypatch.setattr(wheels.subprocess, 'run', run)
    result = wheels.WorkerTransport().fetch(item, 3)
    assert result.data == data and result.url == item.url
    call = run.call_args
    assert call.args[0][:4] == [sys.executable, '-I', '-S', '-c']
    assert call.kwargs['timeout'] == 3
    assert not Path(call.kwargs['cwd']).exists()


@pytest.mark.parametrize('failure', [
    'timeout', 'os', 'invalid-json', 'error', 'exit', 'missing-body', 'status-type', 'oversized'])
def test_request_worker_failure_is_safe_and_cleans_up(artifact, monkeypatch, failure):
    """Only typed availability gets a reserve; malformed worker output is fatal."""
    item, _ = artifact
    run = create_autospec(subprocess.run, spec_set=True)
    if failure in ('timeout', 'os'):
        run.side_effect = (subprocess.TimeoutExpired([], 1) if failure == 'timeout'
                           else OSError('credential'))
    else:
        values = {'invalid-json': b'{', 'error': b'{"error":"TLS"}',
                  'exit': b'{}', 'missing-body': b'{"status":200}',
                  'status-type': b'{"status":"200"}', 'oversized': b'x' * 257}
        run.return_value = subprocess.CompletedProcess([], 1 if failure == 'exit' else 0,
                                                       values[failure], b'credential')
    monkeypatch.setattr(wheels.subprocess, 'run', run)
    with pytest.raises((wheels.PackageError, AvailabilityError)) as error:
        wheels.WorkerTransport().fetch(item, 3)
    assert 'credential' not in str(error.value)
    assert not Path(run.call_args.kwargs['cwd']).exists()


@pytest.mark.parametrize('value', [
    b'{"availability":"dns_unavailable"}', b'{"status":404}'])
def test_request_worker_typed_availability(artifact, monkeypatch, value):
    """Worker JSON carries safe transport/status facts without raw stderr."""
    item, _ = artifact
    run = create_autospec(subprocess.run, spec_set=True,
                          return_value=subprocess.CompletedProcess([], 0, value, b''))
    monkeypatch.setattr(wheels.subprocess, 'run', run)
    if b'availability' in value:
        with pytest.raises(AvailabilityError):
            wheels.WorkerTransport().fetch(item, 3)
    else:
        assert wheels.WorkerTransport().fetch(item, 3).status == 404


class ResponsePort:
    """Strict context manager surface used by urllib."""
    status = 200

    def geturl(self):
        """Final URL fact."""
        return ''

    def read(self, _limit):
        """Bounded owned response."""
        return b''

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return False


@pytest.mark.parametrize('failure', [None, 'large', 'redirect', 'http', 'dns', 'tls', 'timeout'])
def test_worker_request_transport_facts(artifact, tmp_path, monkeypatch, capsys, failure):
    """urllib uses verified TLS and no proxy/redirect with bounded response reads."""
    item, data = artifact
    response = create_autospec(ResponsePort, instance=True, spec_set=True)
    response.__enter__.return_value = response
    response.status = 200
    response.geturl.return_value = item.url if failure != 'redirect' else 'https://evil.test'
    response.read.return_value = data if failure != 'large' else data + b'x'
    opener = create_autospec(wheels.build_opener(), spec_set=True)
    opener.open.return_value = response
    if failure == 'http':
        opener.open.side_effect = HTTPError(item.url, 503, '', {}, None)
    elif failure in ('dns', 'tls', 'timeout'):
        reasons = {'dns': socket.gaierror(), 'tls': ssl.SSLError(), 'timeout': TimeoutError()}
        opener.open.side_effect = URLError(reasons[failure])
    factory = create_autospec(wheels.build_opener, spec_set=True, return_value=opener)
    monkeypatch.setattr(wheels, 'build_opener', factory)
    monkeypatch.setattr(wheels.sys, 'argv', ['worker', item.url, str(item.size)])
    monkeypatch.chdir(tmp_path)
    wheels.request_main()
    result = json.loads(capsys.readouterr().out)
    if failure is None:
        assert result == {'status': 200} and (tmp_path / 'body').read_bytes() == data
        response.read.assert_called_once_with(item.size + 1)
    elif failure == 'http':
        assert result == {'status': 503}
    elif failure in ('dns', 'timeout'):
        assert result == {'availability': 'dns_unavailable' if failure == 'dns' else 'timeout'}
    else:
        assert 'error' in result
    assert opener.open.call_args.kwargs['timeout'] == 15


def test_real_worker_timeout_kills_waits_and_removes_owned_output(
        artifact, tmp_path, monkeypatch):
    """A real stalled process is reaped; this test never makes a network request."""
    item, _ = artifact
    original_run = subprocess.run
    pid_file = tmp_path / 'worker-pid'

    def stalled_request(_arguments, **kwargs):
        code = ('import os,time; from pathlib import Path; '
                f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)')
        return original_run([sys.executable, '-I', '-S', '-c', code],
                            check=kwargs.pop('check'), **kwargs)

    run = create_autospec(subprocess.run, spec_set=True, side_effect=stalled_request)
    monkeypatch.setattr(wheels.subprocess, 'run', run)
    with pytest.raises(AvailabilityError) as error:
        wheels.WorkerTransport().fetch(item, .3)
    assert error.value.reason == Reason.TIMEOUT
    pid = int(pid_file.read_text(encoding='ascii'))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not Path(run.call_args.kwargs['cwd']).exists()


def test_bad_url_port_is_fatal_before_request(artifact):
    """Malformed source ports are sanitized as policy failures."""
    item, _ = artifact
    with pytest.raises(wheels.PackageError, match='source'):
        wheels.validate_artifact(replace(item, url='https://files.pythonhosted.org:bad/packages/x'))
