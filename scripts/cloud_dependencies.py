"""Prepare a locked CPython 3.12/manylinux payload and verify it before publication."""
from dataclasses import dataclass, field
from email.parser import BytesParser
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

from scripts import install_dependencies as installer
from scripts import package_wheels, runtime_lock, sdk_wheel


MANIFEST = 'dependency-manifest.json'
SUPPLEMENTAL_LICENSES = {
    'antlr4-python3-runtime': {
        'version': '4.13.2', 'expression': 'BSD-3-Clause',
        'repository': 'https://github.com/antlr/antlr4',
        'commit': 'cc82115a4e7f53d71d9d905caa2c2dfa4da58899', 'filename': 'LICENSE.txt',
        'sha256': '3db1fb3ee79a4b4f9918fc4d0f6133bf18a3cf787f126cd22f8aa9b862281c0c',
    },
    'grpcio-tools': {
        'version': '1.81.1', 'expression': 'Apache-2.0',
        'repository': 'https://github.com/grpc/grpc',
        'commit': 'e84a8a2f04095f2772ba42a4abccde4f9243e75b', 'filename': 'LICENSE',
        'sha256': '58c86a3f2b5cca0ebdf9ed3e7f4e6dc38e3b38796d8c3dc92064c62a4e39d0dd',
    },
}
SMOKE_IMPORTS = (
    'setuptools', 'main', 'handler', 't_tech.invest', 'grpc', 'grpc._cython.cygrpc',
    'google.protobuf', 'google.protobuf.message', 'grpc_tools._protoc_compiler',
    'iprotopy', 'mypy_protobuf', 'antlr4', 'certifi', 'requests', 'pythonjsonlogger',
)


class PayloadError(ValueError):
    """Fixed safe payload failure, without subprocess output or credentials."""


@dataclass
class Staging:
    """Owned temporary locations and the exact verified files passed between pip stages."""
    wheelhouse: Path
    downloads: Path
    target: Path
    hashes: dict[str, str] = field(default_factory=dict)
    expected: dict[str, bytes] = field(default_factory=dict)

    @property
    def requirements(self) -> Path:
        """Hash input is build-only and never enters the root payload."""
        return self.wheelhouse / 'runtime.txt'


def require(condition: bool, reason: str) -> None:
    """Enforce the publication contract with safe diagnostics."""
    if not condition:
        raise PayloadError(reason)


def check_platform() -> None:
    """Reject unsupported hosts before SDK probing or package acquisition."""
    require(sys.platform == 'linux' and platform.machine().lower() in ('x86_64', 'amd64')
            and sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12),
            'unsupported offline platform; CPython 3.12 Linux x86_64 required')
    libc, version = platform.libc_ver()
    require(libc == 'glibc' and re.fullmatch(r'\d+\.\d+', version) is not None,
            'unsupported offline libc')
    require(tuple(int(n) for n in version.split('.')) >= (2, 17),
            'unsupported offline glibc version')


def safe_path(name: str) -> bool:
    """Shared safe payload-member policy for builders and ready-bundle checks."""
    parts = name.split('/')
    return bool(name) and ':' not in name and '\\' not in name and all(
        p and p not in ('.', '..', '.idea', '__pycache__', '.venv')
        and not p.startswith('.env') for p in parts) and not any(
            ord(c) < 32 or ord(c) == 127 for c in name)


def check_paths(files: dict[str, bytes], application: dict[str, bytes]) -> None:
    """Reject dependency collisions with the application before copying files."""
    roots = {name.split('/')[0] for name in application} | {
        'autorepeater', 'main.py', 'handler.py', 'requirements.txt',
        'requirements-runtime.txt', MANIFEST, 'THIRD_PARTY_NOTICES',
    }
    require(all(safe_path(name) and name.split('/')[0] not in roots for name in files),
            'dependency path collision or reserved path')
    require(not any('/'.join(n.split('/')[:i]) in files
                    for n in files for i in range(1, len(n.split('/')))),
            'dependency file path collision')


def read_target(target: Path) -> dict[str, bytes]:
    """Read only safe regular files from an owned installed target."""
    files: dict[str, bytes] = {}
    for directory, directories, names in os.walk(target, followlinks=False):
        root = Path(directory)
        for name in directories + names:
            path = root / name
            relative = path.relative_to(target).as_posix()
            require(safe_path(relative) and not path.is_symlink(), 'unsafe payload path')
        for name in names:
            path = root / name
            require(path.is_file(), 'unsafe payload file type')
            files[path.relative_to(target).as_posix()] = path.read_bytes()
    return files


def verify_graph(document: dict[str, Any], files: dict[str, bytes]) -> None:
    """Verify top-level payload metadata against the exact locked runtime closure."""
    installed = []
    for name, data in files.items():
        if name.count('/') == 1 and name.endswith('.dist-info/METADATA'):
            metadata = BytesParser().parsebytes(data)
            require(not metadata.defects, 'invalid payload metadata')
            installed.append({'metadata': {'name': metadata['Name'],
                                           'version': metadata['Version'],
                                           'requires_dist': metadata.get_all('Requires-Dist', [])}})
    try:
        runtime_lock.check_installed_graph(document, {
            'version': '1', 'environment': runtime_lock.MARKER_ENVIRONMENT,
            'installed': installed}, profile='cloud')
    except runtime_lock.LockError:
        raise PayloadError('payload runtime graph drift') from None


def verify_target(document: dict[str, Any], files: dict[str, bytes],
                  expected: dict[str, bytes]) -> None:
    """Verify exact graph and preserved source/data/metadata/licence bytes."""
    verify_graph(document, files)
    require(all(files.get(name) == data for name, data in expected.items()
                if not name.endswith('.dist-info/RECORD')),
            'payload source/data/metadata/licence drift')
    # pip may create entrypoints and bookkeeping, but cannot add unowned runtime modules.
    additions = set(files) - set(expected)
    require(all(name.startswith('bin/') or name.endswith((
        '.dist-info/INSTALLER', '.dist-info/REQUESTED', '.dist-info/direct_url.json'))
                for name in additions), 'unexpected payload file')


def _supplemental_license(root: Path, package: dict[str, Any], entry: dict[str, Any]) -> bytes:
    """Use the original release licence for the known wheel omission, after identity checks."""
    policy = SUPPLEMENTAL_LICENSES[package['name']]
    evidence = (policy['repository'].replace('github.com', 'raw.githubusercontent.com')
                + '/' + policy['commit'] + '/' + policy['filename']
                + ' SHA256 ' + policy['sha256'])
    require(package['version'] == policy['version']
            and entry['license']['expression'] == policy['expression']
            and entry['source']['url'] == policy['repository']
            and entry['source']['commit'] == policy['commit']
            and evidence in entry['license']['evidence'],
            'supplemental licence source identity drift')
    path = root / 'third_party' / (package['name'] + '-' + policy['version']) / policy['filename']
    require(not any(p.is_symlink() for p in (path, *path.parents)) and path.is_file(),
            'supplemental licence file unavailable or unsafe')
    with path.open('rb') as stream:
        data = stream.read(65537)
    require(sha256(data).hexdigest() == policy['sha256'], 'supplemental licence hash drift')
    return data


def _license_notice(package: dict[str, Any], entry: dict[str, Any],
                    files: dict[str, bytes], root: Path) -> tuple[bytes, dict[str, Any]]:
    licence = entry['license']
    source = entry['source']['url'] + '/tree/' + entry['source']['commit']
    directory = f'{package["name"].replace("-", "_")}-{package["version"]}.dist-info/'
    materials = [n for n in files if n.startswith(directory) and any(
        token in Path(n).name.lower() for token in ('license', 'licence', 'copying', 'notice'))]
    iprotopy = package['name'] == 'iprotopy'
    require(licence['status'] in ('allowed', 'conditional') and bool(licence['evidence'])
            and (licence['fulfilled'] or iprotopy), 'unresolved payload licence')
    facts = dict(licence, standalone_license_present=bool(materials))
    if iprotopy:
        metadata = BytesParser().parsebytes(files[directory + 'METADATA'])
        require(package['version'] == '0.3.0' and licence['expression'] == 'MIT'
                and entry['source']['commit'] == '031ae0a6536f07964c9802dfe099efeb701e1c25'
                and metadata.get_all('License') == ['MIT']
                and metadata.get_all('Author') == ['Ruslan Sirazhetdinov'],
                'iprotopy notice-only identity drift')
        facts['policy'] = 'user_notice_only'
        text = f'Author: Ruslan Sirazhetdinov\nVersion: {package["version"]}\nSource: {source}\n'
    else:
        supplement = (_supplemental_license(root, package, entry)
                      if package['name'] in SUPPLEMENTAL_LICENSES and not materials else b'')
        require(bool(supplement) or bool(materials) and all(files[n] for n in materials),
                f'missing payload licence materials: {package["name"]}=={package["version"]}')
        text = (f'Package: {package["name"]}\nVersion: {package["version"]}\n'
                f'License: {licence["expression"]}\nSource: {source}\n')
        if supplement:
            text += '\n' + supplement.decode('ascii')
            materials.append('THIRD_PARTY_NOTICES/' + package['name'])
            facts['supplemental_license_sha256'] = sha256(supplement).hexdigest()
            facts['supplemental_license_source'] = (
                entry['source']['url'] + '/blob/' + entry['source']['commit'] + '/'
                + SUPPLEMENTAL_LICENSES[package['name']]['filename'])
    if package['name'] == 'certifi':
        require('certifi/core.py' in files and 'certifi/cacert.pem' in files,
                'missing certifi source form')
        text += 'Source form: unchanged certifi/*.py and certifi/cacert.pem in this payload.\n'
    return text.encode(), {'name': package['name'], 'version': package['version'],
                           'source': source, 'license': facts, 'license_files': materials}


def payload_notices(document: dict[str, Any], root: Path,
                    files: dict[str, bytes]) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """One notice policy serves payload construction and ready-bundle verification."""
    inventory = runtime_lock.runtime_inventory(root)
    notices: dict[str, bytes] = {}
    packages = []
    for package in document['packages']:
        notice, facts = _license_notice(package, inventory[package['name']], files, root)
        notices['THIRD_PARTY_NOTICES/' + package['name']] = notice
        packages.append(facts)
    return notices, packages


def _requirements(document: dict[str, Any], hashes: dict[str, str]) -> str:
    return ''.join(f'{p["name"]}=={p["version"]} --hash=sha256:{hashes[p["name"]]}\n'
                   for p in document['packages'])


def _download(root: Path, document: dict[str, Any], staging: Staging,
              budget: installer.Budget) -> None:
    staging.requirements.write_text(_requirements(document, staging.hashes), encoding='ascii')
    installer.pip(['download', '--no-index', '--find-links', str(staging.wheelhouse),
                   '--dest', str(staging.downloads), '--no-deps', '--require-hashes',
                   '--only-binary=:all:', '--platform', 'manylinux2014_x86_64',
                   '--platform', 'manylinux_2_17_x86_64', '--python-version', '3.12',
                   '--implementation', 'cp', '--abi', 'cp312', '--abi', 'abi3', '--abi', 'none',
                   '--timeout', '15', '--retries', '0', '-r', str(staging.requirements)],
                  root, budget)
    require({p.name for p in staging.downloads.iterdir()} == {
        p.name for p in staging.wheelhouse.glob('*.whl')}, 'downloaded wheel closure drift')
    require(package_wheels.payload_files(document, staging.downloads) == staging.expected,
            'downloaded wheel content drift')


def _install(root: Path, document: dict[str, Any], staging: Staging,
             application: dict[str, bytes], facts: dict[str, Any]) -> dict[str, bytes]:
    budget = installer.Budget(300)
    installer.pip(['install', '--no-index', '--find-links', str(staging.downloads),
                   '--target', str(staging.target), '--ignore-installed', '--no-compile',
                   '--only-binary=:all:', '--require-hashes', '--timeout', '15', '--retries', '0',
                   '-r', str(staging.requirements)], root, budget)
    report = installer.pip(['inspect', '--path', str(staging.target)], root, budget)
    try:
        runtime_lock.check_installed_graph(document, json.loads(report), profile='cloud',
                                           artifact_hashes=staging.hashes)
    except json.JSONDecodeError:
        raise PayloadError('invalid payload inspect report') from None
    files = read_target(staging.target)
    check_paths(files, application)
    verify_target(document, files, staging.expected)
    # Publish only the authenticated wheel projection, excluding pip-generated entrypoints.
    files = dict(staging.expected)
    facts = dict(facts, payload_files=package_wheels.file_hashes(files))
    notices, packages = payload_notices(document, root, files)
    require(facts['payload_files'] == package_wheels.locked_files(root, document, staging.hashes),
            'payload file lock content/membership drift')
    for package in packages:
        locked = next(p for p in document['packages'] if p['name'] == package['name'])
        package['artifact'] = next(dict(a) for a in locked['artifacts']
                                   if a['sha256'] == staging.hashes[package['name']])
    files.update(notices)
    files[MANIFEST] = runtime_lock.canonical_bytes({
        **facts, 'sdk_artifact_sha256': staging.hashes[sdk_wheel.NAME], 'packages': packages})
    for name, data in {**files, **application}.items():
        path = staging.target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    smoke_payload(staging.target, timeout=min(60, budget.remaining()))
    budget.remaining()
    return files


def prepare_payload(root: Path, application: dict[str, bytes], *,
                    use_vendored_sdk: bool = False) -> dict[str, bytes]:
    """Acquire/install into private targets; return only checked owned root payload bytes."""
    check_platform()
    document = runtime_lock.load_lock(root)
    acquisition = installer.Budget(300)
    selected = sdk_wheel.acquire_sdk(root, 'vendored' if use_vendored_sdk else 'auto')
    acquisition.remaining()
    package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    runtime_lock.require(any(a['sha256'] == selected.wheel.manifest.sha256
                             and a['size'] == len(selected.wheel.data)
                             and a['filename'] == selected.wheel.manifest.identity.filename
                             for a in package['artifacts']), 'SDK artifact outside runtime lock')
    installer.check_wheel_metadata(package, selected.wheel.data)
    with TemporaryDirectory(prefix='cloud-wheels-') as wheels, \
            TemporaryDirectory(prefix='cloud-download-') as downloaded, \
            TemporaryDirectory(prefix='cloud-target-') as installed:
        staging = Staging(Path(wheels), Path(downloaded), Path(installed))
        staging.hashes = installer.prepare_wheels(
            root, document, staging.wheelhouse, acquisition, selected)
        facts = installer.receipt(document, selected.source.value, staging.hashes)
        staging.expected = package_wheels.payload_files(document, staging.wheelhouse)
        check_paths(staging.expected, application)
        _download(root, document, staging, acquisition)
        return _install(root, document, staging, application, facts)


def smoke_payload(target: Path, *, timeout: float = 60) -> None:
    """Import only stdlib and the unpacked payload; never construct clients or call RPC."""
    require(0 < timeout <= 60, 'invalid payload smoke deadline')
    code = '''import importlib,json,pathlib,socket,sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
def blocked(*args, **kwargs):
    raise RuntimeError('network forbidden during import')
socket.create_connection = blocked
socket.socket.connect = blocked
try:
    for name in json.loads(sys.argv[2]):
        importlib.import_module(name)
    stdlib = pathlib.Path(sys.base_prefix) / 'lib' / ('python%d.%d' % sys.version_info[:2])
    for module in tuple(sys.modules.values()):
        if module is None:
            continue
        origin = getattr(module, '__file__', None)
        paths = ([origin] if origin else []) + list(getattr(module, '__path__', []))
        for value in paths:
            path = pathlib.Path(value).resolve()
            if not (path.is_relative_to(root) or path.is_relative_to(stdlib)):
                raise RuntimeError('foreign import origin')
            if 'site-packages' in path.parts or 'dist-packages' in path.parts:
                raise RuntimeError('foreign site packages')
    for name in json.loads(sys.argv[2]):
        module = sys.modules[name]
        paths = ([module.__file__] if getattr(module, '__file__', None) else
                 list(getattr(module, '__path__', [])))
        if not paths or not all(pathlib.Path(p).resolve().is_relative_to(root) for p in paths):
            raise RuntimeError('payload import origin')
    print(json.dumps({'status': 'ok'}))
except Exception:
    sys.exit(1)
'''
    try:
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code,
                                 str(target.resolve()), json.dumps(SMOKE_IMPORTS)],
                                cwd=target, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'},
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PayloadError('payload import unavailable or deadline exceeded') from None
    require(result.returncode == 0 and result.stdout == b'{"status": "ok"}\n'
            and len(result.stderr) <= installer.MAX_OUTPUT,
            'payload import failure or foreign origin')
