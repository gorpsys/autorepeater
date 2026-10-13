"""Exact PyPI wheel acquisition with typed transport fallback, without a resolver."""
import base64
import csv
from dataclasses import dataclass
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO, StringIO
import json
import logging
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import tempfile
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
import zipfile
import zlib

from scripts import sdk_wheel
from scripts import runtime_lock
from scripts.runtime_lock import compatible_tag, normalized_name, pip_environment


FILE_LOCK = 'runtime-file-lock.json'


class PackageError(ValueError):
    """Safe wheel policy failure, never a reason for transport fallback."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """Reviewed identity and artifact-specific size limit from lock/inventory."""
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    tag: str
    url: str


class Transport(Protocol):  # pylint: disable=too-few-public-methods
    """Hard bounded request; TLS/invalid response is fatal."""
    def fetch(self, artifact: Artifact, timeout: float) -> sdk_wheel.Response:
        """Return owned bytes or a typed availability failure."""


def require(condition: bool, reason: str) -> None:
    """Only fixed reasons reach diagnostics."""
    if not condition:
        raise PackageError(reason)


def validate_artifact(item: Artifact) -> None:
    """Reject unsupported sources, identities and targets before any request."""
    require(normalized_name(item.name) == item.name and
            re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[a-z0-9.]+)?', item.version) is not None,
            'invalid package identity')
    parts = item.tag.split('-')
    compatible = len(parts) == 3 and any(
        compatible_tag('-'.join([*parts[:2], platform])) for platform in parts[2].split('.'))
    require(compatible and item.filename ==
            f'{item.name.replace("-", "_")}-{item.version}-{item.tag}.whl',
            'invalid package filename/tag')
    require(isinstance(item.size, int) and not isinstance(item.size, bool)
            and 0 < item.size <= 100_000_000 and
            re.fullmatch('[0-9a-f]{64}', item.sha256) is not None, 'invalid package size/hash')
    try:
        url = urlsplit(item.url)
        valid = (url.scheme == 'https' and url.hostname == 'files.pythonhosted.org'
                 and url.username is None and url.password is None
                 and url.port in (None, 443) and not url.query and not url.fragment
                 and url.path.startswith('/packages/')
                 and not any(c.isspace() or ord(c) < 32 for c in item.url))
    except ValueError:
        valid = False
    require(valid, 'invalid package source')


def artifact_for(package: dict[str, Any]) -> Artifact:
    """Select one reviewed artifact; SDK source selection has its own policy."""
    require(bool(package['artifacts']), 'missing package artifact')
    item = package['artifacts'][0]
    return Artifact(package['name'], package['version'], item['filename'],
                    item['sha256'], item['size'], item['tag'], item['url'])


def runtime_artifacts(document: dict[str, Any]) -> list[Artifact]:
    """Preserve the canonical runtime closure including its SDK identity."""
    return [artifact_for(package) for package in document['packages']]


def development_artifacts(entries: list[dict[str, Any]], runtime: set[str]) -> list[Artifact]:
    """Use reviewed development seed identities, never an arbitrary fallback version."""
    result = []
    for entry in entries:
        if (entry['kind'] == 'python' and 'development' in entry['scopes']
                and entry['name'] not in runtime):
            artifacts = [a for a in entry['artifacts'] if a['role'] == 'wheel']
            require(len(artifacts) == 1, 'ambiguous development artifact')
            result.append(artifact_for({**entry, 'artifacts': artifacts}))
    return result


def _path(name: str) -> bool:
    parts = name.rstrip('/').split('/')
    return bool(name) and '\\' not in name and ':' not in name and all(
        p and p not in ('.', '..', '.idea') and not p.startswith('.env') for p in parts
    ) and not any(ord(c) < 32 or ord(c) == 127 for c in name)


def _contents(data: bytes, size: int) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        require(0 < len(entries) <= 20000 and sum(e.file_size for e in entries)
                <= min(512 * 1024 * 1024, max(32 * 1024 * 1024, size * 80)), 'package ZIP limits')
        seen: set[str] = set()
        for entry in entries:
            name = entry.filename
            require(_path(name) and name == entry.orig_filename and name not in seen,
                    'package ZIP path/duplicate')
            seen.add(name)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(not entry.flag_bits & 1 and mode in (0, stat.S_IFREG, stat.S_IFDIR)
                    and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    and (mode != stat.S_IFDIR or entry.is_dir())
                    and (not entry.is_dir() or mode != stat.S_IFREG), 'package ZIP type')
            content = archive.read(entry)
            require(len(content) == entry.file_size, 'package ZIP expanded size')
            if not entry.is_dir():
                contents[name] = content
        require(not any('/'.join(n.split('/')[:i]) in contents
                        for n in seen for i in range(1, len(n.split('/')))),
                'package ZIP collision')
    return contents


def _record(contents: dict[str, bytes], directory: str) -> None:
    name = directory + 'RECORD'
    rows = csv.reader(StringIO(contents[name].decode('utf-8'), newline=''), strict=True)
    seen: set[str] = set()
    for row in rows:
        require(len(row) == 3, 'package RECORD row')
        path, digest, size = row
        require(path in contents and path not in seen, 'package RECORD path')
        seen.add(path)
        if path == name:
            require(digest == size == '', 'package RECORD self')
        else:
            data = contents[path]
            encoded = base64.urlsafe_b64encode(sha256(data).digest()).decode().rstrip('=')
            require(digest == 'sha256=' + encoded and size == str(len(data)), 'package RECORD hash')
    require(seen == set(contents), 'package RECORD closure')


def _validated_contents(item: Artifact, data: bytes) -> dict[str, bytes]:
    """Hash before ZIP; validate metadata, tags, paths, CRC and all RECORD hashes."""
    require(isinstance(data, bytes) and len(data) == item.size, 'package size mismatch')
    require(sha256(data).hexdigest() == item.sha256, 'package hash mismatch')
    directory = f'{item.name.replace("-", "_")}-{item.version}.dist-info/'
    try:
        contents = _contents(data, item.size)
        metadata = BytesParser().parsebytes(contents[directory + 'METADATA'])
        require(not metadata.defects and len(metadata.get_all('Name', [])) == 1
                and normalized_name(metadata['Name']) == item.name
                and metadata.get_all('Version') == [item.version], 'package metadata identity')
        wheel = BytesParser().parsebytes(contents[directory + 'WHEEL'])
        python, abi, platforms = item.tag.split('-')
        tags = {f'{p}-{a}-{platform}' for p in python.split('.') for a in abi.split('.')
                for platform in platforms.split('.')}
        require(not wheel.defects and wheel.get_all('Wheel-Version') == ['1.0']
                and set(wheel.get_all('Tag', [])) == tags, 'package wheel tags')
        _record(contents, directory)
    except (KeyError, ValueError, RuntimeError, OSError, EOFError, csv.Error,
            zipfile.BadZipFile, NotImplementedError, zlib.error) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError('invalid package wheel structure') from None
    return contents


def validate_bytes(item: Artifact, data: bytes) -> bytes:
    """Return the same owned bytes after full wheel validation."""
    _validated_contents(item, data)
    return data


def payload_files(document: dict[str, Any], wheelhouse: Path) -> dict[str, bytes]:
    """Revalidate staged wheels and retain their installed files, including licences."""
    result: dict[str, bytes] = {}
    for package in document['packages']:
        candidates = []
        for value in package['artifacts']:
            path = wheelhouse / value['filename']
            if path.exists():
                data = _staged_bytes(wheelhouse, value['filename'],
                                     max(a['size'] for a in package['artifacts']))
                if len(data) == value['size'] and sha256(data).hexdigest() == value['sha256']:
                    candidates.append(value)
        require(len(candidates) == 1, 'missing or ambiguous staged artifact')
        item = artifact_for({**package, 'artifacts': candidates})
        data = _staged_bytes(wheelhouse, item.filename, item.size)
        directory = f'{item.name.replace("-", "_")}-{item.version}.data/'
        for name, content in _validated_contents(item, data).items():
            if name.startswith(directory):
                scheme, separator, relative = name[len(directory):].partition('/')
                require(bool(separator) and scheme in ('purelib', 'platlib', 'data'),
                        'unsupported wheel install scheme')
                name = relative
            require(name not in result and not any(
                name.startswith(p + '/') or p.startswith(name + '/') for p in result),
                'runtime wheel file collision')
            result[name] = content
    return result


def file_hashes(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    """Project owned bytes to exact sizes and SHA-256, without changing file membership."""
    return {name: {'size': len(data), 'sha256': sha256(data).hexdigest()}
            for name, data in files.items()}


def file_lock(document: dict[str, Any], wheelhouse: Path) -> dict[str, Any]:
    """Generate reviewable file facts exclusively from hash/RECORD-verified artifacts."""
    artifacts = {}
    for package in document['packages']:
        for artifact in package['artifacts']:
            item = {**package, 'artifacts': [artifact]}
            staged = wheelhouse / artifact['sha256']
            files = payload_files({'packages': [item]}, staged if staged.exists() else wheelhouse)
            artifacts[artifact['sha256']] = {
                'name': package['name'], 'files': file_hashes(files)}
    return {'schema_version': 1, 'lock_sha256': runtime_lock.canonical_digest(document),
            'artifacts': artifacts}


def locked_files(root: Path, document: dict[str, Any],
                 hashes: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Read independent checked-in wheel projections, bound to this lock and artifact set."""
    policy = runtime_lock.read_json(root / FILE_LOCK)
    require(set(policy) == {'schema_version', 'lock_sha256', 'artifacts'}
            and isinstance(policy['schema_version'], int)
            and not isinstance(policy['schema_version'], bool) and policy['schema_version'] == 1
            and policy['lock_sha256'] == runtime_lock.canonical_digest(document)
            and isinstance(policy['artifacts'], dict), 'runtime file lock drift')
    allowed = {a['sha256']: p['name'] for p in document['packages'] for a in p['artifacts']}
    require(set(policy['artifacts']) == set(allowed)
            and set(hashes) == {p['name'] for p in document['packages']},
            'runtime file lock artifact closure')
    result: dict[str, dict[str, Any]] = {}
    for name, digest in hashes.items():
        require(allowed.get(digest) == name, 'runtime file lock selected artifact')
        entry = policy['artifacts'][digest]
        require(isinstance(entry, dict) and set(entry) == {'name', 'files'}
                and entry['name'] == name and isinstance(entry['files'], dict)
                and 0 < len(entry['files']) <= 20000, 'runtime file lock package')
        for path, facts in entry['files'].items():
            require(_path(path) and not path.endswith('/') and path not in result
                    and isinstance(facts, dict) and set(facts) == {'sha256', 'size'}
                    and runtime_lock.digest(facts['sha256']) and isinstance(facts['size'], int)
                    and not isinstance(facts['size'], bool)
                    and 0 <= facts['size'] <= 512 * 1024 * 1024, 'runtime file lock file')
            result[path] = facts
    return result


def verify_file_hashes(files: dict[str, bytes], expected: dict[str, dict[str, Any]]) -> None:
    """Authenticate source/data/licence membership; allow only pip's known generated files."""
    require(all(file_hashes({name: files[name]})[name] == facts
                for name, facts in expected.items() if not name.endswith('.dist-info/RECORD')
                and name in files)
            and set(expected) <= set(files), 'runtime file content drift')
    additions = set(files) - set(expected)
    require(all(name.startswith('bin/') or name.endswith((
        '.dist-info/INSTALLER', '.dist-info/REQUESTED', '.dist-info/direct_url.json'))
                for name in additions), 'runtime file membership drift')


def _staged_bytes(root: Path, name: str, size: int) -> bytes:
    try:
        return sdk_wheel._read_file(root, name, size)  # pylint: disable=protected-access
    except (OSError, sdk_wheel.WheelError):
        raise PackageError('staged package files unavailable') from None


def reserve(item: Artifact, root: Path, entries: list[dict[str, Any]]) -> bytes:
    """Only matching eligible inventory artifacts with all licence materials apply."""
    entry = next((e for e in entries if e['name'] == item.name
                  and e['version'] == item.version and e['storage'] == 'eligible'), None)
    require(entry is not None, 'package reserve unavailable')
    assert entry is not None
    artifact = next((a for a in entry['artifacts'] if a['role'] == 'wheel' and
                     all(a[k] == getattr(item, k)
                         for k in ('filename', 'sha256', 'size', 'tag'))), None)
    require(artifact is not None, 'package reserve unavailable')
    assert artifact is not None
    try:
        for material in entry['artifacts']:
            if material['role'] in ('license', 'notice', 'source'):
                data = sdk_wheel._read_file(root, material['path'], material['size'])  # pylint: disable=protected-access
                require(len(data) == material['size'] and sha256(data).hexdigest() ==
                        material['sha256'], 'package licence materials mismatch')
        data = sdk_wheel._read_file(root, artifact['path'], item.size)  # pylint: disable=protected-access
    except (OSError, sdk_wheel.WheelError):
        raise PackageError('package reserve files unavailable') from None
    return validate_bytes(item, data)


def acquire(item: Artifact, root: Path, entries: list[dict[str, Any]],
            port: Transport, *, timeout: float) -> bytes:
    """One PyPI attempt; only known availability facts select a package's reserve."""
    validate_artifact(item)
    require(0 < timeout <= 300, 'package acquisition deadline')
    try:
        response = port.fetch(item, min(15, timeout))
        require(isinstance(response, sdk_wheel.Response) and response.url == item.url
                and isinstance(response.status, int) and not isinstance(response.status, bool),
                'invalid package response')
        if response.status in (401, 403, 404, 429):
            raise sdk_wheel.AvailabilityError(sdk_wheel.Reason(f'http_{response.status}'))
        if 500 <= response.status <= 599:
            raise sdk_wheel.AvailabilityError(sdk_wheel.Reason.HTTP_5XX)
        require(response.status == 200, 'invalid package HTTP status')
        return validate_bytes(item, response.data)
    except sdk_wheel.AvailabilityError as error:
        data = reserve(item, root, entries)
        logging.getLogger(__name__).warning(
            'package %s version=%s reason=%s path=vendor/%s/%s',
            item.name, item.version, error.reason.value, item.name, item.filename)
        return data


class WorkerTransport:  # pylint: disable=too-few-public-methods
    """One request process has a hard deadline covering DNS and slow streams."""
    def fetch(self, artifact: Artifact, timeout: float) -> sdk_wheel.Response:
        """subprocess.run kills and waits on timeout; the owned directory always closes."""
        validate_artifact(artifact)
        root = str(Path(__file__).resolve().parents[1])
        bootstrap = ('import sys,resource; '
                     'resource.setrlimit(resource.RLIMIT_FSIZE, '
                     f'({artifact.size}, {artifact.size})); '
                     f'sys.path.insert(0, {root!r}); '
                     'from scripts.package_wheels import request_main; request_main()')
        with tempfile.TemporaryDirectory(prefix='package-request-') as directory:
            try:
                result = subprocess.run([sys.executable, '-I', '-S', '-c', bootstrap,
                                         artifact.url, str(artifact.size)], cwd=directory,
                                        env=pip_environment(), stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=min(15, timeout), check=False)
            except subprocess.TimeoutExpired:
                raise sdk_wheel.AvailabilityError(sdk_wheel.Reason.TIMEOUT) from None
            except OSError:
                raise PackageError('package request unavailable') from None
            require(result.returncode == 0 and len(result.stdout) <= 256, 'invalid package worker')
            try:
                value = json.loads(result.stdout)
                require(isinstance(value, dict), 'invalid package worker')
                if set(value) == {'availability'}:
                    raise sdk_wheel.AvailabilityError(sdk_wheel.Reason(value['availability']))
                require(set(value) == {'status'} and isinstance(value['status'], int),
                        'invalid package worker')
                data = (sdk_wheel._read_file(Path(directory), 'body', artifact.size)  # pylint: disable=protected-access
                        if value['status'] == 200 else b'')
            except (ValueError, OSError):
                raise PackageError('invalid package worker') from None
            return sdk_wheel.Response(artifact.url, value['status'], '', data)


def request_main() -> None:
    """Worker accepts only a previously validated URL/limit; no raw errors leave it."""
    url, limit = sys.argv[1:]
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()),
        sdk_wheel.NoRedirect())
    try:
        with opener.open(Request(url), timeout=15) as response:
            require(response.geturl() == url, 'invalid package response URL')
            data = response.read(int(limit) + 1)
            require(len(data) <= int(limit), 'package response size limit')
            Path('body').write_bytes(data)
            value: dict[str, Any] = {'status': response.status}
    except HTTPError as error:
        error.close()
        value = {'status': error.code}
    except (URLError, OSError) as error:
        try:
            reason = error.reason if isinstance(error, URLError) else error
            sdk_wheel._transport_error(reason)  # pylint: disable=protected-access
        except sdk_wheel.AvailabilityError as availability:
            value = {'availability': availability.reason.value}
        except sdk_wheel.WheelError:
            value = {'error': 'transport_validation'}
    except PackageError:
        value = {'error': 'response_validation'}
    print(json.dumps(value))
