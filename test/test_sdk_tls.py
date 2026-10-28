"""Offline checks of SDK-only trust-chain construction from pinned public data."""
from io import BytesIO
from email.message import Message
from types import SimpleNamespace
import ssl
from unittest.mock import create_autospec
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector
from urllib.response import addinfourl
import zipfile

import pytest

from scripts import sdk_wheel as sdk


@pytest.fixture(name='tls_ports')
def fixture_tls_ports(monkeypatch):
    """No real CA bytes, sockets or downloads are needed to check trust provenance."""
    root, issuer = b'pinned SDK root', b'pinned issuer'
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr('t_tech/invest/certs/RussianTrustedRootCA.pem', root)
    reserve = create_autospec(sdk.load_vendored_wheel, spec_set=True,
                              return_value=SimpleNamespace(data=archive.getvalue()))
    monkeypatch.setattr(sdk, 'load_vendored_wheel', reserve)
    response = addinfourl(BytesIO(issuer), Message(), 'http://issuer.example/issuer.crt', code=200)
    port = create_autospec(OpenerDirector, instance=True, spec_set=True)
    port.open.return_value = response
    build = create_autospec(sdk.build_opener, spec_set=True, return_value=port)
    context = create_autospec(ssl.SSLContext, instance=True, spec_set=True)
    default = create_autospec(ssl.create_default_context, spec_set=True, return_value=context)
    monkeypatch.setattr(sdk, 'build_opener', build)
    monkeypatch.setattr(sdk.ssl, 'create_default_context', default)
    monkeypatch.setattr(sdk, 'SDK_ISSUER_URL', 'http://issuer.example/issuer.crt')
    monkeypatch.setattr(sdk, 'SDK_ISSUER_SHA256', sdk.sha256(issuer).hexdigest())
    return SimpleNamespace(root=root, issuer=issuer, reserve=reserve, response=response,
                           port=port, build=build, context=context, default=default)


def test_sdk_tls_context_uses_verified_wheel_and_pinned_issuer(tls_ports):
    """Root authenticity precedes issuer download; defaults remain enabled."""
    assert sdk.sdk_tls_context() is tls_ports.context
    tls_ports.reserve.assert_called_once_with(sdk.Path(sdk.__file__).resolve().parents[1])
    tls_ports.port.open.assert_called_once_with(sdk.SDK_ISSUER_URL, timeout=5)
    tls_ports.default.assert_called_once_with()
    tls_ports.context.load_verify_locations.assert_called_once_with(
        cadata='pinned SDK root\npinned issuer')
    assert tls_ports.response.closed
    handlers = tls_ports.build.call_args.args
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], sdk.NoRedirect)


def test_sdk_tls_certificates_use_selected_project_without_mutating_trust(tls_ports, tmp_path):
    """Cloud packaging reuses the same verified bytes and the requested reserve."""
    assert sdk.sdk_tls_certificates(tmp_path) == 'pinned SDK root\npinned issuer'
    tls_ports.reserve.assert_called_once_with(tmp_path)
    tls_ports.default.assert_not_called()


@pytest.mark.parametrize('failure', ['hash', 'url', 'size', 'encoding'])
def test_sdk_tls_context_rejects_unpinned_or_malformed_issuer(tls_ports, failure, monkeypatch):
    """No transport-provided certificate may become a new trust anchor."""
    body = {'hash': b'changed', 'url': tls_ports.issuer,
            'size': b'x' * 32769, 'encoding': b'\xff'}[failure]
    if failure in ('size', 'encoding'):
        monkeypatch.setattr(sdk, 'SDK_ISSUER_SHA256', sdk.sha256(body).hexdigest())
    response = addinfourl(
        BytesIO(body), Message(),
        'http://unexpected.example/' if failure == 'url' else sdk.SDK_ISSUER_URL, code=200)
    tls_ports.port.open.return_value = response
    with pytest.raises(sdk.WheelError, match='^invalid SDK TLS$'):
        sdk.sdk_tls_context()
    assert response.closed
    tls_ports.context.load_verify_locations.assert_not_called()


@pytest.mark.parametrize('error', [
    URLError('untrusted diagnostic'), TimeoutError('untrusted'),
    HTTPError('http://issuer.example/', 301, 'untrusted', {}, BytesIO(b'untrusted response')),
])
def test_sdk_tls_context_download_failure_is_fatal_and_safe(tls_ports, error):
    """Issuer failure cannot be mistaken for permission to bypass TLS."""
    tls_ports.port.open.side_effect = error
    with pytest.raises(sdk.WheelError, match='^invalid SDK TLS$'):
        sdk.sdk_tls_context()
    tls_ports.context.load_verify_locations.assert_not_called()
    if isinstance(error, HTTPError):
        assert error.fp.closed


def test_sdk_tls_context_does_not_download_if_wheel_integrity_fails(tls_ports):
    """A corrupt reserve cannot supply trusted root data."""
    tls_ports.reserve.side_effect = sdk.WheelError('invalid SDK wheel hash')
    with pytest.raises(sdk.WheelError, match='^invalid SDK wheel hash$'):
        sdk.sdk_tls_context()
    tls_ports.port.open.assert_not_called()


def test_sdk_tls_context_rejects_ssl_load_error(tls_ports):
    """Trust-store parser errors retain a fixed noncredential diagnostic."""
    tls_ports.context.load_verify_locations.side_effect = ssl.SSLError('untrusted')
    with pytest.raises(sdk.WheelError, match='^invalid SDK TLS$'):
        sdk.sdk_tls_context()
