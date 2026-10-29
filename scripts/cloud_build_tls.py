"""Prepare pinned public CA data for YC's default dependency installer."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import ssl
from zipfile import ZipFile

from scripts import package_wheels, runtime_lock, sdk_wheel


def prepare(root: Path) -> dict[str, bytes]:
    """Keep normal PyPI trust and complete T-Bank's chain without shipping its SDK."""
    document = runtime_lock.load_lock(root)
    package = next(p for p in document['packages'] if p['name'] == 'certifi')
    artifact = package_wheels.artifact_for(package)
    data = package_wheels.acquire(artifact, root, [], package_wheels.WorkerTransport(), timeout=15)
    with ZipFile(BytesIO(data)) as wheel:
        mozilla = wheel.read('certifi/cacert.pem')
        license_bytes = wheel.read(f'certifi-{package["version"]}.dist-info/licenses/LICENSE')
    chain = sdk_wheel.sdk_tls_certificates(root).encode('ascii')
    bundle = mozilla.rstrip() + b'\n' + chain.rstrip() + b'\n'
    ssl.create_default_context(cadata=bundle.decode('ascii'))
    manifest = runtime_lock.canonical_bytes({
        'bundle_sha256': sha256(bundle).hexdigest(),
        'certifi_artifact_sha256': artifact.sha256,
        'sdk_wheel_sha256': sdk_wheel.SDK_HASH,
        'issuer_url': sdk_wheel.SDK_ISSUER_URL,
        'issuer_sha256': sdk_wheel.SDK_ISSUER_SHA256,
    })
    return {
        'pip3.12': Path(__file__).with_name('yandex-pip3.12.sh').read_bytes(),
        'cloud-ca/ca-bundle.pem': bundle,
        'cloud-ca/manifest.json': manifest,
        'cloud-ca/LICENSE.certifi': license_bytes,
    }
