"""Stdlib canonical runtime policy, pip exports and installed metadata checks."""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.sdk_wheel import INDEX_URL, NAME, MAX_WHEEL_BYTES, load_vendored_wheel, check_approval
from scripts.vendor_inventory import validate_inventory


TARGET = {'implementation': 'cpython', 'python': '3.12', 'sys_platform': 'linux',
          'machine': 'x86_64', 'platforms': ['manylinux2014_x86_64', 'manylinux_2_17_x86_64'],
          'abis': ['cp312', 'abi3', 'none']}
SOURCES = {'pypi': 'https://pypi.org/simple', 'sdk_index': INDEX_URL}
ROOTS = [NAME, 'python-json-logger']
MARKER_ENVIRONMENT = {'implementation_name': 'cpython', 'python_version': '3.12',
                      'sys_platform': 'linux', 'platform_machine': 'x86_64',
                      'platform_system': 'Linux'}
DEV_TOOLS = ('pytest\npytest-cov==7.1.0\ndiff-cover==10.6.0\npylint\nflake8\nmypy==1.18.2\n'
             'pip==26.2.2\n')


class LockError(ValueError):
    """Safe policy/metadata failure with no raw URLs or subprocess output."""


def require(condition: bool, reason: str) -> None:
    """Reject invalid facts before producing exports or installing anything."""
    if not condition:
        raise LockError(reason)


def record(value: object, fields: set[str] | None = None) -> dict[str, Any]:
    """Validate JSON objects before consuming their fields."""
    require(isinstance(value, dict), 'invalid object')
    assert isinstance(value, dict)
    require(all(isinstance(key, str) for key in value), 'invalid object keys')
    require(fields is None or set(value) == fields, 'invalid schema fields')
    return value


def strings(value: object) -> list[str]:
    """Require bounded, nonempty string elements; preserve marker expressions."""
    require(isinstance(value, list), 'invalid string list')
    assert isinstance(value, list)
    require(len(value) <= 1024 and all(isinstance(s, str) and 0 < len(s) <= 8192
                                       for s in value), 'invalid string list')
    return value


def digest(value: object) -> bool:
    """Only lower-case SHA-256 identities are accepted."""
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def normalized_name(value: object) -> str:
    """PEP 503 project name normalization, not a dependency expression parser."""
    require(isinstance(value, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', value)
            is not None, 'invalid package name')
    assert isinstance(value, str)
    return re.sub('[-_.]+', '-', value).lower()


def canonical_bytes(value: object) -> bytes:
    """Stable JSON serialization; floats/NaN are not runtime policy facts."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def canonical_digest(value: object) -> str:
    """Digest the entire validated policy, including its resolver evidence."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def report_digest(packages: list[dict[str, Any]]) -> str:
    """Portable public projection of the resolver's environment and metadata."""
    return canonical_digest({'environment': MARKER_ENVIRONMENT, 'packages': packages})


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    """Load bounded regular JSON files, rejecting symlink/duplicate fields."""
    require(not path.is_symlink() and path.is_file(), 'invalid JSON file')
    require(path.stat().st_size <= 4 * 1024 * 1024, 'JSON size limit')
    try:
        return record(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs))
    except (UnicodeError, json.JSONDecodeError):
        raise LockError('invalid JSON') from None


def runtime_inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Inventory supplies reviewed seed versions, artifacts and license caveats."""
    entries = validate_inventory(read_json(root / 'vendor/inventory.json'))
    return {p['name']: p for p in entries if 'runtime' in p['scopes']}


def compatible_tag(tag: str) -> bool:
    """Known cloud wheel ABI policy; dependency/marker evaluation belongs to pip."""
    parts = tag.split('-')
    if len(parts) != 3:
        return False
    python, abi, platforms = parts
    if platforms == 'any':
        return abi == 'none' and 'py3' in python.split('.')
    if not set(platforms.split('.')).issubset(set(TARGET['platforms'])):
        return False
    if python == 'cp312' and abi in ('cp312', 'abi3', 'none'):
        return True
    return abi == 'abi3' and python in {f'cp3{n}' for n in range(2, 13)}


def approved_artifacts(root: Path, item: dict[str, Any]) -> list[dict[str, Any]]:
    """SDK reserve is the sole initial hash; additional official hashes need approval."""
    artifacts = [{'filename': a['filename'], 'sha256': a['sha256'], 'size': a['size'],
                  'tag': a['tag'], 'url': a['url']}
                 for a in item['artifacts'] if a['role'] == 'wheel']
    if item['name'] == NAME:
        reserve = load_vendored_wheel(root)
        require(item['version'] == reserve.manifest.identity.version, 'SDK version drift')
        artifacts[0]['url'] = 'repo:vendor/t-tech-investments/' + artifacts[0]['filename']
        require(artifacts[0]['sha256'] == reserve.manifest.sha256, 'SDK reserve drift')
    return artifacts


def _sdk_artifacts(root: Path, value: object, reserve: list[dict[str, Any]]) -> None:
    require(isinstance(value, list) and bool(value) and value[0] == reserve[0], 'SDK reserve drift')
    assert isinstance(value, list)
    manifest = load_vendored_wheel(root).manifest
    hashes = {manifest.sha256}
    for raw in value[1:]:
        artifact = record(raw, {'filename', 'sha256', 'size', 'tag', 'url'})
        require(digest(artifact['sha256']) and artifact['sha256'] not in hashes,
                'SDK artifact hash drift')
        check_approval(manifest, artifact['sha256'])
        require(artifact['filename'] == manifest.identity.filename
                and artifact['tag'] == manifest.identity.tag
                and isinstance(artifact['size'], int) and not isinstance(artifact['size'], bool)
                and 0 < artifact['size'] <= MAX_WHEEL_BYTES
                and artifact['url'] == 'tbank:sha256:' + artifact['sha256'], 'SDK artifact drift')
        hashes.add(artifact['sha256'])


def validate_lock(value: object, root: Path) -> dict[str, Any]:
    """Validate a full reviewed closure, without inferring new versions or hashes."""
    document = record(value, {'schema_version', 'target', 'sources', 'roots',
                              'packages', 'report_sha256'})
    require(isinstance(document['schema_version'], int)
            and not isinstance(document['schema_version'], bool)
            and document['schema_version'] == 1,
            'unsupported schema')
    require(document['target'] == TARGET, 'unsupported target profile')
    require(document['sources'] == SOURCES and document['roots'] == ROOTS, 'source/root drift')
    require(digest(document['report_sha256']), 'invalid report digest')
    inventory = runtime_inventory(root)
    require(isinstance(document['packages'], list), 'invalid packages')
    names: list[str] = []
    for raw in document['packages']:
        package = record(raw, {'name', 'version', 'requires', 'requires_dist', 'artifacts'})
        name = normalized_name(package['name'])
        require(name == package['name'] and name in inventory and name not in names,
                'package identity/closure drift')
        names.append(name)
        seed = inventory[name]
        require(package['version'] == seed['version'], 'runtime version drift')
        require(package['requires'] == sorted(seed['requires']), 'dependency closure drift')
        strings(package['requires_dist'])
        require(package['requires_dist'] == sorted(set(package['requires_dist'])),
                'duplicate/unsorted metadata dependencies')
        artifacts = approved_artifacts(root, seed)
        if name == NAME:
            try:
                _sdk_artifacts(root, package['artifacts'], artifacts)
            except ValueError:
                raise LockError('SDK artifact/source approval drift') from None
        else:
            require(package['artifacts'] == artifacts, 'artifact/source drift')
        for artifact in package['artifacts']:
            require(compatible_tag(artifact['tag']), 'incompatible artifact target')
    require(names == sorted(inventory), 'missing/extra runtime packages')
    reachable = set(ROOTS)
    while True:
        expanded = reachable | {n for p in document['packages'] if p['name'] in reachable
                                for n in p['requires']}
        if expanded == reachable:
            break
        reachable = expanded
    require(reachable == set(names), 'incomplete/unreachable runtime closure')
    require(document['report_sha256'] == report_digest(document['packages']),
            'resolver metadata/report digest drift')
    return document


def load_lock(root: Path) -> dict[str, Any]:
    """Read and validate the checked-in runtime policy."""
    return validate_lock(read_json(root / 'runtime-lock.json'), root)


def render_requirements(document: dict[str, Any]) -> dict[str, str]:
    """One renderer owns pins, constraints, hashes, source policy and dev exports."""
    header = '# Generated from runtime-lock.json; use make update-runtime-lock.\n'
    pins = ''.join(f'{p["name"]}=={p["version"]}\n' for p in document['packages'])
    hashed = {p['name']: f'{p["name"]}=={p["version"]}' + ''.join(
        f' --hash=sha256:{a["sha256"]}' for a in p['artifacts']) + '\n'
              for p in document['packages']}
    source = (f'--index-url {document["sources"]["pypi"]}\n'
              f'--extra-index-url {document["sources"]["sdk_index"]}\n')
    return {
        'requirements.txt': header + source + hashed[NAME] + '-r requirements-runtime.txt\n',
        'requirements-runtime.txt': header + ''.join(v for k, v in hashed.items() if k != NAME),
        'requirements-runtime-pins.txt': header + pins,
        'requirements-runtime-constraints.txt': header + pins,
        'requirements-dev-tools.txt': header + DEV_TOOLS,
        'requirements-dev.txt': header + source + '-c requirements-runtime-constraints.txt\n'
        '-r requirements-runtime-pins.txt\n-r requirements-dev-tools.txt\n',
    }


def pip_environment() -> dict[str, str]:
    """Managed pip calls ignore user index/config overrides without reading them."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith('PIP_')}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1',
                       PIP_NO_INPUT='1')
    return environment


def check_host() -> None:
    """Marker facts must come from the actual advertised CPython/Linux profile."""
    require(sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 12)
            and sys.platform == 'linux' and platform.machine().lower() == 'x86_64',
            'unsupported target profile; additional profile requires review')
    libc, version = platform.libc_ver()
    require(libc == 'glibc' and re.fullmatch(r'\d+\.\d+', version) is not None,
            'unsupported libc')
    require(tuple(int(n) for n in version.split('.')) >= (2, 17), 'unsupported libc version')


def check_installed_graph(document: dict[str, Any], inspect: object, *,
                          profile: str, artifact_hashes: dict[str, str] | None = None) -> None:
    """Compare pip inspect metadata; optional hashes are verified acquisition facts.

    No version-only check writes a receipt or attests the installed wheel's source.
    The caller also runs pip check for dependency constraints/marker conflicts.
    """
    require(profile in ('development', 'cloud'), 'invalid installed profile')
    facts = record(inspect)
    require(facts.get('version') == '1' and isinstance(facts.get('installed'), list),
            'invalid inspect report')
    environment = record(facts.get('environment'))
    require(all(environment.get(k) == v for k, v in MARKER_ENVIRONMENT.items()),
            'installed target marker drift')
    installed: dict[str, dict[str, Any]] = {}
    for item in facts['installed']:
        metadata = record(record(item).get('metadata'))
        name = normalized_name(metadata.get('name'))
        require(name not in installed, 'duplicate installed distribution')
        installed[name] = metadata
    expected = {p['name'] for p in document['packages']}
    require(expected.issubset(installed), 'missing runtime package')
    require(profile != 'cloud' or set(installed) == expected, 'extra cloud package')
    for package in document['packages']:
        metadata = installed[package['name']]
        require(metadata.get('version') == package['version'], 'installed version drift')
        requirements = strings(metadata.get('requires_dist', []))
        require(sorted(set(requirements)) == package['requires_dist'], 'installed metadata drift')
        if artifact_hashes is not None:
            require(artifact_hashes.get(package['name']) in {
                a['sha256'] for a in package['artifacts']}, 'installed artifact drift')
    require(artifact_hashes is None or set(artifact_hashes) == expected,
            'artifact closure drift')


def inspect_interpreter(document: dict[str, Any], profile: str) -> None:
    """Supported pip inspect/check, bounded output; never remove foreign packages."""
    check_host()
    try:
        for arguments in (['inspect', '--local'], ['check']):
            result = subprocess.run([sys.executable, '-m', 'pip', *arguments],
                                    env=pip_environment(), capture_output=True,
                                    text=True, timeout=60, check=False)
            require(result.returncode == 0,
                    'installed dependency conflict; use a clean environment')
            if arguments[0] == 'inspect':
                require(len(result.stdout) <= 8 * 1024 * 1024, 'inspect size limit')
                check_installed_graph(document, json.loads(result.stdout), profile=profile)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise LockError('installed graph check unavailable') from None


def main(arguments: list[str] | None = None) -> int:
    """Check reviewed exports, optionally the current interpreter's runtime graph."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--installed', choices=['development', 'cloud'])
    args = parser.parse_args(arguments)
    try:
        document = load_lock(args.project_root)
        for name, text in render_requirements(document).items():
            require((args.project_root / name).read_text(encoding='utf-8') == text,
                    'generated requirements drift')
        if args.installed:
            inspect_interpreter(document, args.installed)
        print('runtime lock OK: ' + canonical_digest(document))
        return 0
    except (LockError, OSError) as error:
        print(str(error) if isinstance(error, LockError) else 'runtime lock file unavailable',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
