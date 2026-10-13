"""Validate reviewed source/licence facts and the repository's vendor tree offline."""
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any
from urllib.parse import urlsplit


TARGET = 'cpython312-linux-x86_64'
SDK_IDENTITY = ('t-tech-investments', '1.51.0')
SDK_HASH = 'f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856'
SDK_OVERRIDE = 'explicit-user-sdk-1.51.0-recovered-wheel'
SDK_LICENSE_HASH = '5484c945bc04994e2080cd022e6df8ef3977230fcd65168846be50e0ecdfde89'
MAX_GIT_BYTES = 100_000_000
ENTRY_FIELDS = {'name', 'version', 'kind', 'scopes', 'requires', 'source', 'license',
                'artifacts', 'storage', 'reason'}
SOURCE_FIELDS = {'url', 'tag', 'commit', 'github', 'evidence'}
LICENSE_FIELDS = {'expression', 'status', 'evidence', 'obligations', 'fulfilled'}
ARTIFACT_FIELDS = {'filename', 'path', 'sha256', 'size', 'tag', 'role', 'url'}
PROVIDED_TOOLS = ['Python', 'Git', 'Make', 'GitHub Actions']


class InventoryError(ValueError):
    """Fixed diagnostics never echo credentials, remote responses or file contents."""


def _check(condition: bool, field: str) -> None:
    if not condition:
        raise InventoryError(f'invalid inventory {field}')


def _record(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError(f'invalid inventory {label}')
    _check(set(value) == fields, label)
    return value


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _strings(value: object, *, empty: bool = False) -> bool:
    return (isinstance(value, list) and (empty or bool(value)) and
            all(_text(item) for item in value) and len(set(value)) == len(value))


def _url(value: object) -> bool:
    if not _text(value) or not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None and
                not parsed.fragment and not parsed.query and parsed.port in (None, 443) and
                not any(character.isspace() for character in value))
    except ValueError:
        return False


def _digest(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f'[0-9a-f]{{{length}}}', value) is not None


def _source(value: object) -> dict[str, Any]:
    source = _record(value, SOURCE_FIELDS, 'source')
    _check(_url(source['url']) and _strings(source['evidence']), 'source evidence')
    _check(source['tag'] is None or _text(source['tag']), 'source tag')
    _check(source['commit'] is None or _digest(source['commit'], 40), 'source commit')
    _check(source['github'] in ('exact', 'other-version', 'absent', 'unverified'), 'source github')
    if source['github'] == 'exact':
        _check(urlsplit(source['url']).hostname == 'github.com' and
               _digest(source['commit'], 40), 'source exact identity')
    return source


def _license(value: object) -> dict[str, Any]:
    licence = _record(value, LICENSE_FIELDS, 'license')
    _check(_text(licence['expression']) and _strings(licence['evidence']) and
           _strings(licence['obligations']), 'license evidence')
    _check(licence['status'] in ('allowed', 'conditional', 'forbidden', 'unknown') and
           isinstance(licence['fulfilled'], bool), 'license status')
    if any(word in licence['expression'] for word in ('GPL-', 'MPL-')):
        _check(licence['status'] != 'allowed', 'license conditions')
    return licence


def _safe_path(value: object, name: str, filename: str) -> bool:
    if not isinstance(value, str) or '\\' in value:
        return False
    parts = value.split('/')
    if any(not part or part in ('.', '..', '.idea') or part.startswith('.env') for part in parts):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and parts[:2] == ['vendor', name] and path.name == filename


def _wheel_tag(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in ('py3-none-any', 'py2.py3-none-any'):
        return True
    parts = value.split('-')
    if len(parts) != 3 or re.fullmatch(
            r'cp312-cp312|cp3[2-9]-abi3|cp310-abi3|cp311-abi3|cp312-abi3',
            '-'.join(parts[:2])) is None:
        return False
    platforms = parts[2].split('.')
    if len(set(platforms)) != len(platforms) or not all(re.fullmatch(
            r'manylinux(?:_(?:2_[0-9]+)|1|2010|2014)_x86_64', tag) for tag in platforms):
        return False
    return any(tag in {'manylinux1_x86_64', 'manylinux2010_x86_64', 'manylinux2014_x86_64',
                       'manylinux_2_5_x86_64', 'manylinux_2_12_x86_64',
                       'manylinux_2_17_x86_64'} for tag in platforms)


def _artifact(value: object, name: str, version: str, kind: str) -> dict[str, Any]:
    artifact = _record(value, ARTIFACT_FIELDS, 'artifact')
    filename = artifact['filename']
    _check(isinstance(filename, str) and re.fullmatch(r'[A-Za-z0-9_.+-]+', filename) is not None,
           'artifact filename')
    _check(_safe_path(artifact['path'], name, filename), 'artifact path')
    _check(_digest(artifact['sha256'], 64) and isinstance(artifact['size'], int) and
           not isinstance(artifact['size'], bool) and
           0 < artifact['size'] <= MAX_GIT_BYTES, 'artifact hash/size')
    _check(_url(artifact['url']), 'artifact url')
    _check(artifact['role'] in ('wheel', 'native', 'source', 'license', 'notice'), 'artifact role')
    if artifact['role'] == 'wheel':
        _check(kind == 'python' and _wheel_tag(artifact['tag']), 'artifact tag')
        expected = f"{name.replace('-', '_')}-{version}-{artifact['tag']}.whl"
        _check(filename == expected, 'artifact identity')
    elif artifact['role'] == 'native':
        _check(kind == 'deployment' and artifact['tag'] == 'linux-x86_64' and
               version in filename, 'artifact native identity')
    else:
        _check(artifact['tag'] is None, 'artifact tag')
    return artifact


def _storage(item: dict[str, Any]) -> str:
    licence = item['license']
    source = item['source']
    if source['github'] == 'exact':
        return 'excluded'
    if licence['status'] in ('unknown', 'forbidden') or not licence['fulfilled']:
        return 'blocked'
    if source['github'] == 'unverified':
        return 'blocked'
    return 'eligible'


def _validate_override(item: dict[str, Any]) -> None:
    _check((item['name'], item['version']) == SDK_IDENTITY and
           item['source']['github'] == 'exact' and _storage(item) == 'excluded', 'storage override')
    _check(item['license']['status'] in ('allowed', 'conditional') and
           item['license']['fulfilled'] and item['license']['expression'] == 'Apache-2.0',
           'storage override license')
    wheels = [a for a in item['artifacts'] if a['role'] == 'wheel']
    _check(len(wheels) == 1 and wheels[0]['sha256'] == SDK_HASH and
           wheels[0]['size'] == 316190 and wheels[0]['tag'] == 'py3-none-any',
           'storage override artifact')
    licences = [a for a in item['artifacts'] if a['role'] == 'license']
    _check(len(licences) == 1 and licences[0]['filename'] == 'LICENSE' and
           licences[0]['sha256'] == SDK_LICENSE_HASH and licences[0]['size'] == 11540,
           'storage override license artifact')


def _entry(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError('invalid inventory entry')
    fields = ENTRY_FIELDS | ({'override'} if 'override' in value else set())
    item = _record(value, fields, 'entry fields')
    _check(isinstance(item['name'], str) and
           re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', item['name']) is not None, 'name')
    _check(isinstance(item['version'], str) and re.fullmatch(
        r'[0-9]+(?:\.[0-9]+)+(?:a[0-9]+|b[0-9]+|rc[0-9]+|\.post[0-9]+|\.dev[0-9]+)?',
        item['version']) is not None, 'version')
    _check(item['kind'] in ('python', 'deployment'), 'kind')
    _check(_strings(item['scopes']) and
           set(item['scopes']) <= {'runtime', 'development', 'deployment'}, 'scopes')
    _check(_strings(item['requires'], empty=True), 'requires')
    _source(item['source'])
    _license(item['license'])
    _check(isinstance(item['artifacts'], list), 'artifacts')
    for artifact in item['artifacts']:
        _artifact(artifact, item['name'], item['version'], item['kind'])
    expected = _storage(item)
    if 'override' in item:
        _check(item['override'] == SDK_OVERRIDE, 'storage override')
        _validate_override(item)
        expected = 'eligible'
    _check(item['storage'] == expected and _text(item['reason']), 'storage')
    _check(expected != 'eligible' or any(a['role'] == 'license' for a in item['artifacts']),
           'storage missing license artifact')
    return item


def validate_inventory(value: object) -> list[dict[str, Any]]:
    """Validate complete identities/evidence and derive each storage decision."""
    data = _record(value, {'schema_version', 'target', 'provided_tools', 'entries'}, 'schema')
    _check(isinstance(data['schema_version'], int) and
           not isinstance(data['schema_version'], bool) and data['schema_version'] == 1,
           'schema version')
    _check(data['target'] == TARGET and data['provided_tools'] == PROVIDED_TOOLS, 'target/tools')
    _check(isinstance(data['entries'], list) and bool(data['entries']), 'entries')
    entries = [_entry(item) for item in data['entries']]
    names = [item['name'] for item in entries]
    _check(len(set(names)) == len(names), 'duplicate identity')
    paths = [artifact['path'] for item in entries for artifact in item['artifacts']]
    _check(len(set(paths)) == len(paths), 'duplicate artifact identity')
    for item in entries:
        _check(set(item['requires']) <= set(names), 'dependency closure')
    return entries


def _vendor_paths(directory: Path) -> list[Path]:
    _check(not directory.is_symlink(), 'symlink')
    if not directory.exists():
        return []
    paths = []
    for path in sorted(directory.iterdir()):
        _check(not path.name.startswith('.env') and path.name != '.idea', 'protected path')
        _check(not path.is_symlink(), 'symlink')
        if path.is_dir():
            paths.extend(_vendor_paths(path))
        else:
            _check(path.is_file(), 'vendor file')
            paths.append(path)
    return paths


def validate_vendor_files(project_root: Path, value: object) -> None:
    """Audit actual files including untracked/ignored content before reading bytes."""
    entries = validate_inventory(value)
    _check(not any(path.is_symlink() for path in (project_root, *project_root.parents)),
           'symlink')
    candidates = {artifact['path']: (item, artifact)
                  for item in entries for artifact in item['artifacts']}
    paths = _vendor_paths(project_root / 'vendor')
    present = {path.relative_to(project_root).as_posix() for path in paths}
    documents = {'vendor/inventory.json', 'vendor/README.md'}
    if any(item.get('override') == SDK_OVERRIDE for item in entries):
        documents.update({'vendor/t-tech-investments/README.md',
                          'vendor/t-tech-investments/manifest.json'})
    for item in entries:
        if any(artifact['path'] in present and artifact['role'] in ('wheel', 'native')
               for artifact in item['artifacts']):
            _check(all(artifact['path'] in present for artifact in item['artifacts']
                       if artifact['role'] in ('license', 'notice', 'source')),
                   'artifact licence materials')
    for path in paths:
        relative = path.relative_to(project_root).as_posix()
        candidate = candidates.get(relative)
        if candidate is None:
            _check(relative in documents, 'undeclared vendor file')
            continue
        item, artifact = candidate
        _check(item['storage'] == 'eligible', 'storage')
        _check(path.stat().st_size == artifact['size'], 'artifact size')
        _check(sha256(path.read_bytes()).hexdigest() == artifact['sha256'], 'artifact hash')


def main(arguments: list[str]) -> int:
    """Read local facts and inspect files; never query a registry or install code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(arguments)
    try:
        _check(not any(path.is_symlink() for path in
                       (args.project_root, *args.project_root.parents)), 'symlink')
        _vendor_paths(args.project_root / 'vendor')
        data = json.loads((args.project_root / 'vendor/inventory.json').read_text(encoding='utf-8'))
        validate_vendor_files(args.project_root, data)
    except (InventoryError, OSError, ValueError):
        print('invalid vendor inventory or files', file=sys.stderr)
        return 1
    print('vendor inventory validated')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
