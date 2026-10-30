"""Default cloud pip receives pinned CA bytes without vendoring the SDK."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import ssl
import subprocess
import sys
from unittest.mock import create_autospec
from zipfile import ZipFile

import pytest

from scripts import cloud_build_tls
from scripts import package_wheels, runtime_lock, sdk_wheel


@pytest.fixture(name='ca_ports')
def fixture_ca_ports(monkeypatch):
    """Strict ports return synthetic public data; no certificates live in Git."""
    root = Path(__file__).resolve().parents[1]
    document = runtime_lock.load_lock(root)
    package = next(p for p in document['packages'] if p['name'] == 'certifi')
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('certifi/cacert.pem', b'mozilla roots\n')
        archive.writestr(f'certifi-{package["version"]}.dist-info/licenses/LICENSE', b'MPL license')
    acquire = create_autospec(package_wheels.acquire, spec_set=True, return_value=data.getvalue())
    chain = create_autospec(sdk_wheel.sdk_tls_certificates, spec_set=True,
                            return_value='SDK root\nissuer')
    context = create_autospec(ssl.SSLContext, instance=True, spec_set=True)
    create = create_autospec(ssl.create_default_context, spec_set=True, return_value=context)
    monkeypatch.setattr(package_wheels, 'acquire', acquire)
    monkeypatch.setattr(sdk_wheel, 'sdk_tls_certificates', chain)
    monkeypatch.setattr(cloud_build_tls.ssl, 'create_default_context', create)
    return root, document, package, acquire, chain, create


def test_default_cloud_ca_uses_locked_sources_and_preserves_pip_arguments(ca_ports):
    """The cloud archive has every root pip needs, but no wheel or trust bypass."""
    root, _document, package, acquire, chain, create = ca_ports
    files = cloud_build_tls.prepare(root)
    assert set(files) == {'pip3.12', 'cloud-ca/ca-bundle.pem', 'cloud-ca/manifest.json',
                          'cloud-ca/LICENSE.certifi'}
    bundle = files['cloud-ca/ca-bundle.pem']
    assert bundle == b'mozilla roots\nSDK root\nissuer\n'
    acquire.assert_called_once()
    args = acquire.call_args
    assert args.args[:3] == (package_wheels.artifact_for(package), root, [])
    assert isinstance(args.args[3], package_wheels.WorkerTransport)
    assert args.kwargs == {'timeout': 15}
    chain.assert_called_once_with(root)
    create.assert_called_once_with(cadata=bundle.decode('ascii'))
    manifest = json.loads(files['cloud-ca/manifest.json'])
    assert manifest['bundle_sha256'] == sha256(bundle).hexdigest()
    assert manifest['certifi_artifact_sha256'] == package['artifacts'][0]['sha256']
    assert manifest['sdk_wheel_sha256'] == sdk_wheel.SDK_HASH
    assert manifest['issuer_sha256'] == sdk_wheel.SDK_ISSUER_SHA256
    assert files['cloud-ca/LICENSE.certifi'] == b'MPL license'
    wrapper = files['pip3.12'].decode()
    assert 'exec python3.12 -m pip --cert "$PIP_CERT" "$@"' in wrapper
    assert 'trusted-host' not in wrapper and 'insecure' not in wrapper


def test_default_ca_acquisition_failure_is_fatal(ca_ports):
    """Unverified or unavailable input must not produce a partial trust bundle."""
    root, _document, _package, acquire, _chain, _create = ca_ports
    acquire.side_effect = package_wheels.PackageError('package hash mismatch')
    with pytest.raises(package_wheels.PackageError, match='package hash mismatch'):
        cloud_build_tls.prepare(root)


@pytest.mark.parametrize('result_code', [0, 19])
def test_cloud_pip_wrapper_forwards_arguments_and_exit_code(tmp_path, result_code):
    """Exercise the shipped shell and hash check without real CA bytes or installers."""
    root = tmp_path / 'cloud code'
    root.mkdir()
    ca = root / 'cloud-ca'
    ca.mkdir()
    bundle = b'public CA test double'
    (ca / 'ca-bundle.pem').write_bytes(bundle)
    (ca / 'manifest.json').write_text(json.dumps({'bundle_sha256': sha256(bundle).hexdigest()}))
    wrapper = root / 'pip3.12'
    wrapper.write_bytes(Path(cloud_build_tls.__file__).with_name('yandex-pip3.12.sh').read_bytes())
    wrapper.chmod(0o755)
    capture = tmp_path / 'arguments.json'
    python = root / 'python3.12'
    python.write_text(f'''#!{sys.executable}
import json, os, pathlib, ssl, sys
if sys.argv[1:3] == ['-m', 'pip']:
    pathlib.Path({str(capture)!r}).write_text(json.dumps({{
        'argv': sys.argv[3:],
        'env': {{k: os.environ[k] for k in ['PIP_CERT','REQUESTS_CA_BUNDLE','SSL_CERT_FILE']}}
    }}))
    sys.exit({result_code})
ssl.create_default_context = lambda **kwargs: None
sys.argv = sys.argv[1:]
exec(compile(sys.stdin.read(), '<wrapper-validation>', 'exec'))
''')
    python.chmod(0o755)
    arguments = ['install', '--target', 'path with spaces', '-r', 'requirements.txt',
                 '--literal=;$(not-executed)']
    result = subprocess.run(['pip3.12', *arguments], env={'PATH': str(root) + ':/usr/bin:/bin'},
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == result_code, result.stderr
    assert result.stdout.splitlines() == ['AUTOREPEATER_PIP_TLS: ENTER',
                                          'AUTOREPEATER_PIP_TLS: BUNDLE_OK',
                                          'AUTOREPEATER_PIP_TLS: EXEC_PIP']
    received = json.loads(capture.read_text())
    assert received['argv'] == ['--cert', str(ca / 'ca-bundle.pem'), *arguments]
    assert set(received['env'].values()) == {str(ca / 'ca-bundle.pem')}
    capture.unlink()
    (ca / 'ca-bundle.pem').write_bytes(b'changed')
    result = subprocess.run(['pip3.12', *arguments], env={'PATH': str(root) + ':/usr/bin:/bin'},
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode != 0
    assert 'cloud CA bundle hash mismatch' in result.stderr
    assert 'EXEC_PIP' not in result.stdout
    assert not capture.exists()
