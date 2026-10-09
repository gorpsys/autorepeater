"""Credential-bearing HTTP calls are bounded and never expose a provider error body."""
from http.client import HTTPResponse
from io import BytesIO
import json
from pathlib import Path
import runpy
from unittest.mock import create_autospec
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector, Request

import pytest

from scripts import deploy_support as support


def transport(monkeypatch, body=b'{"ok": true}'):
    """No unrestricted mock or network client constructor is involved."""
    opener = create_autospec(OpenerDirector, instance=True, spec_set=True)
    response = create_autospec(HTTPResponse, instance=True, spec_set=True)
    response.__enter__.return_value = response
    response.read.return_value = body
    opener.open.return_value = response
    factory = create_autospec(support.build_opener, spec_set=True, return_value=opener)
    monkeypatch.setattr(support, 'build_opener', factory)
    return factory, opener, response


def test_json_http_has_finite_timeout_and_size(monkeypatch):
    """Credential-bearing transport is bounded and refuses redirects."""
    factory, opener, response = transport(monkeypatch)
    request = Request('https://auth.yandex.cloud/oauth/token')
    assert support.request_json(request) == {'ok': True}
    opener.open.assert_called_once_with(request, timeout=15)
    response.read.assert_called_once_with(support.MAX_RESPONSE_BYTES + 1)
    assert isinstance(factory.call_args.args[0], support.NoRedirect)
    redirect = factory.call_args.args[0].redirect_request
    assert redirect(request, None, 302, '', {}, 'https://evil') is None


@pytest.mark.parametrize('failure', ['http', 'transport', 'json', 'unicode', 'size'])
def test_http_failures_are_fixed_safe_messages(monkeypatch, failure):
    """Provider bodies and exception strings never become diagnostics."""
    _, opener, response = transport(monkeypatch)
    secret = 'should-never-appear-in-a-diagnostic'
    if failure == 'http':
        opener.open.side_effect = HTTPError('https://example', 403, secret, None, None)
    elif failure == 'transport':
        opener.open.side_effect = URLError(secret)
    elif failure == 'json':
        response.read.return_value = secret.encode()
    elif failure == 'unicode':
        response.read.return_value = b'\xff'
    else:
        response.read.return_value = b'x' * (support.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(support.DeployError) as error:
        support.request_json(Request('https://auth.yandex.cloud/oauth/token'))
    assert secret not in str(error.value)
    assert error.value.__suppress_context__ or failure == 'size'


@pytest.mark.parametrize('value', [None, [], {'ok': 1, 2: 'bad'}])
def test_only_json_objects_are_accepted(value):
    """Boundary narrowing does not coerce invalid producer data."""
    with pytest.raises(support.DeployError):
        support.record(value)


@pytest.mark.parametrize('value', [None, '', False, 1, 'has space', 'newline\n'])
def test_identifiers_cannot_inject_workflow_outputs(value):
    """Output identities must be nonempty single-line strings."""
    with pytest.raises(support.DeployError):
        support.text(value, 'ID')


@pytest.mark.parametrize('value', ['0' * 40, 'a' * 39, 'A' * 40, 'master'])
def test_sha_is_not_a_mutable_ref_or_deleted_sentinel(value):
    """Deployment references must be full immutable commit hashes."""
    with pytest.raises(support.DeployError):
        support.commit_sha(value)


@pytest.mark.parametrize('script', ['deploy_gate.py', 'yandex_deploy.py'])
def test_script_entrypoint_rejects_untrusted_launch(monkeypatch, capsys, script):
    """Direct script invocation still obeys the master-only boundary."""
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/untrusted')
    monkeypatch.setattr('sys.argv', [script])
    path = Path(__file__).resolve().parents[1] / 'scripts' / script
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(path), run_name='__main__')
    assert error.value.code == 1
    assert 'Deployment' in capsys.readouterr().err


@pytest.mark.parametrize('code,expected', [
    ('invalid_grant', '; oauth=invalid_grant'),
    ('invalid_request', '; oauth=invalid_request'),
    ('private-token', ''),
])
def test_http_error_exposes_only_allowlisted_oauth_code(monkeypatch, code, expected):
    """Provider descriptions and arbitrary error values must remain private."""
    _, opener, _ = transport(monkeypatch)
    body = json.dumps({'error': code, 'error_description': 'private-token'}).encode()
    opener.open.side_effect = HTTPError(
        'https://auth.yandex.cloud', 400, 'private-token', None, BytesIO(body))
    with pytest.raises(support.DeployError) as error:
        support.request_json(Request('https://auth.yandex.cloud/oauth/token'))
    assert str(error.value) == 'HTTP request failed (status 400)' + expected


@pytest.mark.parametrize('body', [b'invalid JSON', b'[]', b'x' * 4097])
def test_unreadable_oauth_error_body_keeps_status_only(body):
    """Malformed and oversized provider bodies never escape diagnostics."""
    error = HTTPError('https://auth.yandex.cloud', 400, 'private', None, BytesIO(body))
    assert not support.oauth_error_code(error)


def test_oauth_error_body_read_failure_is_not_replaced_by_unsafe_exception(monkeypatch):
    """A broken error stream still preserves the original safe status diagnostic."""
    error = HTTPError('https://auth.yandex.cloud', 400, 'private', None, BytesIO())
    reader = create_autospec(error.read, spec_set=True, side_effect=OSError('private-token'))
    monkeypatch.setattr(error, 'read', reader)
    assert not support.oauth_error_code(error)
    reader.assert_called_once_with(4097)
