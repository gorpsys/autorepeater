"""Stdlib-only SDK integrity and bounded official/reserve source selection."""
import base64
import csv
from dataclasses import dataclass, replace
from datetime import datetime
from email import policy as email_policy
from email.parser import BytesParser
from hashlib import sha256
from enum import StrEnum
from html.parser import HTMLParser
from io import BytesIO, StringIO
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat
import socket
import ssl
import sys
import time
from typing import Any, Callable, NoReturn, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import zipfile
import zlib

from scripts.vendor_inventory import SDK_HASH, SDK_LICENSE_HASH
from scripts import sdk_worker


NAME = 't-tech-investments'
VERSION = '1.51.0'
TAG = 'py3-none-any'
FILENAME = f't_tech_investments-{VERSION}-{TAG}.whl'
DIST_INFO = f't_tech_investments-{VERSION}.dist-info/'
LICENSE_PATH = DIST_INFO + 'licenses/LICENSE'
PROJECT_URL = 'https://opensource.tbank.ru/invest/invest-python'
INDEX_URL = 'https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple'
SIMPLE_URL = INDEX_URL + '/t-tech-investments/'
MAX_WHEEL_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 512
RECOVERY_SOURCE = {
    'kind': 'recovered-installed-distribution',
    'source_url': 'https://github.com/Invest-SDK/invest-python',
    'source_commit': '0110e451cb0316e975ac5fc5ee027b95246f6396',
    'matched_code_files': 125,
    'caveat': 'Code matches source; official wheel hash not independently verified.',
}
MANIFEST_FIELDS = {'schema_version', 'name', 'version', 'filename', 'size', 'sha256',
                   'tag', 'license', 'project_url', 'index_url', 'recovery_source',
                   'official_hash_verified', 'approvals'}
APPROVAL_FIELDS = {'version', 'reserve_sha256', 'index_sha256', 'approved_by',
                   'approved_at_utc', 'evidence'}


class WheelError(ValueError):
    """Fixed safe reasons, without raw metadata, paths or credentials."""


@dataclass(frozen=True, slots=True)
class HashApproval:
    """Recorded permission for exactly one version and reserve/index pair."""
    version: str
    reserve_sha256: str
    index_sha256: str
    approved_by: str
    approved_at_utc: str
    evidence: str


@dataclass(frozen=True, slots=True)
class WheelIdentity:
    """SDK identity shared by manifest and the wheel's own metadata."""
    name: str
    version: str
    filename: str
    tag: str
    license: str


@dataclass(frozen=True, slots=True)
class WheelManifest:
    """Immutable validated policy; provenance remains explicitly unverified."""
    identity: WheelIdentity
    size: int
    sha256: str
    official_hash_verified: bool
    approvals: tuple[HashApproval, ...]


@dataclass(frozen=True, slots=True)
class ValidatedWheel:
    """Owned immutable bytes for consumers; no mutable path to reread."""
    data: bytes
    license_bytes: bytes
    manifest: WheelManifest


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise WheelError(f'invalid SDK {reason}')


def _record(value: object, fields: set[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WheelError(f'invalid SDK {reason}')
    _check(set(value) == fields, reason)
    return value


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _evidence_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 4096 or any(
            character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.port not in (None, 443):
            return False
        return (parsed.username is None and parsed.password is None and
                not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _approval(value: object) -> HashApproval:
    item = _record(value, APPROVAL_FIELDS, 'approval schema')
    _check(item['version'] == VERSION and item['reserve_sha256'] == SDK_HASH and
           _digest(item['index_sha256']), 'approval pair')
    _check(isinstance(item['approved_by'], str) and
           re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@ -]{0,199}', item['approved_by']) is not None,
           'approval author')
    date = item['approved_at_utc']
    _check(isinstance(date, str) and
           re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', date) is not None,
           'approval date')
    try:
        datetime.strptime(date, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError as error:
        raise WheelError('invalid SDK approval date') from error
    _check(_evidence_url(item['evidence']), 'approval evidence')
    return HashApproval(**item)


def parse_manifest(value: object) -> WheelManifest:
    """Reject coercion, extra fields and implicit broad mismatch permissions."""
    item = _record(value, MANIFEST_FIELDS, 'manifest schema')
    _check(isinstance(item['schema_version'], int) and
           not isinstance(item['schema_version'], bool) and item['schema_version'] == 1,
           'manifest version')
    _check((item['name'], item['version'], item['filename'], item['tag'], item['license']) ==
           (NAME, VERSION, FILENAME, TAG, 'Apache-2.0'), 'manifest identity')
    _check(isinstance(item['size'], int) and not isinstance(item['size'], bool) and
           0 < item['size'] <= MAX_WHEEL_BYTES and
           _digest(item['sha256']), 'manifest hash/size')
    _check(item['official_hash_verified'] is False, 'manifest provenance')
    source = _record(item['recovery_source'], set(RECOVERY_SOURCE), 'manifest recovery')
    _check(item['project_url'] == PROJECT_URL and item['index_url'] == INDEX_URL and
           source == RECOVERY_SOURCE and isinstance(source['matched_code_files'], int),
           'manifest source')
    _check(isinstance(item['approvals'], list) and len(item['approvals']) <= 512,
           'manifest approvals')
    approvals = tuple(_approval(entry) for entry in item['approvals'])
    pairs = [(entry.version, entry.reserve_sha256, entry.index_sha256) for entry in approvals]
    _check(len(set(pairs)) == len(pairs), 'duplicate approval')
    identity = WheelIdentity(item['name'], item['version'], item['filename'],
                             item['tag'], item['license'])
    return WheelManifest(identity, item['size'], item['sha256'], False, approvals)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _check(key not in result, 'duplicate JSON field')
        result[key] = value
    return result


def _read_file(root: Path, relative: str, limit: int, *, allow_empty: bool = False) -> bytes:
    # Walk directory descriptors with O_NOFOLLOW so path replacement cannot redirect reads.
    parts = (root.absolute() / relative).parts
    _check(all(part not in ('.', '..', '.idea') and not part.startswith('.env')
               for part in parts[1:]), 'file path')
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=descriptor)
        with os.fdopen(file_descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _check(stat.S_ISREG(info.st_mode) and (0 if allow_empty else 1) <= info.st_size
                   <= limit, 'file size/type')
            data = stream.read(limit + 1)
            _check(len(data) == info.st_size and len(data) <= limit, 'file size')
            return data
    finally:
        os.close(descriptor)


def _entry_path(name: str) -> bool:
    parts = name.split('/')
    return (bool(name) and '\\' not in name and ':' not in name and
            not any(ord(character) < 32 or ord(character) == 127 for character in name) and
            all(part and part not in ('.', '..', '.idea') and
                not part.startswith('.env') for part in parts) and
            (parts[0] == 't_tech' or parts[0] == DIST_INFO.rstrip('/')))


def _zip_contents(data: bytes) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        _check(0 < len(entries) <= MAX_ENTRIES and
               sum(entry.file_size for entry in entries) <= MAX_EXPANDED_BYTES,
               'ZIP limits')
        for entry in entries:
            _check(_entry_path(entry.filename) and entry.orig_filename == entry.filename and
                   entry.filename not in contents, 'ZIP path/duplicate')
            mode = entry.external_attr >> 16
            _check(not entry.flag_bits & 1 and not entry.is_dir() and
                   stat.S_IFMT(mode) in (0, stat.S_IFREG) and
                   entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                   'ZIP entry type')
            with archive.open(entry) as stream:
                content = stream.read(entry.file_size + 1)
            _check(len(content) == entry.file_size, 'ZIP expanded size')
            contents[entry.filename] = content
        _check(not any('/'.join(name.split('/')[:index]) in contents
                       for name in contents for index in range(1, len(name.split('/')))),
               'ZIP file/directory collision')
    return contents


def _headers(data: bytes, expected: dict[str, str]) -> None:
    data.decode('utf-8', errors='strict')
    message = BytesParser(policy=email_policy.compat32).parsebytes(data)
    _check(not message.defects, 'metadata format')
    for field, value in expected.items():
        _check(message.get_all(field) == [value], 'metadata identity')


def _verify_record(contents: dict[str, bytes]) -> None:
    record_name = DIST_INFO + 'RECORD'
    rows = csv.reader(StringIO(contents[record_name].decode('utf-8'), newline=''), strict=True)
    seen: set[str] = set()
    for row in rows:
        _check(len(row) == 3, 'RECORD row')
        name, digest, size = row
        _check(name in contents and name not in seen, 'RECORD path/duplicate')
        seen.add(name)
        if name == record_name:
            _check(digest == '' and size == '', 'RECORD self')
        else:
            content = contents[name]
            expected = base64.urlsafe_b64encode(sha256(content).digest()).decode().rstrip('=')
            _check(digest == 'sha256=' + expected and size == str(len(content)), 'RECORD hash/size')
    _check(seen == set(contents), 'RECORD closure')


def validate_wheel(data: bytes, manifest: WheelManifest, license_bytes: bytes) -> ValidatedWheel:
    """Validate the exact in-memory artifact before any consumer receives it."""
    _check(manifest.identity == WheelIdentity(NAME, VERSION, FILENAME, TAG, 'Apache-2.0'),
           'wheel identity')
    _check(isinstance(data, bytes) and 0 < len(data) <= MAX_WHEEL_BYTES and
           len(data) == manifest.size, 'wheel size')
    _check(sha256(data).hexdigest() == manifest.sha256, 'wheel hash')
    _check(isinstance(license_bytes, bytes) and len(license_bytes) == 11540 and
           sha256(license_bytes).hexdigest() == SDK_LICENSE_HASH, 'license hash/size')
    try:
        contents = _zip_contents(data)
        _headers(contents[DIST_INFO + 'METADATA'], {
            'Metadata-Version': '2.4', 'Name': NAME, 'Version': VERSION,
            'License-Expression': 'Apache-2.0', 'License-File': 'LICENSE',
        })
        _headers(contents[DIST_INFO + 'WHEEL'], {
            'Wheel-Version': '1.0', 'Root-Is-Purelib': 'true', 'Tag': TAG,
        })
        _check(contents[LICENSE_PATH] == license_bytes and
               b'Copyright 2023 Tinkoff' in license_bytes, 'license attribution')
        _verify_record(contents)
    except (KeyError, ValueError, RuntimeError, OSError, EOFError, csv.Error,
            zipfile.BadZipFile, NotImplementedError, zlib.error) as error:
        if isinstance(error, WheelError):
            raise
        raise WheelError('invalid SDK wheel structure') from None
    return ValidatedWheel(data, license_bytes, manifest)


def load_vendored_wheel(project_root: Path) -> ValidatedWheel:
    """Bounded no-symlink local reads, exact reserve pin, owned-byte validation."""
    directory = 'vendor/t-tech-investments/'
    try:
        value = json.loads(_read_file(project_root, directory + 'manifest.json', 65536),
                           object_pairs_hook=_json_pairs)
        manifest = parse_manifest(value)
        _check(manifest.sha256 == SDK_HASH and manifest.size == 316190, 'reserve pin')
        license_bytes = _read_file(project_root, directory + 'LICENSE', 11540)
        data = _read_file(project_root, directory + manifest.identity.filename, MAX_WHEEL_BYTES)
        return validate_wheel(data, manifest, license_bytes)
    except (OSError, ValueError) as error:
        if isinstance(error, WheelError):
            raise
        raise WheelError('invalid SDK local files') from None


class Source(StrEnum):
    """Source identity is independent of installation."""
    OFFICIAL = 'official'
    VENDORED = 'vendored'


class Reason(StrEnum):
    """Only known facts may be logged or cross the worker boundary."""
    OFFICIAL_AVAILABLE = 'official_available'
    EXPLICIT_VENDORED = 'explicit_vendored_mode'
    VERSION_MISSING = 'version_missing'
    DNS = 'dns_unavailable'
    NETWORK = 'network_unavailable'
    TIMEOUT = 'timeout'
    HTTP_401 = 'http_401'
    HTTP_403 = 'http_403'
    HTTP_404 = 'http_404'
    HTTP_429 = 'http_429'
    HTTP_5XX = 'http_5xx'


AVAILABILITY_REASONS = frozenset(Reason) - {
    Reason.OFFICIAL_AVAILABLE, Reason.EXPLICIT_VENDORED, Reason.VERSION_MISSING,
}


class AvailabilityError(Exception):
    """Typed transport failure; raw response/error strings are never used."""
    def __init__(self, reason: Reason) -> None:
        _check(isinstance(reason, Reason) and reason in AVAILABILITY_REASONS,
               'availability reason')
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class Response:
    """One transport result, with explicit URL/status/media and owned bytes."""
    url: str
    status: int
    media_type: str
    data: bytes


class Transport(Protocol):  # pylint: disable=too-few-public-methods
    """One bounded request, with no retry or implicit redirect."""
    def fetch(self, url: str) -> Response:
        """Return bytes or raise a typed availability/fatal failure."""


@dataclass(frozen=True, slots=True)
class Selection:
    """Immutable result handed to subsequent installation/packaging tasks."""
    source: Source
    reason: Reason
    wheel: ValidatedWheel
    index_sha256: str | None


def official_url(value: str) -> str:
    """Resolve only canonical HTTPS links on the fixed official origin."""
    _check(isinstance(value, str) and len(value) <= 4096 and
           not any(ord(char) <= 32 or ord(char) == 127 for char in value) and
           not any(char in value for char in ('\\', '%')) and
           not any(part in ('.', '..') for part in value.split('/')), 'official URL')
    try:
        resolved = urljoin(SIMPLE_URL, value)
        parsed = urlsplit(resolved)
        _check(parsed.scheme == 'https' and parsed.netloc == 'opensource.tbank.ru' and
               parsed.port is None and parsed.username is None and parsed.password is None and
               not parsed.query and not parsed.fragment and
               all(part not in ('.', '..') for part in parsed.path.split('/')),
               'official URL')
    except ValueError:
        raise WheelError('invalid SDK official URL') from None
    return resolved


class SimpleIndex(HTMLParser):
    """Supported complete GitLab HTML simple index, including an empty body."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.roots = 0
        self.sections: set[str] = set()
        self.headings: list[str] = []
        self.links: list[tuple[str, str, bool]] = []
        self.attributes: dict[str, str | None] = {}
        self.text = ''

    def handle_decl(self, decl: str) -> None:
        """Only the standard HTML doctype is accepted."""
        _check(decl.lower() == 'doctype html' and not self.stack, 'index doctype')

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enforce nesting rather than trusting HTMLParser's recovery behavior."""
        parents = {'html': None, 'head': 'html', 'body': 'html', 'title': 'head',
                   'h1': 'body', 'a': 'body', 'br': 'body', 'meta': 'head'}
        _check(tag in parents and (self.stack[-1] if self.stack else None) == parents[tag],
               'index structure')
        _check(len(dict(attrs)) == len(attrs), 'index attributes')
        if tag == 'html':
            self.roots += 1
            _check(self.roots == 1, 'index roots')
        if tag in ('head', 'body'):
            _check(tag not in self.sections, 'index sections')
            self.sections.add(tag)
        if tag == 'a':
            self.attributes = dict(attrs)
            _check(isinstance(self.attributes.get('href'), str), 'index href')
        if tag in ('a', 'h1', 'title'):
            self.text = ''
        if tag not in ('br', 'meta'):
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Void elements may use XHTML spelling; other self-closing tags fail."""
        _check(tag in ('br', 'meta'), 'index self-closing tag')
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        """Require all non-void tags to close in order."""
        _check(bool(self.stack) and self.stack[-1] == tag, 'index closing tag')
        if tag == 'a':
            self.links.append((str(self.attributes['href']), self.text.strip(),
                               'data-yanked' in self.attributes))
        if tag in ('h1', 'title'):
            self.headings.append(self.text.strip())
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        """Free text/login messages cannot masquerade as an empty index."""
        if self.stack and self.stack[-1] in ('a', 'h1', 'title'):
            self.text += data
        else:
            _check(not data.strip(), 'index text')


def parse_index(data: bytes) -> tuple[str, str] | None:
    """Extract exactly the known wheel with a mandatory official SHA256."""
    _check(isinstance(data, bytes) and len(data) <= MAX_WHEEL_BYTES, 'index size')
    parser = SimpleIndex()
    try:
        parser.feed(data.decode('utf-8', errors='strict'))
        parser.close()
    except (UnicodeError, ValueError):
        raise WheelError('invalid SDK index format') from None
    _check(parser.roots == 1 and 'body' in parser.sections and not parser.stack and
           bool(parser.headings) and
           all(heading in (NAME, 'Links for ' + NAME) for heading in parser.headings),
           'index document')
    matches: list[tuple[str, str]] = []
    for href, label, yanked in parser.links:
        link, separator, fragment = href.partition('#')
        url = official_url(link)
        filename = urlsplit(url).path.rsplit('/', 1)[-1]
        identity = re.fullmatch(
            r'(?:t_tech_investments|t-tech-investments)-(?P<version>[0-9][A-Za-z0-9_.+!]*)'
            r'(?:-(?:[0-9][A-Za-z0-9_.]*-)?[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-'
            r'[A-Za-z0-9_.]+\.whl|\.tar\.gz|\.zip)', filename)
        _check(filename == label and identity is not None,
               'index filename')
        assert identity is not None
        if filename.endswith('.whl') and identity['version'] == VERSION:
            _check(filename == FILENAME and not yanked and separator == '#' and
                   fragment.startswith('sha256=') and _digest(fragment[7:]), 'index wheel')
            matches.append((url, fragment[7:]))
    _check(len(matches) <= 1, 'ambiguous index wheel')
    return matches[0] if matches else None


def check_approval(manifest: WheelManifest, digest: str) -> None:
    """An explicit pair authorizes source selection, never corruption."""
    _check(_digest(digest), 'index digest')
    if digest != manifest.sha256 and not any(
            (entry.version, entry.reserve_sha256, entry.index_sha256) ==
            (manifest.identity.version, manifest.sha256, digest) for entry in manifest.approvals):
        raise WheelError('SDK approval_required')


def _response(port: Transport, url: str) -> Response:
    response = port.fetch(url)
    _check(isinstance(response, Response) and response.url == url and
           isinstance(response.status, int) and not isinstance(response.status, bool) and
           isinstance(response.media_type, str) and isinstance(response.data, bytes) and
           len(response.data) <= MAX_WHEEL_BYTES, 'transport response')
    if response.status in (401, 403, 404, 429):
        raise AvailabilityError(Reason(f'http_{response.status}'))
    if 500 <= response.status <= 599:
        raise AvailabilityError(Reason.HTTP_5XX)
    _check(response.status == 200, 'HTTP status')
    return response


def warn_selection(result: Selection) -> None:
    """Log only fixed reasons and a fixed relative reserve path."""
    if result.source == Source.VENDORED:
        logging.getLogger(__name__).warning(
            'SDK %s source=vendored reason=%s path=%s', VERSION, result.reason.value,
            'vendor/t-tech-investments/' + FILENAME)


def select_source(project_root: Path, mode: str, port: Transport,
                  phase: Callable[[str, str | None], None] | None = None) -> Selection:
    """One official attempt or forced probe; no install, retry or policy mutation."""
    _check(mode in ('auto', 'vendored'), 'source mode')
    if phase:
        phase('reserve_validation', None)
    reserve = load_vendored_wheel(project_root)
    digest = None
    try:
        if phase:
            phase('index_request', None)
        response = _response(port, SIMPLE_URL)
        if phase:
            phase('index_validation', None)
        _check(response.media_type.lower().replace(' ', '') in
               ('text/html', 'text/html;charset=utf-8'), 'index media type')
        candidate = parse_index(response.data)
        if candidate is None:
            reason = Reason.VERSION_MISSING
        else:
            url, digest = candidate
            check_approval(reserve.manifest, digest)
            if mode == 'auto':
                if phase:
                    phase('wheel_request', digest)
                data = _response(port, url).data
                if phase:
                    phase('wheel_validation', digest)
                official = validate_wheel(
                    data, replace(reserve.manifest, size=len(data), sha256=digest),
                    reserve.license_bytes)
                return Selection(Source.OFFICIAL, Reason.OFFICIAL_AVAILABLE, official, digest)
            reason = Reason.EXPLICIT_VENDORED
    except AvailabilityError as error:
        reason = error.reason
    result = Selection(Source.VENDORED, Reason.EXPLICIT_VENDORED if mode == 'vendored'
                       else reason, reserve, digest)
    warn_selection(result)
    return result


def acquire_sdk(project_root: Path, mode: str = 'auto') -> Selection:
    """Supervise the entire SDK stage (60s), including DNS and slow streams."""
    _check(mode in ('auto', 'vendored'), 'source mode')
    deadline = time.monotonic() + 60
    try:
        value = sdk_worker.run_worker('select', project_root, mode, timeout=40)
    except sdk_worker.WorkerTimeout as error:
        _check(error.phase in ('index_request', 'wheel_request'), 'acquisition deadline')
        value = sdk_worker.run_worker('reserve', project_root, mode,
                                      timeout=min(15, max(0.001, deadline - time.monotonic())))
        item = _record(value, {'source', 'reason', 'wheel', 'index_sha256'}, 'worker result')
        value = {**item, 'index_sha256': error.index_sha256}
    reserve = load_vendored_wheel(project_root)
    result = decode_selection(value, reserve)
    _check((result.source == Source.VENDORED and result.reason == Reason.EXPLICIT_VENDORED)
           if mode == 'vendored' else result.reason != Reason.EXPLICIT_VENDORED,
           'worker source mode')
    _check(time.monotonic() <= deadline, 'acquisition deadline')
    warn_selection(result)
    return result


def _worker_data(value: object) -> bytes:
    if not isinstance(value, str):
        raise WheelError('invalid SDK worker bytes')
    _check(len(value) <= ((MAX_WHEEL_BYTES + 2) // 3) * 4, 'worker bytes size')
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        raise WheelError('invalid SDK worker bytes') from None
    _check(len(data) <= MAX_WHEEL_BYTES, 'worker bytes')
    return data


def encode_selection(result: Selection) -> dict[str, Any]:
    """No mutable path, manifest replacement, raw URL/error or credentials."""
    return {'source': result.source.value, 'reason': result.reason.value,
            'wheel': base64.b64encode(result.wheel.data).decode('ascii'),
            'index_sha256': result.index_sha256}


def _worker_error(value: object) -> None:
    if isinstance(value, dict) and 'error' in value:
        record = _record(value, {'error'}, 'worker error')
        if record['error'] == 'approval_required':
            raise WheelError('SDK approval_required')
        raise WheelError('invalid SDK acquisition worker')


def decode_selection(value: object, reserve: ValidatedWheel) -> Selection:
    """Revalidate owned bytes and the exact pair against parent-owned policy."""
    _worker_error(value)
    item = _record(value, {'source', 'reason', 'wheel', 'index_sha256'}, 'worker result')
    try:
        source, reason = Source(item['source']), Reason(item['reason'])
    except (ValueError, TypeError):
        raise WheelError('invalid SDK worker result') from None
    data = _worker_data(item['wheel'])
    digest = item['index_sha256']
    if digest is not None:
        check_approval(reserve.manifest, digest)
    if source == Source.OFFICIAL:
        _check(reason == Reason.OFFICIAL_AVAILABLE and digest is not None, 'worker source')
        result = validate_wheel(
            data, replace(reserve.manifest, size=len(data), sha256=digest), reserve.license_bytes)
    else:
        _check(reason != Reason.OFFICIAL_AVAILABLE and data == reserve.data, 'worker reserve')
        _check(reason != Reason.VERSION_MISSING or digest is None, 'worker absence')
        result = reserve
    return Selection(source, reason, result, digest)


class NoRedirect(HTTPRedirectHandler):
    """Reject even same-origin redirects rather than following an unknown chain."""
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """All redirects are rejected regardless of status or target."""
        return None


def _transport_error(reason: object) -> NoReturn:
    if isinstance(reason, ssl.SSLError):
        raise WheelError('invalid SDK TLS') from None
    if isinstance(reason, TimeoutError):
        raise AvailabilityError(Reason.TIMEOUT) from None
    if isinstance(reason, socket.gaierror):
        raise AvailabilityError(Reason.DNS) from None
    if isinstance(reason, ConnectionError):
        raise AvailabilityError(Reason.NETWORK) from None
    if isinstance(reason, OSError) and reason.errno in (101, 113, 100, 104, 111):
        raise AvailabilityError(Reason.NETWORK) from None
    raise WheelError('invalid SDK transport') from None


def request_bytes(url: str) -> Response:
    """Fixed official origin, verified certificates, bounded body, no proxy overrides."""
    url = official_url(url)
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    request = Request(url, headers={
        'Accept': 'text/html' if url == SIMPLE_URL else 'application/octet-stream'})
    try:
        with opener.open(request, timeout=15) as response:
            _check(response.geturl() == url, 'response URL')
            data = response.read(MAX_WHEEL_BYTES + 1)
            _check(len(data) <= MAX_WHEEL_BYTES, 'response size')
            return Response(url, response.status, response.headers.get('Content-Type', ''), data)
    except HTTPError as error:
        error.close()
        return Response(url, error.code, '', b'')
    except URLError as error:
        _transport_error(error.reason)
    except OSError as error:
        _transport_error(error)


class WorkerTransport:  # pylint: disable=too-few-public-methods
    """Each request gets a hard 15s process timeout, covering DNS/slow streaming."""
    def __init__(self, temp_root: Path | None = None) -> None:
        self.temp_root = temp_root

    def fetch(self, url: str) -> Response:
        """Decode a bounded response DTO, never arbitrary child paths or stderr."""
        try:
            value = sdk_worker.run_worker('request', Path(__file__).resolve().parents[1], url,
                                          timeout=15, temp_root=self.temp_root)
        except sdk_worker.WorkerTimeout:
            raise AvailabilityError(Reason.TIMEOUT) from None
        _worker_error(value)
        if isinstance(value, dict) and set(value) == {'availability'}:
            try:
                raise AvailabilityError(Reason(value['availability']))
            except (ValueError, TypeError):
                raise WheelError('invalid SDK worker result') from None
        item = _record(value, {'url', 'status', 'media_type', 'data'}, 'worker response')
        _check(item['url'] == url and isinstance(item['status'], int) and
               not isinstance(item['status'], bool) and isinstance(item['media_type'], str) and
               len(item['media_type']) <= 512, 'worker response')
        return Response(url, item['status'], item['media_type'], _worker_data(item['data']))


def worker_main() -> None:
    """Trusted entry point emits a fixed schema; diagnostics stay private."""
    def interrupted(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    # Graceful group cancellation lets the selection process reap its request child.
    signal.signal(signal.SIGTERM, interrupted)
    action, root, argument = sys.argv[1:]
    try:
        if action == 'request':
            response = request_bytes(argument)
            value: dict[str, Any] = {'url': response.url, 'status': response.status,
                                     'media_type': response.media_type,
                                     'data': base64.b64encode(response.data).decode('ascii')}
        elif action == 'reserve':
            value = encode_selection(Selection(
                Source.VENDORED, Reason.EXPLICIT_VENDORED if argument == 'vendored'
                else Reason.TIMEOUT, load_vendored_wheel(Path(root)), None))
        elif action == 'select':
            selected = select_source(Path(root), argument, WorkerTransport(Path.cwd()),
                                     sdk_worker.publish_phase)
            value = encode_selection(selected)
        else:
            raise WheelError('invalid SDK worker action')
    except AvailabilityError as error:
        value = {'availability': error.reason.value}
    except (WheelError, sdk_worker.WorkerError) as error:
        value = {'error': 'approval_required' if str(error) == 'SDK approval_required'
                 else 'validation_failed'}
    print(json.dumps(value, separators=(',', ':')))
