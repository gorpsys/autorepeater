"""Offline source policy and real supervised process deadlines."""
from dataclasses import FrozenInstanceError
from email.message import Message
from io import BytesIO
import json
import base64
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from unittest.mock import create_autospec
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector
from urllib.response import addinfourl

from test.test_sdk_wheel import ROOT, approval, manifest_data, wheel

import pytest

from scripts import sdk_wheel as sdk
from scripts import sdk_worker


URL = ('https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/files/'
       + sdk.FILENAME)


def index(digest=sdk.SDK_HASH, anchors=None):
    """A normal GitLab simple index, with hash on its wheel link."""
    if anchors is None:
        anchors = f'<a href="{URL}#sha256={digest}">{sdk.FILENAME}</a><br/>'
    return ('<!DOCTYPE html><html><head><title>Links for t-tech-investments</title>'
            '</head><body><h1>Links for t-tech-investments</h1>' + anchors
            + '</body></html>').encode()


def transport(body=None, payload=None, digest=sdk.SDK_HASH):
    """Strict transport port, returning explicit bounded response DTOs."""
    port = create_autospec(sdk.Transport, instance=True, spec_set=True)
    if payload is None:
        payload = sdk.load_vendored_wheel(ROOT).data
    port.fetch.side_effect = [
        sdk.Response(sdk.SIMPLE_URL, 200, 'text/html', index(digest) if body is None else body),
        sdk.Response(URL, 200, 'application/octet-stream', payload),
    ]
    return port


def test_auto_official_first():
    """The index digest selects owned bytes and causes exactly two requests."""
    port = transport()
    result = sdk.select_source(ROOT, 'auto', port)
    assert result.source == sdk.Source.OFFICIAL
    assert result.reason == sdk.Reason.OFFICIAL_AVAILABLE
    assert result.index_sha256 == sdk.SDK_HASH
    assert result.wheel.data == sdk.load_vendored_wheel(ROOT).data
    assert [call.args[0] for call in port.fetch.call_args_list] == [sdk.SIMPLE_URL, URL]
    with pytest.raises(FrozenInstanceError):
        result.source = sdk.Source.VENDORED


def test_restored_index_links_download_from_the_existing_official_origin():
    """The restored GitLab hostname does not extend allowed download hosts."""
    restored = URL.replace('https://opensource.tbank.ru/',
                           'https://tbank-restore.gitlab.yandexcloud.net/')
    anchor = f'<a href="{restored}#sha256={sdk.SDK_HASH}">{sdk.FILENAME}</a>'
    port = transport(body=index(anchors=anchor))
    result = sdk.select_source(ROOT, 'auto', port)
    assert result.source == sdk.Source.OFFICIAL
    assert [item.args[0] for item in port.fetch.call_args_list] == [sdk.SIMPLE_URL, URL]


@pytest.mark.parametrize('origin', [
    'http://tbank-restore.gitlab.yandexcloud.net',
    'https://tbank-restore.gitlab.yandexcloud.net:443',
    'https://tbank-restore.gitlab.yandexcloud.net.example.org',
    'https://user@tbank-restore.gitlab.yandexcloud.net',
])
def test_restored_index_does_not_accept_other_origins(origin):
    """Only the exact observed HTTPS origin may map back to the allowed host."""
    link = origin + '/files/' + sdk.FILENAME
    anchor = f'<a href="{link}#sha256={sdk.SDK_HASH}">{sdk.FILENAME}</a>'
    with pytest.raises(sdk.WheelError, match='invalid SDK official URL'):
        sdk.parse_index(index(anchors=anchor))


@pytest.mark.parametrize('version', ['1.52.0rc1', '1.52.0.post1', '1.52.0.dev1', '1.52.0+local'])
@pytest.mark.parametrize('mode', ['auto', 'vendored'])
@pytest.mark.parametrize('present', [False, True])
def test_unrelated_valid_releases_do_not_block_pinned_sdk(version, mode, present):
    """Neighbour releases cannot change the selected filename, hash or source policy."""
    filename = f't_tech_investments-{version}-py3-none-any.whl'
    anchors = f'<a href="/files/{filename}#sha256={"1" * 64}">{filename}</a>'
    if present:
        anchors += f'<a href="{URL}#sha256={sdk.SDK_HASH}">{sdk.FILENAME}</a>'
    port = transport(body=index(anchors=anchors))
    selected = sdk.select_source(ROOT, mode, port)
    assert selected.wheel.manifest.sha256 == sdk.SDK_HASH
    assert selected.source == (sdk.Source.OFFICIAL if present and mode == 'auto'
                               else sdk.Source.VENDORED)
    assert port.fetch.call_count == (2 if present and mode == 'auto' else 1)
    assert selected.index_sha256 == (sdk.SDK_HASH if present else None)


@pytest.mark.parametrize('stage', ['index', 'wheel'])
@pytest.mark.parametrize('reason', ['dns_unavailable', 'network_unavailable', 'timeout'])
def test_transport_availability_fallback(stage, reason, caplog):
    """Only typed availability facts allow a single reserve."""
    port = transport()
    failure = sdk.AvailabilityError(sdk.Reason(reason))
    if stage == 'index':
        port.fetch.side_effect = failure
    else:
        port.fetch.side_effect = [sdk.Response(sdk.SIMPLE_URL, 200, 'text/html', index()), failure]
    result = sdk.select_source(ROOT, 'auto', port)
    assert result.source == sdk.Source.VENDORED
    assert result.reason == sdk.Reason(reason)
    assert port.fetch.call_count == (1 if stage == 'index' else 2)
    assert 'WARNING' in caplog.text and reason in caplog.text
    assert sdk.VERSION in caplog.text and 'vendor/t-tech-investments/' in caplog.text


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500, 503, 599])
@pytest.mark.parametrize('stage', ['index', 'wheel'])
def test_http_availability_fallback(status, stage):
    """Allowed HTTP facts are identical for index and download."""
    port = transport()
    response = sdk.Response(sdk.SIMPLE_URL if stage == 'index' else URL,
                            status, 'application/json', b'secret raw body')
    port.fetch.side_effect = (
        [response] if stage == 'index' else
        [sdk.Response(sdk.SIMPLE_URL, 200, 'text/html', index()), response])
    assert sdk.select_source(ROOT, 'auto', port).reason.value == (
        'http_5xx' if status >= 500 else f'http_{status}')


@pytest.mark.parametrize('status', [201, 204, 301, 302, 307, 308, 400, 405, 418])
def test_other_http_fatal(status):
    """Redirects and unrelated HTTP responses are not absence evidence."""
    port = transport()
    port.fetch.side_effect = [sdk.Response(sdk.SIMPLE_URL, status, 'text/html', index())]
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, 'auto', port)
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('body', [
    index(anchors=''), index(anchors=(
        '<a href="https://opensource.tbank.ru/files/t_tech_investments-1.50.0-py3-none-any.whl'
        '#sha256=' + '1' * 64 + '">t_tech_investments-1.50.0-py3-none-any.whl</a>')),
])
def test_valid_missing_version(body):
    """A complete empty index or another release is valid absence."""
    port = transport(body=body)
    assert sdk.select_source(ROOT, 'auto', port).reason == sdk.Reason.VERSION_MISSING
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('body', [
    b'{}', b'', b'<html><body>Login</body></html>',
    b'<html><body><form><input type="password"></form></body></html>',
    index().replace(b'</body>', b''), index().replace(b'</a>', b''),
    index().replace(b'</html>', b'<html>'), index(anchors='<p>error</p>'),
    index(anchors='<a>missing href</a>'), index(anchors='<a href="/bad">bad</a>'),
    index(anchors='<a href="/x" href="/y">bad</a>'),
    index(anchors='<script>login()</script>'), index().replace(b'Links for', b'Login for'),
    index().replace(b'#sha256=', b'#md5='),
    index().replace(b'#sha256=' + sdk.SDK_HASH.encode(), b''),
    index().replace(b'<a href=', b'<a data-yanked="reason" href='),
    index(anchors=f'<a href="{URL}#sha256={sdk.SDK_HASH}">{sdk.FILENAME}</a>' * 2),
    index().replace(sdk.FILENAME.encode(), b't_tech_investments-1.51.0-cp312-none-any.whl'),
])
def test_malformed_index_fatal(body):
    """HTMLParser tolerance cannot turn malformed/login/yanked pages into reserve."""
    port = transport(body=body)
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, 'auto', port)
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('bad_url', [
    'http://opensource.tbank.ru/x', 'https://evil.example/x',
    'https://user:secret@opensource.tbank.ru/x', '//evil.example/x',
    'https://opensource.tbank.ru:444/x', 'https://opensource.tbank.ru:bad/x',
    'https://opensource.tbank.ru/x?token=secret', 'https://opensource.tbank.ru/x%2f../x',
])
def test_unknown_link_fatal(bad_url):
    """Unknown origins and disguised paths never cause a request or a fallback."""
    port = transport(body=index().replace(URL.encode(), bad_url.encode()))
    with pytest.raises(sdk.WheelError) as caught:
        sdk.select_source(ROOT, 'auto', port)
    assert 'secret' not in str(caught.value)
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('media', ['application/json', 'text/plain', '',
                                   'text/html; charset=latin1'])
def test_index_media_fatal(media):
    """Only the supported HTML encoding is acceptable."""
    port = transport()
    port.fetch.side_effect = [sdk.Response(sdk.SIMPLE_URL, 200, media, index())]
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, 'auto', port)


def test_forced_vendored_probes_only(caplog):
    """Explicit source retains approval probing without remote wheel download."""
    port = transport()
    result = sdk.select_source(ROOT, 'vendored', port)
    assert result.source == sdk.Source.VENDORED
    assert result.reason == sdk.Reason.EXPLICIT_VENDORED
    assert result.index_sha256 == sdk.SDK_HASH
    assert port.fetch.call_count == 1
    assert 'explicit_vendored_mode' in caplog.text


def approved_root(tmp_path, digest):
    """Copy only task-owned reserve files; approval is confined to a test fixture."""
    directory = tmp_path / 'vendor/t-tech-investments'
    directory.mkdir(parents=True)
    for name in [sdk.FILENAME, 'LICENSE']:
        (directory / name).write_bytes((ROOT / 'vendor/t-tech-investments' / name).read_bytes())
    value = manifest_data()
    item = approval()
    item['index_sha256'] = digest
    value['approvals'] = [item]
    (directory / 'manifest.json').write_text(json.dumps(value))
    return tmp_path


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_mismatch_requires_exact_approval(mode, tmp_path):
    """Matching source code is not approval of a different official wheel digest."""
    data, policy = wheel()
    port = transport(payload=data, digest=policy.sha256)
    with pytest.raises(sdk.WheelError, match='approval_required'):
        sdk.select_source(ROOT, mode, port)
    assert port.fetch.call_count == 1
    root = approved_root(tmp_path, policy.sha256)
    for _ in range(2):
        result = sdk.select_source(root, mode, transport(payload=data, digest=policy.sha256))
        assert result.wheel.data == (data if mode == 'auto' else sdk.load_vendored_wheel(ROOT).data)
    with pytest.raises(sdk.WheelError, match='approval_required'):
        sdk.select_source(root, mode, transport(digest='2' * 64))
    assert manifest_data()['approvals'] == []


@pytest.mark.parametrize('damage', ['hash', 'metadata', 'license', 'record'])
def test_approval_does_not_waive_corruption(tmp_path, damage):
    """Approved index bytes still pass the complete Task2 boundary."""
    files = {}
    if damage == 'metadata':
        files[sdk.DIST_INFO + 'METADATA'] = b'Name: wrong\n'
    if damage == 'license':
        files[sdk.LICENSE_PATH] = b'wrong'
    data, policy = wheel(files=files, record=b'' if damage == 'record' else None)
    root = approved_root(tmp_path, policy.sha256)
    port = transport(payload=b'bad' if damage == 'hash' else data, digest=policy.sha256)
    with pytest.raises(sdk.WheelError):
        sdk.select_source(root, 'auto', port)
    assert port.fetch.call_count == 2


@pytest.mark.parametrize('stage', ['index', 'wheel'])
def test_tls_fatal_and_safe(stage, caplog):
    """TLS errors never degrade to reserve or expose a raw credential-bearing cause."""
    port = transport()
    error = sdk.WheelError('invalid SDK TLS')
    error.__cause__ = ssl.SSLCertVerificationError('https://secret@example.org')
    port.fetch.side_effect = (
        error if stage == 'index' else
        [sdk.Response(sdk.SIMPLE_URL, 200, 'text/html', index()), error])
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, 'auto', port)
    assert 'secret' not in caplog.text


def blocking_command(tmp_path, monkeypatch, *, child=False):
    """An actual offline child process, replacing only the launch command port."""
    marker = tmp_path / 'pid'
    code = ('import os,time,pathlib,subprocess,sys; '
            + ('subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
               if child else '')
            + f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(120)')
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', code])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    return marker


def test_real_worker_deadline_cleanup(tmp_path, monkeypatch):
    """A stuck process is killed, waited, and its owned result directory removed."""
    marker = blocking_command(tmp_path, monkeypatch)
    start = time.monotonic()
    with pytest.raises(sdk_worker.WorkerTimeout):
        sdk_worker.run_worker('select', ROOT, 'auto', timeout=0.2, temp_root=tmp_path)
    assert time.monotonic() - start < 3
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert sorted(path.name for path in tmp_path.iterdir()) == ['pid']


def test_worker_interruption_cleanup(tmp_path, monkeypatch):
    """Cancellation also confirms process termination before leaving its scope."""
    blocking_command(tmp_path, monkeypatch)
    original_wait = subprocess.Popen.wait
    calls = 0

    def interrupted_wait(process, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.1)
            raise KeyboardInterrupt
        return original_wait(process, timeout)

    monkeypatch.setattr(subprocess.Popen, 'wait', create_autospec(
        original_wait, spec_set=True, side_effect=interrupted_wait))
    with pytest.raises(KeyboardInterrupt):
        sdk_worker.run_worker('select', ROOT, 'auto', timeout=1, temp_root=tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'pid').read_text()), 0)
    assert sorted(path.name for path in tmp_path.iterdir()) == ['pid']


def test_acquisition_timeout_falls_back_once(monkeypatch, caplog):
    """The public API uses one bounded worker attempt then one local-only reserve."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True)
    reserve = sdk.Selection(sdk.Source.VENDORED, sdk.Reason.TIMEOUT,
                            sdk.load_vendored_wheel(ROOT), None)
    run.side_effect = [sdk_worker.WorkerTimeout('index_request'), sdk.encode_selection(reserve)]
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    assert sdk.acquire_sdk(ROOT).reason == sdk.Reason.TIMEOUT
    assert [call.args[0] for call in run.call_args_list] == ['select', 'reserve']
    assert all(0 < call.kwargs['timeout'] <= 60 for call in run.call_args_list)
    assert 'timeout' in caplog.text


@pytest.mark.parametrize('value', [
    None, {}, {'source': 'official'}, {'error': 'https://secret@example.org'},
    {'error': 'validation_failed', 'extra': 'secret'},
])
def test_worker_result_schema(value):
    """Arbitrary child output is never trusted as source, bytes or a safe reason."""
    with pytest.raises(sdk.WheelError) as caught:
        sdk.decode_selection(value, sdk.load_vendored_wheel(ROOT))
    assert 'secret' not in str(caught.value)


def test_relative_link_and_utf8_index():
    """Canonical same-origin relative links and HTML UTF8 are supported."""
    port = transport()
    payload = sdk.load_vendored_wheel(ROOT).data
    body = index().replace(URL.encode(), sdk.FILENAME.encode())
    url = sdk.SIMPLE_URL + sdk.FILENAME
    port.fetch.side_effect = [
        sdk.Response(sdk.SIMPLE_URL, 200, 'text/html; charset=UTF-8', body),
        sdk.Response(url, 200, 'application/zip', payload),
    ]
    assert sdk.select_source(ROOT, 'auto', port).source == sdk.Source.OFFICIAL


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_corrupt_reserve_prevents_network(tmp_path, mode):
    """The reserve pin/licence is never relaxed by official source availability."""
    root = approved_root(tmp_path, '1' * 64)
    (root / 'vendor/t-tech-investments' / sdk.FILENAME).write_bytes(b'bad')
    port = transport()
    with pytest.raises(sdk.WheelError):
        sdk.select_source(root, mode, port)
    port.fetch.assert_not_called()


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
@pytest.mark.parametrize('body', [b'\xff', b'x' * (sdk.MAX_WHEEL_BYTES + 1)])
def test_index_size_and_encoding_fatal(mode, body):
    """Invalid UTF8 or excess bytes fail before any wheel fetch."""
    port = transport(body=body)
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, mode, port)
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('field,value', [
    ('url', 'https://secret@example.org'), ('status', True), ('status', '200'),
    ('media_type', None), ('data', bytearray(b'html')),
])
def test_response_dto_fail_closed(field, value):
    """The typed port cannot silently coerce malformed output."""
    values = {'url': sdk.SIMPLE_URL, 'status': 200, 'media_type': 'text/html', 'data': index()}
    values[field] = value
    port = transport()
    port.fetch.side_effect = [sdk.Response(**values)]
    with pytest.raises(sdk.WheelError):
        sdk.select_source(ROOT, 'auto', port)


def opener_port(monkeypatch):
    """Autospecced urllib boundary; no actual connection can be made."""
    port = create_autospec(OpenerDirector, instance=True, spec_set=True)
    build = create_autospec(sdk.build_opener, spec_set=True, return_value=port)
    monkeypatch.setattr(sdk, 'build_opener', build)
    context = create_autospec(sdk.sdk_tls_context, spec_set=True,
                              return_value=ssl.create_default_context())
    monkeypatch.setattr(sdk, 'sdk_tls_context', context)
    return port, build


@pytest.mark.parametrize('url', [sdk.SIMPLE_URL, URL])
def test_verified_tls_request_and_bounded_read(monkeypatch, url):
    """Default certificate verification stays on; proxies and redirects are disabled."""
    port, build = opener_port(monkeypatch)
    headers = Message()
    headers['Content-Type'] = 'text/html'
    response = addinfourl(BytesIO(b'body'), headers, url, code=200)
    port.open.return_value = response
    result = sdk.request_bytes(url)
    assert result == sdk.Response(url, 200, 'text/html', b'body')
    assert response.closed
    handlers = build.call_args.args
    assert handlers[0].proxies == {}
    assert handlers[1]._context.check_hostname  # pylint: disable=protected-access
    assert handlers[1]._context.verify_mode == ssl.CERT_REQUIRED  # pylint: disable=protected-access
    assert isinstance(handlers[2], sdk.NoRedirect)
    assert handlers[2].redirect_request(None) is None
    request = port.open.call_args.args[0]
    assert request.full_url == url
    assert request.headers['Accept'] == ('text/html' if url == sdk.SIMPLE_URL
                                         else 'application/octet-stream')
    assert port.open.call_args.kwargs == {'timeout': 15}


@pytest.mark.parametrize('status', [301, 403, 404, 500])
def test_http_error_keeps_status_without_reading_body(monkeypatch, status):
    """HTTP response status is evidence; credential-bearing body is not classified."""
    port, _ = opener_port(monkeypatch)
    body = BytesIO(b'password=secret')
    error = HTTPError(URL, status, 'secret', {}, body)
    port.open.side_effect = error
    assert sdk.request_bytes(URL) == sdk.Response(URL, status, '', b'')
    assert body.closed


@pytest.mark.parametrize('error,reason', [
    (URLError(socket.gaierror(-2, 'secret')), sdk.Reason.DNS),
    (URLError(TimeoutError('secret')), sdk.Reason.TIMEOUT),
    (ConnectionResetError('secret'), sdk.Reason.NETWORK),
    (URLError(OSError(101, 'secret')), sdk.Reason.NETWORK),
])
def test_native_availability_classification(monkeypatch, error, reason):
    """Structured native errors, not localized messages, determine fallback."""
    port, _ = opener_port(monkeypatch)
    port.open.side_effect = error
    with pytest.raises(sdk.AvailabilityError) as caught:
        sdk.request_bytes(URL)
    assert caught.value.reason == reason
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('error', [
    URLError(ssl.SSLCertVerificationError('secret')),
    ssl.SSLError('secret'), URLError('secret DNS timeout'), URLError(ValueError('secret')),
    OSError(13, 'secret'),
])
def test_native_unknown_or_tls_failure_fatal(monkeypatch, error):
    """Text resembling network failure and unknown native failures remain fatal."""
    port, _ = opener_port(monkeypatch)
    port.open.side_effect = error
    with pytest.raises(sdk.WheelError) as caught:
        sdk.request_bytes(URL)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('bad', ['url', 'size'])
def test_native_response_bounds(monkeypatch, bad):
    """The reader enforces the fixed URL and a 2MiB body before parsing."""
    port, _ = opener_port(monkeypatch)
    port.open.return_value = addinfourl(
        BytesIO(b'a' * (sdk.MAX_WHEEL_BYTES + 1) if bad == 'size' else b'body'), Message(),
        'https://secret@example.org' if bad == 'url' else URL, code=200)
    with pytest.raises(sdk.WheelError):
        sdk.request_bytes(URL)


@pytest.mark.parametrize('value', [
    {'availability': 'dns_unavailable'}, {'availability': 'network_unavailable'},
    {'availability': 'timeout'},
])
def test_request_worker_availability(monkeypatch, value):
    """Known child transport facts cross the boundary as typed availability."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True, return_value=value)
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.AvailabilityError) as caught:
        sdk.WorkerTransport().fetch(URL)
    assert caught.value.reason.value == value['availability']
    assert run.call_args.kwargs['timeout'] == 15


@pytest.mark.parametrize('value', [
    {'availability': 'official_available'}, {'availability': 'unknown'},
    {'availability': []}, {'error': 'unknown raw secret'},
    {'url': URL, 'status': 200, 'media_type': 'application/zip', 'data': '%%%bad'},
    {'url': URL, 'status': True, 'media_type': 'application/zip', 'data': ''},
])
def test_request_worker_malformed_fatal(monkeypatch, value):
    """Unknown reasons, DTO types and encodings are not transport availability."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True, return_value=value)
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.WheelError):
        sdk.WorkerTransport().fetch(URL)


def test_request_worker_valid_bytes(monkeypatch):
    """Only explicit bounded bytes, URL, status and media form a response."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True, return_value={
        'url': URL, 'status': 200, 'media_type': 'application/zip', 'data': 'Ym9keQ=='})
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    assert sdk.WorkerTransport().fetch(URL) == sdk.Response(URL, 200, 'application/zip', b'body')


def test_request_worker_hard_timeout(monkeypatch):
    """Supervisor timeout becomes only the known request-timeout reason."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True,
                          side_effect=sdk_worker.WorkerTimeout())
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.AvailabilityError) as caught:
        sdk.WorkerTransport().fetch(URL)
    assert caught.value.reason == sdk.Reason.TIMEOUT


@pytest.mark.parametrize('root', [ROOT, Path('.')])
def test_real_reserve_worker_handoff(tmp_path, root):
    """Real isolated stdlib worker hands off immutable wheel bytes, with no network."""
    value = sdk_worker.run_worker('reserve', root, 'vendored', timeout=5, temp_root=tmp_path)
    result = sdk.decode_selection(value, sdk.load_vendored_wheel(ROOT))
    assert result.reason == sdk.Reason.EXPLICIT_VENDORED
    assert result.wheel.data == sdk.load_vendored_wheel(ROOT).data
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('payload', [
    "print('{}')", "print('not-json')", "print('{\"key\":1,\"key\":2}')", 'raise SystemExit(1)',
    "print('x' * (3 * 1024 * 1024 + 1))",
])
def test_real_worker_bounded_json(tmp_path, monkeypatch, payload):
    """Actual process output is bounded and strict JSON; duplicate keys fail."""
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', payload])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    if payload == "print('{}')":
        assert sdk_worker.run_worker('reserve', ROOT, 'auto', timeout=3, temp_root=tmp_path) == {}
    else:
        with pytest.raises(sdk_worker.WorkerError):
            sdk_worker.run_worker('reserve', ROOT, 'auto', timeout=3, temp_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_worker_force_kill_cleanup(tmp_path, monkeypatch):
    """A process ignoring graceful termination is killed and waited."""
    marker = tmp_path / 'pid'
    code = ('import signal,os,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(120)')
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', code])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    with pytest.raises(sdk_worker.WorkerTimeout):
        sdk_worker.run_worker('select', ROOT, 'auto', timeout=0.25, temp_root=tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_forced_probe_unavailable(mode, caplog):
    """Forced mode still warns explicitly on a valid unavailable probe."""
    port = transport()
    port.fetch.side_effect = sdk.AvailabilityError(sdk.Reason.DNS)
    result = sdk.select_source(ROOT, mode, port)
    assert result.reason == (sdk.Reason.EXPLICIT_VENDORED if mode == 'vendored' else sdk.Reason.DNS)
    assert port.fetch.call_count == 1
    assert result.reason.value in caplog.text


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
@pytest.mark.parametrize('deadline', ['request', 'outer'])
def test_actual_acquisition_blocked_dns_cleanup(tmp_path, monkeypatch, mode, deadline):
    """An offline blocking DNS stub traverses real selection/request processes."""
    marker = tmp_path / 'request.json'
    stub = ('import os,time,json,pathlib; '
            f'pathlib.Path({str(marker)!r}).write_text(json.dumps('
            '{"pid":os.getpid(), "directory":str(pathlib.Path.cwd())})); time.sleep(120)')
    # The selection subprocess uses the real worker entry point and only replaces DNS.
    code = f'''import sys
sys.path.insert(0, {str(ROOT)!r})
from scripts import sdk_worker, sdk_wheel
original_run = sdk_worker.run_worker
def launch(action, root, argument):
    return [sys.executable, '-I', '-S', '-c', {stub!r}]
def bounded(action, root, argument, **kwargs):
    if action == 'request':
        kwargs['timeout'] = {0.25 if deadline == 'request' else 15}
    return original_run(action, root, argument, **kwargs)
sdk_worker.worker_command = launch
sdk_worker.run_worker = bounded
sdk_wheel.worker_main()
'''
    original_command = sdk_worker.worker_command

    def launch(action, root, argument):
        if action == 'select':
            return [sys.executable, '-I', '-S', '-c', code, action, str(root), argument]
        return original_command(action, root, argument)

    original_run = sdk_worker.run_worker

    def bounded(action, root, argument, **kwargs):
        if action == 'select' and deadline == 'outer':
            kwargs['timeout'] = 0.5
        return original_run(action, root, argument, **kwargs)

    monkeypatch.setattr(sdk_worker, 'worker_command', create_autospec(
        original_command, spec_set=True, side_effect=launch))
    monkeypatch.setattr(sdk_worker, 'run_worker', create_autospec(
        original_run, spec_set=True, side_effect=bounded))
    start = time.monotonic()
    result = sdk.acquire_sdk(ROOT, mode)
    assert time.monotonic() - start < 3
    assert result.reason == (sdk.Reason.EXPLICIT_VENDORED if mode == 'vendored'
                             else sdk.Reason.TIMEOUT)
    child = json.loads(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child['pid'], 0)
    assert not Path(child['directory']).exists()
    assert not Path(child['directory']).parent.exists()


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_worker_entry_selection(mode, monkeypatch, capsys):
    """Entry point JSON matches actual selection, without invoking the network."""
    port = transport()
    factory = create_autospec(sdk.WorkerTransport, spec_set=True, return_value=port)
    handler = create_autospec(sdk.signal.signal, spec_set=True)
    monkeypatch.setattr(sdk, 'WorkerTransport', factory)
    monkeypatch.setattr(sdk.signal, 'signal', handler)
    monkeypatch.setattr(sdk_worker, 'publish_phase', create_autospec(
        sdk_worker.publish_phase, spec_set=True))
    monkeypatch.setattr(sys, 'argv', ['worker', 'select', str(ROOT), mode])
    sdk.worker_main()
    result = sdk.decode_selection(json.loads(capsys.readouterr().out),
                                  sdk.load_vendored_wheel(ROOT))
    assert result.source == (sdk.Source.OFFICIAL if mode == 'auto' else sdk.Source.VENDORED)
    with pytest.raises(KeyboardInterrupt):
        handler.call_args.args[1](15, None)


@pytest.mark.parametrize('kind', ['request', 'availability', 'invalid', 'approval'])
def test_worker_entry_request_errors(kind, monkeypatch, capsys):
    """Fixed JSON reasons are emitted even when raw causes contain secrets."""
    request = create_autospec(sdk.request_bytes, spec_set=True,
                              return_value=sdk.Response(URL, 200, 'application/zip', b'body'))
    handler = create_autospec(sdk.signal.signal, spec_set=True)
    if kind == 'availability':
        request.side_effect = sdk.AvailabilityError(sdk.Reason.DNS)
    if kind == 'approval':
        request.side_effect = sdk.WheelError('SDK approval_required')
    monkeypatch.setattr(sdk, 'request_bytes', request)
    monkeypatch.setattr(sdk.signal, 'signal', handler)
    monkeypatch.setattr(sys, 'argv', [
        'worker', 'invalid' if kind == 'invalid' else 'request', str(ROOT), URL])
    sdk.worker_main()
    value = json.loads(capsys.readouterr().out)
    if kind == 'request':
        assert value == {'url': URL, 'status': 200, 'media_type': 'application/zip',
                         'data': 'Ym9keQ=='}
    else:
        expected = ('approval_required' if kind == 'approval' else 'validation_failed')
        assert value == ({'availability': 'dns_unavailable'} if kind == 'availability' else
                         {'error': expected})


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_worker_entry_local_only_reserve(mode, monkeypatch, capsys):
    """Reserve recovery is an isolated local read, not another official attempt."""
    monkeypatch.setattr(sdk.signal, 'signal', create_autospec(sdk.signal.signal, spec_set=True))
    monkeypatch.setattr(sys, 'argv', ['worker', 'reserve', str(ROOT), mode])
    sdk.worker_main()
    value = json.loads(capsys.readouterr().out)
    result = sdk.decode_selection(value, sdk.load_vendored_wheel(ROOT))
    assert result.reason == (sdk.Reason.TIMEOUT if mode == 'auto' else sdk.Reason.EXPLICIT_VENDORED)


@pytest.mark.parametrize('field,value', [
    ('source', 'unknown'), ('reason', []), ('wheel', None), ('wheel', '%%%%'),
    ('wheel', 'abc\u00e9'),
    ('wheel', base64.b64encode(b'x' * (sdk.MAX_WHEEL_BYTES + 1)).decode()),
    ('wheel', base64.b64encode(b'x' * sdk.MAX_WHEEL_BYTES + b'xy').decode()),
    ('wheel', 'YmFk'), ('index_sha256', 'bad'),
])
def test_selection_wire_mutations(field, value):
    """Malformed JSON fields and mutated bytes cannot produce a validated wheel."""
    reserve = sdk.load_vendored_wheel(ROOT)
    item = sdk.encode_selection(sdk.Selection(sdk.Source.VENDORED, sdk.Reason.TIMEOUT,
                                              reserve, None))
    item[field] = value
    with pytest.raises(sdk.WheelError):
        sdk.decode_selection(item, reserve)


def test_source_reason_wire_consistency():
    """Official source requires its known reason and independently hashed bytes."""
    reserve = sdk.load_vendored_wheel(ROOT)
    for result in [sdk.Selection(sdk.Source.OFFICIAL, sdk.Reason.TIMEOUT, reserve, None),
                   sdk.Selection(sdk.Source.VENDORED, sdk.Reason.OFFICIAL_AVAILABLE, reserve, None),
                   sdk.Selection(sdk.Source.VENDORED, sdk.Reason.VERSION_MISSING,
                                 reserve, sdk.SDK_HASH)]:
        with pytest.raises(sdk.WheelError):
            sdk.decode_selection(sdk.encode_selection(result), reserve)


@pytest.mark.parametrize('action,timeout', [('invalid', 1), ('select', 0), ('select', 61)])
def test_worker_budget_guard(action, timeout):
    """Bad budgets/actions cannot launch a subprocess."""
    with pytest.raises(sdk_worker.WorkerError):
        sdk_worker.run_worker(action, ROOT, 'auto', timeout=timeout)


@pytest.mark.parametrize('phase', [None, 'reserve_validation', 'index_validation',
                                   'wheel_validation'])
def test_outer_timeout_outside_transport_is_fatal(monkeypatch, phase):
    """Time spent validating local/index/wheel data cannot trigger reserve fallback."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True,
                          side_effect=sdk_worker.WorkerTimeout(phase))
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.WheelError, match='acquisition deadline'):
        sdk.acquire_sdk(ROOT)
    assert run.call_count == 1


@pytest.mark.parametrize('mode', ['auto', 'vendored'])
def test_download_timeout_preserves_approval_digest(monkeypatch, mode):
    """Outer cancellation preserves the probed pair for parent-side rechecking."""
    reserve = sdk.load_vendored_wheel(ROOT)
    result = sdk.Selection(sdk.Source.VENDORED, sdk.Reason.EXPLICIT_VENDORED if mode == 'vendored'
                           else sdk.Reason.TIMEOUT, reserve, None)
    run = create_autospec(sdk_worker.run_worker, spec_set=True, side_effect=[
        sdk_worker.WorkerTimeout('wheel_request', '1' * 64), sdk.encode_selection(result)])
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.WheelError, match='approval_required'):
        sdk.acquire_sdk(ROOT, mode)
    assert run.call_count == 2


@pytest.mark.parametrize('body', [
    index(anchors='<a href="/files/t_tech_investments-1.51.0.tar.gz">'
                  't_tech_investments-1.51.0.tar.gz</a>'),
    index(anchors='<a href="/files/t-tech-investments-1.51.0.zip">'
                  't-tech-investments-1.51.0.zip</a>'),
])
def test_sdist_only_index_has_no_supported_wheel(body):
    """Valid sdists do not become a download or an implicit source build."""
    port = transport(body=body)
    assert sdk.select_source(ROOT, 'auto', port).reason == sdk.Reason.VERSION_MISSING
    assert port.fetch.call_count == 1


@pytest.mark.parametrize('action', ['select', 'request'])
def test_worker_force_kill_request_and_group(tmp_path, monkeypatch, action):
    """Request-local and whole-stage supervisors both force termination when needed."""
    marker = tmp_path / 'pid'
    code = ('import signal,os,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(120)')
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', code])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    with pytest.raises(sdk_worker.WorkerTimeout):
        sdk_worker.run_worker(action, ROOT, 'auto', timeout=0.25, temp_root=tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.parametrize('value', [
    None, {}, {'phase': 'request', 'index_sha256': None},
    {'phase': 'wheel_request', 'index_sha256': None},
    {'phase': 'index_request', 'index_sha256': sdk.SDK_HASH},
    {'phase': 'wheel_request', 'index_sha256': 'bad'},
])
def test_timeout_phase_evidence_is_bounded_json(tmp_path, monkeypatch, value):
    """An untrusted phase marker never grants a timeout fallback."""
    code = ('import pathlib,time; '
            f'pathlib.Path("phase.json").write_text({json.dumps(value)!r}); time.sleep(120)')
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', code])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    with pytest.raises(sdk_worker.WorkerError):
        sdk_worker.run_worker('select', ROOT, 'auto', timeout=0.2, temp_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_unknown_mode_does_not_start_worker(monkeypatch):
    """Mode spelling is checked before subprocess effects."""
    run = create_autospec(sdk_worker.run_worker, spec_set=True)
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    with pytest.raises(sdk.WheelError):
        sdk.acquire_sdk(ROOT, 'other')
    run.assert_not_called()


def test_atomic_phase_evidence_tracks_validated_bytes(tmp_path, monkeypatch):
    """Only fixed stage/hash evidence survives, and the atomic staging file is cleaned."""
    monkeypatch.chdir(tmp_path)
    result = sdk.select_source(ROOT, 'auto', transport(), sdk_worker.publish_phase)
    assert result.source == sdk.Source.OFFICIAL
    assert json.loads((tmp_path / 'phase.json').read_text()) == {
        'phase': 'wheel_validation', 'index_sha256': sdk.SDK_HASH}
    assert [path.name for path in tmp_path.iterdir()] == ['phase.json']


def test_worker_reports_approval_error_safely():
    """The worker approval-required state crosses the wire without raw error strings."""
    with pytest.raises(sdk.WheelError, match='approval_required'):
        sdk.decode_selection({'error': 'approval_required'}, sdk.load_vendored_wheel(ROOT))


@pytest.mark.parametrize('reason', [
    'invalid SDK TLS', 'invalid SDK transport', 'invalid SDK HTTP status',
    'invalid SDK response URL', 'invalid SDK response size', 'invalid SDK official URL',
    'invalid SDK index media type', 'invalid SDK index format', 'invalid SDK index document',
    'invalid SDK index filename', 'invalid SDK index wheel', 'invalid SDK ambiguous index wheel',
    'invalid SDK wheel size', 'invalid SDK wheel hash', 'invalid SDK wheel structure',
    'invalid SDK metadata format', 'invalid SDK metadata identity',
    'invalid SDK license hash/size', 'invalid SDK local files',
    'invalid SDK worker result', 'invalid SDK worker phase',
])
def test_worker_safe_failure_survives_nested_handoff(monkeypatch, capsys, reason):
    """Known fatal reasons survive request, selection and parent without raw causes."""
    error = sdk.WheelError(reason)
    error.__cause__ = RuntimeError('untrusted upstream details')
    request = create_autospec(sdk.request_bytes, spec_set=True, side_effect=error)
    monkeypatch.setattr(sdk, 'request_bytes', request)
    monkeypatch.setattr(sdk.signal, 'signal', create_autospec(sdk.signal.signal, spec_set=True))
    monkeypatch.setattr(sys, 'argv', ['worker', 'request', str(ROOT), sdk.SIMPLE_URL])
    sdk.worker_main()
    request_output = capsys.readouterr()
    value = json.loads(request_output.out)
    assert value == {'error': reason}
    assert request_output.err == ''
    request.assert_called_once_with(sdk.SIMPLE_URL)

    run = create_autospec(sdk_worker.run_worker, spec_set=True, return_value=value)
    monkeypatch.setattr(sdk_worker, 'run_worker', run)
    monkeypatch.setattr(sdk_worker, 'publish_phase', create_autospec(
        sdk_worker.publish_phase, spec_set=True))
    monkeypatch.setattr(sys, 'argv', ['worker', 'select', str(ROOT), 'auto'])
    sdk.worker_main()
    selection_output = capsys.readouterr()
    value = json.loads(selection_output.out)
    assert value == {'error': reason}
    assert selection_output.err == ''
    run.assert_called_once_with('request', ROOT, sdk.SIMPLE_URL,
                                timeout=15, temp_root=Path.cwd())
    with pytest.raises(sdk.WheelError) as caught:
        sdk.decode_selection(value, sdk.load_vendored_wheel(ROOT))
    assert str(caught.value) == reason


@pytest.mark.parametrize('error_type', [sdk.WheelError, sdk_worker.WorkerError])
def test_worker_unknown_failure_stays_private(monkeypatch, capsys, error_type):
    """An arbitrary exception message cannot become a diagnostic wire value."""
    request = create_autospec(sdk.request_bytes, spec_set=True,
                              side_effect=error_type('invalid SDK TLS: untrusted details'))
    monkeypatch.setattr(sdk, 'request_bytes', request)
    monkeypatch.setattr(sdk.signal, 'signal', create_autospec(sdk.signal.signal, spec_set=True))
    monkeypatch.setattr(sys, 'argv', ['worker', 'request', str(ROOT), sdk.SIMPLE_URL])
    sdk.worker_main()
    output = capsys.readouterr()
    assert json.loads(output.out) == {'error': 'validation_failed'}
    assert output.err == ''


@pytest.mark.parametrize('reason', [None, [], {}, 1, 'invalid SDK TLS: untrusted details'])
def test_worker_unknown_wire_reason_stays_generic(reason):
    """Only exact allowlisted strings may reach the installer's diagnostic message."""
    with pytest.raises(sdk.WheelError) as caught:
        sdk.decode_selection({'error': reason}, sdk.load_vendored_wheel(ROOT))
    assert str(caught.value) == 'invalid SDK acquisition worker'


@pytest.mark.parametrize('payload', ['not-json', 'x' * 257])
def test_unreadable_timeout_phase_is_fatal(tmp_path, monkeypatch, payload):
    """Corrupt or oversized stage evidence cannot turn validation into availability."""
    code = ('import pathlib,time; '
            f'pathlib.Path("phase.json").write_text({payload!r}); time.sleep(120)')
    command = create_autospec(sdk_worker.worker_command, spec_set=True,
                              return_value=[sys.executable, '-I', '-S', '-c', code])
    monkeypatch.setattr(sdk_worker, 'worker_command', command)
    with pytest.raises(sdk_worker.WorkerError):
        sdk_worker.run_worker('select', ROOT, 'auto', timeout=0.2, temp_root=tmp_path)
    assert not list(tmp_path.iterdir())
