"""Install owned verified wheels, check the locked graph and attest Make's interpreter."""
import argparse
from dataclasses import dataclass
import importlib.metadata
import importlib.util
from email.parser import BytesParser
from io import BytesIO
import json
import marshal
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile

from scripts import package_wheels, runtime_lock, sdk_wheel
from scripts.sdk_worker import WorkerError
from scripts.vendor_inventory import InventoryError, validate_inventory


RECEIPT = '.autorepeater-runtime-receipt.json'
MAX_OUTPUT = 8 * 1024 * 1024
MAX_INSTALLED_ENTRIES = 50000


class InstallError(ValueError):
    """Fixed safe installer/receipt error without subprocess output."""


@dataclass
class Budget:
    """A single deadline shared by acquisition, validation and all pip calls."""
    seconds: float

    def __post_init__(self) -> None:
        self.deadline = time.monotonic() + self.seconds

    def remaining(self) -> float:
        """Never reset the helper's budget per package or pip stage."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise InstallError('setup deadline exceeded')
        return left


def pip(arguments: list[str], root: Path, budget: Budget) -> str:
    """Run supported pip commands with explicit policy and a common deadline."""
    try:
        result = subprocess.run([sys.executable, '-m', 'pip', '--isolated',
                                 '--disable-pip-version-check', '--no-input', *arguments],
                                cwd=root, env=runtime_lock.pip_environment(),
                                capture_output=True, text=True, timeout=budget.remaining(),
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError('pip unavailable or setup deadline exceeded') from None
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > MAX_OUTPUT:
        raise InstallError('pip failed; check dependencies in a clean environment')
    budget.remaining()
    return result.stdout


def graph(document: dict[str, Any], root: Path, budget: Budget,
          hashes: dict[str, str] | None = None) -> None:
    """pip evaluates dependency expressions/markers; lock checks exact metadata."""
    report = pip(['inspect', '--local'], root, budget)
    try:
        runtime_lock.check_installed_graph(document, json.loads(report), profile='development',
                                           artifact_hashes=hashes)
    except json.JSONDecodeError:
        raise InstallError('invalid pip inspect report') from None
    pip(['check'], root, budget)


def receipt(document: dict[str, Any], sdk_source: str,
            hashes: dict[str, str]) -> dict[str, Any]:
    """Attest only known acquisition hashes for the exact canonical runtime graph."""
    if sdk_source not in ('official', 'vendored'):
        raise InstallError('invalid receipt source')
    expected = {p['name']: p for p in document['packages']}
    if set(hashes) != set(expected) or any(hashes[n] not in {
            a['sha256'] for a in p['artifacts']} for n, p in expected.items()):
        raise InstallError('invalid receipt artifacts')
    if (sdk_source == 'vendored' and
            hashes[sdk_wheel.NAME] != expected[sdk_wheel.NAME]['artifacts'][0]['sha256']):
        raise InstallError('invalid receipt SDK source hash')
    return {'schema_version': 1, 'lock_sha256': runtime_lock.canonical_digest(document),
            'sdk_source': sdk_source, 'artifact_hashes': hashes}


def write_receipt(path: Path, facts: dict[str, Any]) -> None:
    """Atomically publish success only after installation and all graph checks."""
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise InstallError('invalid receipt path')
    with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', prefix='runtime-receipt-',
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(facts, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_receipt(path: Path, document: dict[str, Any]) -> None:
    """Missing/stale/malformed receipts require verified setup in this interpreter."""
    try:
        value = runtime_lock.read_json(path)
        expected = receipt(document, value['sdk_source'], value['artifact_hashes'])
        files = value['installed_files']
        members = value['installed_members']
        if (not isinstance(value['schema_version'], int)
                or isinstance(value['schema_version'], bool) or value['schema_version'] != 2):
            raise InstallError('runtime receipt schema invalid; run verified setup')
        if not isinstance(files, dict) or not files or not isinstance(members, dict):
            raise InstallError('runtime receipt schema invalid; run verified setup')
        if (set(members) != {p['name'] for p in document['packages']}
                or any(not isinstance(names, list) or names != sorted(set(names))
                       for names in members.values())
                or set(files) != {n for names in members.values() for n in names}):
            raise InstallError('runtime receipt membership stale; run verified setup')
        if value != {**expected, 'schema_version': 2, 'installed_files': files,
                     'installed_members': members}:
            raise InstallError('runtime receipt stale; run verified setup')
        _check_installed_file_facts(path.parent, files)
    except (OSError, KeyError, TypeError, ValueError):
        raise InstallError('runtime receipt missing or stale; run verified setup') from None


def _check_installed_file_facts(prefix: Path, files: dict[str, Any]) -> None:
    for name, facts in files.items():
        if (not package_wheels._path(name) or not isinstance(facts, dict)  # pylint: disable=protected-access
                or set(facts) != {'size', 'sha256'}):
            raise InstallError('runtime receipt file facts invalid')
        if (not isinstance(facts['size'], int) or isinstance(facts['size'], bool)
                or not 0 <= facts['size'] <= 512 * 1024 * 1024
                or not runtime_lock.digest(facts['sha256'])):
            raise InstallError('runtime receipt file facts invalid')
        data = sdk_wheel._read_file(prefix, name, facts['size'],  # pylint: disable=protected-access
                                    allow_empty=True)
        if package_wheels.file_hashes({name: data})[name] != facts:
            raise InstallError('runtime receipt file content drift')


def _installed_file(prefix: Path, distribution: importlib.metadata.Distribution,
                    entry: importlib.metadata.PackagePath) -> tuple[str, bytes]:
    if any(p.startswith('.env') or p == '.idea' for p in entry.parts):
        raise InstallError('runtime installed file path unsafe')
    path = Path(str(distribution.locate_file(entry)))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise InstallError('runtime installed file path unsafe')
    try:
        installed = path.resolve().relative_to(prefix).as_posix()
    except ValueError:
        raise InstallError('runtime installed file outside interpreter') from None
    data = sdk_wheel._read_file(prefix, installed, 512 * 1024 * 1024,  # pylint: disable=protected-access
                                allow_empty=True)
    return installed, data


def _directory_descriptor(path: Path) -> int:
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if not package_wheels._path(part):  # pylint: disable=protected-access
                raise InstallError('runtime installed file path unsafe')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _cache_paths(sources: set[str]) -> set[str]:
    return {importlib.util.cache_from_source(name, optimization=optimization)
            for name in sources if name.endswith('.py')
            for optimization in ('', '1', '2')}


def _check_directory_members(root: Path, names: set[str], allowed: set[str]) -> None:
    """Walk real entries using nofollow descriptors; RECORD only supplies expected membership."""
    pending = [(name, 1) for name in names]
    count = 0
    while pending:
        name, depth = pending.pop()
        count += 1
        if count > MAX_INSTALLED_ENTRIES or depth > 64 or not package_wheels._path(name):  # pylint: disable=protected-access
            raise InstallError('runtime installed file enumeration limits/path')
        parent = _directory_descriptor(root / Path(name).parent)
        try:
            info = os.stat(Path(name).name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(Path(name).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
                try:
                    with os.scandir(child) as entries:
                        for entry in entries:
                            if count + len(pending) >= MAX_INSTALLED_ENTRIES:
                                raise InstallError('runtime installed file enumeration limits/path')
                            pending.append((name + '/' + entry.name, depth + 1))
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode) or name not in allowed:
                raise InstallError('runtime installed file membership/type drift')
        finally:
            os.close(parent)


def _development_members(root: Path, owned: set[str], distributions: list[
        importlib.metadata.Distribution]) -> set[str]:
    """Permit recorded, separate dev branches of shared namespaces, never SDK extensions."""
    allowed: set[str] = set()
    branches = {'/'.join(name.split('/')[:2]).split('.', 1)[0] for name in owned}
    for distribution in distributions:
        if Path(str(distribution.locate_file(''))).absolute() != root:
            continue
        entries = distribution.files or []
        if len(entries) > MAX_INSTALLED_ENTRIES:
            raise InstallError('runtime installed file enumeration limits/path')
        for entry in entries:
            name = str(entry)
            if not package_wheels._path(name):  # pylint: disable=protected-access
                continue
            parts = name.split('/')
            if len(parts) < 3 or parts[0] == 't_tech':
                continue
            branch = '/'.join(parts[:2]).split('.', 1)[0]
            if (branch not in branches and parts[0] + '/__init__.py' not in owned
                    and parts[0] + '/__init__.pyc' not in owned):
                allowed.add(name)
    return allowed


def _owned_roots(prefix: Path, distributions: list[importlib.metadata.Distribution],
                 members: dict[str, list[str]]) -> dict[Path, set[str]]:
    roots: dict[Path, set[str]] = {}
    for distribution in distributions:
        name = runtime_lock.normalized_name(distribution.metadata['Name'])
        if name in members:
            root = Path(str(distribution.locate_file(''))).absolute()
            try:
                relative_root = root.relative_to(prefix)
            except ValueError:
                raise InstallError('runtime installed file root outside interpreter') from None
            roots.setdefault(root, set()).update(
                Path(path).relative_to(relative_root).as_posix()
                for path in members[name] if Path(path).is_relative_to(relative_root))
    return roots


def _root_entries(root: Path, owned: set[str]) -> set[str]:
    top = {name.split('/')[0] for name in owned}
    stems = {name.split('.')[0] for name in top if not name.endswith('.pth')}
    descriptor = _directory_descriptor(root)
    actual: set[str] = set()
    try:
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_INSTALLED_ENTRIES:
                    raise InstallError('runtime installed file enumeration limits/path')
                if entry.name in top or entry.name.split('.')[0] in stems:
                    actual.add(entry.name)
                elif entry.name == '__pycache__' and any(n.endswith('.py') for n in top):
                    cache = _directory_descriptor(root / '__pycache__')
                    try:
                        with os.scandir(cache) as cached:
                            for index, item in enumerate(cached):
                                if index >= MAX_INSTALLED_ENTRIES:
                                    raise InstallError(
                                        'runtime installed file enumeration limits/path')
                                if item.name.split('.')[0] in stems:
                                    actual.add('__pycache__/' + item.name)
                    finally:
                        os.close(cache)
    finally:
        os.close(descriptor)
    return actual


def _check_actual_members(prefix: Path, distributions: list[
        importlib.metadata.Distribution], members: dict[str, list[str]], names: set[str]) -> None:
    if set(members) != names:
        raise InstallError('runtime installed file membership missing')
    try:
        roots = _owned_roots(prefix, distributions, members)
        for root, owned in roots.items():
            allowed = owned | _development_members(root, owned, distributions)
            allowed |= _cache_paths(allowed)
            _check_directory_members(root, _root_entries(root, owned), allowed)
    except (OSError, sdk_wheel.WheelError, ValueError) as error:
        if isinstance(error, InstallError):
            raise
        raise InstallError('runtime installed file enumeration unavailable') from None


def runtime_snapshot(document: dict[str, Any]) -> tuple[
        dict[str, bytes], dict[str, list[str]], dict[str, bytes]]:
    """Capture installed wheel membership and bytes inside this interpreter's prefix."""
    files: dict[str, bytes] = {}
    members: dict[str, list[str]] = {}
    payload: dict[str, bytes] = {}
    prefix = Path(sys.prefix)
    total = 0
    names = {p['name'] for p in document['packages']}
    distributions = list(importlib.metadata.distributions())
    for distribution in distributions:
        name = runtime_lock.normalized_name(distribution.metadata['Name'])
        if name not in names:
            continue
        if name in members or distribution.files is None:
            raise InstallError('runtime installed file membership unavailable')
        members[name] = []
        for entry in distribution.files:
            relative = str(entry)
            if '__pycache__' in entry.parts or entry.suffix == '.pyc':
                continue
            installed, data = _installed_file(prefix, distribution, entry)
            total += len(data)
            if len(files) >= MAX_INSTALLED_ENTRIES or total > 512 * 1024 * 1024:
                raise InstallError('runtime installed file enumeration limits/path')
            if installed in files:
                raise InstallError('runtime installed file collision')
            files[installed] = data
            members[name].append(installed)
            if package_wheels._path(relative):  # pylint: disable=protected-access
                payload[relative] = data
        members[name].sort()
    _check_actual_members(prefix, distributions, members, names)
    return files, members, payload


def _compiled_cache(source: bytes, path: Path, optimization: str) -> bytes:
    """Bound compilation of authenticated source using CPython's import semantics."""
    if len(source) > MAX_OUTPUT:
        raise InstallError('runtime cache compilation limits')
    code = compile(source, str(path), 'exec', dont_inherit=True,
                   optimize=int(optimization or 0))
    expected = marshal.dumps(code)
    if len(expected) > 4 * MAX_OUTPUT:
        raise InstallError('runtime cache compilation limits')
    return expected


def _check_runtime_caches(prefix: Path, files: dict[str, bytes]) -> None:
    """Compare opaque cache bytes to trusted compilation; never deserialize cached code."""
    budget = Budget(30)
    try:
        for name, source in files.items():
            if not name.endswith('.py'):
                continue
            path = prefix / name
            for optimization in ('', '1', '2'):
                cache = Path(importlib.util.cache_from_source(
                    str(path), optimization=optimization))
                try:
                    cache.lstat()
                except FileNotFoundError:
                    continue
                budget.remaining()
                expected = _compiled_cache(source, path, optimization)
                data = sdk_wheel._read_file(  # pylint: disable=protected-access
                    prefix, cache.relative_to(prefix).as_posix(), len(expected) + 16,
                    allow_empty=True)
                timestamp = ((int(path.stat(follow_symlinks=False).st_mtime)
                              & 0xffffffff).to_bytes(4, 'little')
                             + (len(source) & 0xffffffff).to_bytes(4, 'little'))
                flags = int.from_bytes(data[4:8], 'little')
                header = (importlib.util.source_hash(source) if flags in (1, 3)
                          else timestamp)
                if (data[:4] != importlib.util.MAGIC_NUMBER or flags not in (0, 1, 3)
                        or data[8:16] != header or data[16:] != expected):
                    raise InstallError('runtime cache content drift')
                budget.remaining()
    except (OSError, SyntaxError, ValueError) as error:
        if isinstance(error, InstallError):
            raise
        raise InstallError('runtime cache unavailable or invalid') from None


def attest_environment(root: Path, document: dict[str, Any], facts: dict[str, Any],
                       expected: dict[str, bytes]) -> dict[str, Any]:
    """Issue file evidence only after matching the acquired wheels and independent file lock."""
    files, members, payload = runtime_snapshot(document)
    package_wheels.verify_file_hashes(payload, package_wheels.file_hashes(expected))
    package_wheels.verify_file_hashes(
        payload, package_wheels.locked_files(root, document, facts['artifact_hashes']))
    _check_runtime_caches(Path(sys.prefix), files)
    return {**facts, 'schema_version': 2, 'installed_files': package_wheels.file_hashes(files),
            'installed_members': members}


def check_environment(root: Path) -> None:
    """Offline Make guard reads distribution metadata without importing the SDK."""
    runtime_lock.check_host()
    document = runtime_lock.load_lock(root)
    check_receipt(Path(sys.prefix) / RECEIPT, document)
    installed = [{'metadata': {'name': d.metadata['Name'], 'version': d.version,
                               'requires_dist': d.requires or []}}
                 for d in importlib.metadata.distributions()]
    runtime_lock.check_installed_graph(document, {
        'version': '1', 'environment': runtime_lock.MARKER_ENVIRONMENT,
        'installed': installed}, profile='development')
    files, members, payload = runtime_snapshot(document)
    value = runtime_lock.read_json(Path(sys.prefix) / RECEIPT)
    if members != value['installed_members'] or package_wheels.file_hashes(files) != value[
            'installed_files']:
        raise InstallError('runtime receipt installed membership drift; run verified setup')
    package_wheels.verify_file_hashes(
        payload, package_wheels.locked_files(root, document, value['artifact_hashes']))
    _check_runtime_caches(Path(sys.prefix), files)
    pip(['check'], root, Budget(30))


def check_inputs(root: Path, document: dict[str, Any]) -> None:
    """Reject manually drifted requirements before network or interpreter changes."""
    for name, text in runtime_lock.render_requirements(document).items():
        if (root / name).read_text(encoding='utf-8') != text:
            raise InstallError('generated requirements drift')


def check_wheel_metadata(package: dict[str, Any], data: bytes) -> None:
    """Compare opaque Requires-Dist to the reviewed resolver metadata before effects."""
    path = f'{package["name"].replace("-", "_")}-{package["version"]}.dist-info/METADATA'
    with zipfile.ZipFile(BytesIO(data)) as archive:
        metadata = BytesParser().parsebytes(archive.read(path))
    if sorted(set(metadata.get_all('Requires-Dist', []))) != package['requires_dist']:
        raise InstallError('runtime wheel dependency metadata drift')


def prepare_wheels(root: Path, document: dict[str, Any], wheelhouse: Path, budget: Budget,
                   sdk: sdk_wheel.Selection) -> dict[str, str]:
    """Validate all acquired artifacts while their immutable bytes remain owned."""
    entries = validate_inventory(runtime_lock.read_json(root / 'vendor/inventory.json'))
    transport = package_wheels.WorkerTransport()
    hashes = {sdk_wheel.NAME: sdk.wheel.manifest.sha256}
    (wheelhouse / sdk.wheel.manifest.identity.filename).write_bytes(sdk.wheel.data)
    for item in package_wheels.runtime_artifacts(document):
        if item.name != sdk_wheel.NAME:
            acquired = package_wheels.acquire(item, root, entries, transport,
                                              timeout=budget.remaining())
            check_wheel_metadata(next(p for p in document['packages']
                                      if p['name'] == item.name), acquired)
            (wheelhouse / item.filename).write_bytes(acquired)
            hashes[item.name] = item.sha256
    return hashes


def prepare_development(root: Path, wheelhouse: Path, budget: Budget, runtime: set[str]) -> None:
    """Acquire the reviewed tools without changing any locked runtime selection."""
    entries = validate_inventory(runtime_lock.read_json(root / 'vendor/inventory.json'))
    transport = package_wheels.WorkerTransport()
    for item in package_wheels.development_artifacts(entries, runtime):
        acquired = package_wheels.acquire(item, root, entries, transport,
                                          timeout=budget.remaining())
        (wheelhouse / item.filename).write_bytes(acquired)
    constraints = ''.join(f'{item.name}=={item.version}\n'
                          for item in package_wheels.development_artifacts(entries, runtime))
    (wheelhouse / 'development-constraints.txt').write_text(constraints, encoding='ascii')


def preflight(root: Path, wheelhouse: Path, budget: Budget, dev_args: list[str]) -> None:
    """Ask pip to solve retained packages together with every proposed local wheel."""
    try:
        report = json.loads(pip(['inspect', '--local'], root, budget))
        retained = runtime_lock.record(report).get('installed')
        if not isinstance(retained, list):
            raise InstallError('invalid preflight installed graph')
        planned = {path.name.split('-')[0].replace('_', '-') for path in wheelhouse.glob('*.whl')}
        requirements = []
        for item in retained:
            metadata = runtime_lock.record(runtime_lock.record(item).get('metadata'))
            name = runtime_lock.normalized_name(metadata.get('name'))
            version = metadata.get('version')
            if not isinstance(version, str) or not version or any(
                    c.isspace() or c in '/\\@;' for c in version):
                raise InstallError('invalid preflight installed version')
            if name not in planned:
                requirements.append(name + '==' + version)
    except json.JSONDecodeError:
        raise InstallError('invalid preflight installed graph') from None
    pip(['install', '--dry-run', '--no-index', '--only-binary=:all:',
         '--timeout', '15', '--retries', '0', *dev_args,
         *[str(path) for path in sorted(wheelhouse.glob('*.whl'))], *requirements], root, budget)


def update_file_lock(root: Path) -> dict[str, Any]:
    """Explicit package-only regeneration; never infer identity from installed site-packages."""
    runtime_lock.check_host()
    document = runtime_lock.load_lock(root)
    budget = Budget(300)
    selected = sdk_wheel.acquire_sdk(root)
    package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    runtime_lock.require(any(a['sha256'] == selected.wheel.manifest.sha256 and
                             a['size'] == len(selected.wheel.data) for a in package['artifacts']),
                         'SDK artifact outside runtime lock')
    with tempfile.TemporaryDirectory(prefix='runtime-file-lock-') as directory:
        wheelhouse = Path(directory)
        prepare_wheels(root, document, wheelhouse, budget, selected)
        reserve = (selected.wheel if selected.wheel.manifest.sha256 ==
                   package['artifacts'][0]['sha256']
                   else sdk_wheel.load_vendored_wheel(root))
        for package in document['packages']:
            for artifact in package['artifacts']:
                target = wheelhouse / artifact['sha256']
                target.mkdir(exist_ok=True)
                if package['name'] == sdk_wheel.NAME:
                    wheel = (selected.wheel if artifact['sha256'] == selected.wheel.manifest.sha256
                             else reserve)
                    runtime_lock.require(wheel.manifest.sha256 == artifact['sha256']
                                         and len(wheel.data) == artifact['size'],
                                         'SDK artifact unavailable for file lock')
                    sdk_wheel.check_approval(reserve.manifest, wheel.manifest.sha256)
                    check_wheel_metadata(package, wheel.data)
                    data = wheel.data
                elif artifact == package['artifacts'][0]:
                    data = package_wheels._staged_bytes(  # pylint: disable=protected-access
                        wheelhouse, artifact['filename'], artifact['size'])
                else:
                    entries = validate_inventory(runtime_lock.read_json(
                        root / 'vendor/inventory.json'))
                    data = package_wheels.acquire(
                        package_wheels.artifact_for({**package, 'artifacts': [artifact]}),
                        root, entries, package_wheels.WorkerTransport(), timeout=budget.remaining())
                    check_wheel_metadata(package, data)
                (target / artifact['filename']).write_bytes(data)
        result = package_wheels.file_lock(document, wheelhouse)
        budget.remaining()
    return result


def install(root: Path, *, development: bool = False, sdk_source: str = 'auto') -> None:
    """Prepare/validate the full graph before the first install; never delete foreign packages."""
    budget = Budget(300)
    runtime_lock.check_host()
    document = runtime_lock.load_lock(root)
    check_inputs(root, document)
    # Existing dependency conflicts are diagnostic, not grounds to remove foreign packages.
    pip(['check'], root, budget)
    if budget.remaining() < 60:
        raise InstallError('insufficient remaining SDK acquisition budget')
    sdk = sdk_wheel.acquire_sdk(root, sdk_source)
    budget.remaining()
    sdk_package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    runtime_lock.require(any(a['sha256'] == sdk.wheel.manifest.sha256 and
                             a['size'] == len(sdk.wheel.data) for a in sdk_package['artifacts']),
                         'SDK artifact outside runtime lock')
    check_wheel_metadata(sdk_package, sdk.wheel.data)
    receipt_path = Path(sys.prefix) / RECEIPT
    with tempfile.TemporaryDirectory(prefix='verified-install-') as directory:
        wheelhouse = Path(directory)
        hashes = prepare_wheels(root, document, wheelhouse, budget, sdk)
        if development:
            prepare_development(root, wheelhouse, budget, set(hashes))
        budget.remaining()
        # Validate the entire local dev solver result before effects, under runtime constraints.
        dev_args = ['install', '--no-index', '--find-links', str(wheelhouse),
                    '--timeout', '15', '--retries', '0', '--only-binary=:all:',
                    '-c', str(root / 'requirements-runtime-constraints.txt'),
                    '-c', str(wheelhouse / 'development-constraints.txt'),
                    '-r', str(root / 'requirements-dev-tools.txt')]
        preflight(root, wheelhouse, budget, dev_args[1:] if development else [])
        facts = receipt(document, sdk.source.value, hashes)
        expected = package_wheels.payload_files(document, wheelhouse)
        package_wheels.verify_file_hashes(
            expected, package_wheels.locked_files(root, document, hashes))
        runtime_files = [str(wheelhouse / a.filename)
                         for a in package_wheels.runtime_artifacts(document)]
        receipt_path.unlink(missing_ok=True)
        try:
            pip(['install', '--no-index', '--no-deps', '--force-reinstall',
                 '--timeout', '15', '--retries', '0', *runtime_files], root, budget)
            graph(document, root, budget, hashes)
            if development:
                pip(dev_args, root, budget)
                graph(document, root, budget, hashes)
            facts = attest_environment(root, document, facts, expected)
            budget.remaining()
            write_receipt(receipt_path, facts)
        except BaseException:
            receipt_path.unlink(missing_ok=True)
            raise


def main(arguments: list[str] | None = None) -> int:
    """Bootstrap requires only stdlib and this checkout's stdlib helpers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--sdk-source', choices=['auto', 'vendored'], default='auto')
    parser.add_argument('--write-file-lock', action='store_true')
    args = parser.parse_args(arguments)
    try:
        if args.write_file_lock:
            facts = update_file_lock(args.project_root)
            (args.project_root / package_wheels.FILE_LOCK).write_text(
                json.dumps(facts, indent=2, sort_keys=True) + '\n', encoding='ascii')
            print('verified runtime file lock written')
            return 0
        install(args.project_root, development=args.development, sdk_source=args.sdk_source)
    except (InstallError, runtime_lock.LockError, sdk_wheel.WheelError, WorkerError,
            package_wheels.PackageError, InventoryError, OSError) as error:
        reason = 'local files unavailable' if isinstance(error, OSError) else str(error)
        print(f'verified setup failed: {reason}; no success receipt issued', file=sys.stderr)
        return 1
    print('verified runtime installed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
