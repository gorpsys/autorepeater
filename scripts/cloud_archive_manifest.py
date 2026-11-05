"""Canonical schema-one inventory and independent verification of final ZIP bytes."""
from collections.abc import Sequence
from hashlib import sha256
import json
import platform
import re
import struct
from typing import Any
from zipfile import ZIP_DEFLATED, ZipInfo
import zlib

from scripts.cloud_archive_inputs import safe_member_name
from scripts.runtime_lock import canonical_bytes


MANIFEST = 'archive-manifest.json'
TIMESTAMP = (1980, 1, 1, 0, 0, 0)
PROFILES = {'default': {'cloud-index'}, 'vendored': {'vendored'},
            'offline': {'auto', 'vendored'}}


class ManifestError(ValueError):
    """Fixed diagnostics do not expose untrusted values or member contents."""


def require(condition: bool, reason: str) -> None:
    """Reject an invalid inventory before consuming its declarations."""
    if not condition:
        raise ManifestError(reason)


def file_mode(name: str) -> str:
    """The single executable wrapper is the only nonstandard file permission."""
    return '100755' if name == 'pip3.12' else '100644'


def check_names(names: Sequence[str]) -> None:
    """Reject unsafe names, duplicate files and file/directory collisions."""
    require(len(set(names)) == len(names) and all(safe_member_name(n) for n in names),
            'invalid archive member names')
    known = set(names)
    require(not any('/'.join(n.split('/')[:i]) in known
                    for n in names for i in range(1, len(n.split('/')))),
            'archive member path collision')


def _record(value: object, fields: set[str]) -> dict[str, Any]:
    require(isinstance(value, dict) and set(value) == fields, 'invalid archive manifest fields')
    assert isinstance(value, dict)
    return value


def _source(value: object, mode: str) -> None:
    source = _record(value, {'head', 'dirty', 'git_state'})
    if source['git_state'] == 'known':
        require(isinstance(source['head'], str) and re.fullmatch(
            '[0-9a-f]{40}', source['head']) is not None
            and isinstance(source['dirty'], bool), 'invalid archive source facts')
    else:
        require(source['git_state'] == 'unavailable' and source['head'] is None
                and source['dirty'] is None, 'invalid archive source facts')
    if mode == 'release':
        require(source['git_state'] == 'known' and source['dirty'] is False,
                'invalid archive release source')


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _schema(value: object) -> dict[str, Any]:
    document = _record(value, {'schema_version', 'mode', 'profile', 'sdk_policy',
                               'source', 'zip_format', 'members'})
    require(_integer(document['schema_version']) and document['schema_version'] == 1,
            'unsupported archive manifest schema')
    require(document['mode'] in ('local', 'release'), 'invalid archive mode')
    profile, policy = document['profile'], document['sdk_policy']
    require(isinstance(profile, str) and profile in PROFILES and isinstance(policy, str)
            and policy in PROFILES[profile] and not (
                document['mode'] == 'release' and profile == 'offline' and policy == 'auto'),
            'invalid archive profile or SDK policy')
    _source(document['source'], document['mode'])
    fingerprint = _record(document['zip_format'], {
        'version', 'compression', 'compresslevel', 'python', 'zlib'})
    require(_integer(fingerprint['version']) and fingerprint['version'] == 1
            and fingerprint['compression'] == 'deflate'
            and _integer(fingerprint['compresslevel']) and fingerprint['compresslevel'] == 9
            and isinstance(fingerprint['python'], str) and re.fullmatch(
                r'\d+\.\d+\.\d+', fingerprint['python']) is not None
            and isinstance(fingerprint['zlib'], str) and re.fullmatch(
                r'\d+(?:\.\d+)+', fingerprint['zlib']) is not None,
            'invalid archive ZIP format')
    members = document['members']
    require(isinstance(members, dict), 'invalid archive inventory')
    check_names([*members, MANIFEST])
    for name, entry in members.items():
        facts = _record(entry, {'size', 'sha256', 'mode'})
        require(_integer(facts['size']) and facts['size'] >= 0
                and isinstance(facts['sha256'], str)
                and re.fullmatch('[0-9a-f]{64}', facts['sha256']) is not None
                and facts['mode'] == file_mode(name), 'invalid archive member facts')
    return document


def render(files: dict[str, bytes], *, profile: str, sdk_policy: str, mode: str = 'local',
           source: dict[str, Any] | None = None) -> bytes:
    """Inventory only the final, owned byte map, excluding this document itself."""
    document = {
        'schema_version': 1, 'mode': mode, 'profile': profile, 'sdk_policy': sdk_policy,
        'source': source if source is not None else {
            'head': None, 'dirty': None, 'git_state': 'unavailable'},
        'zip_format': {'version': 1, 'compression': 'deflate', 'compresslevel': 9,
                       'python': platform.python_version(), 'zlib': zlib.ZLIB_RUNTIME_VERSION},
        'members': {name: {'size': len(data), 'sha256': sha256(data).hexdigest(),
                           'mode': file_mode(name)} for name, data in files.items()},
    }
    return canonical_bytes(_schema(document))


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, 'duplicate archive manifest key')
        result[key] = value
    return result


def _metadata(members: Sequence[ZipInfo], names: set[str], comment: bytes) -> None:
    ordered = [m.filename for m in members]
    require(not comment and ordered == sorted(names, key=lambda name: name.encode('utf-8')),
            'invalid archive metadata membership or order')
    for member in members:
        require(member.filename == member.orig_filename and not member.is_dir()
                and member.date_time == TIMESTAMP and member.create_system == 3
                and member.compress_type == ZIP_DEFLATED
                and member.external_attr == int(file_mode(member.filename), 8) << 16
                and not member.comment and not member.extra and not member.internal_attr
                and member.create_version == 20 and member.extract_version == 20
                and not member.reserved and not member.volume
                and member.flag_bits == (0 if member.filename.isascii() else 0x800),
                'invalid archive ZIP metadata')


def verify_local_header(member: ZipInfo, header: bytes) -> None:
    """Check raw local metadata against the fixed format and central content facts."""
    name = member.filename.encode('utf-8')
    expected = struct.pack('<4s5H3I2H', b'PK\x03\x04', 20,
                           0 if member.filename.isascii() else 0x800,
                           ZIP_DEFLATED, 0, 33, member.CRC, member.compress_size,
                           member.file_size, len(name), 0) + name
    require(header == expected, 'invalid archive local ZIP header')


def verify(files: dict[str, bytes], members: Sequence[ZipInfo], *,
           comment: bytes = b'') -> dict[str, Any]:
    """Authenticate membership, bytes and ZIP metadata against an untrusted declaration."""
    check_names(list(files))
    require(MANIFEST in files, 'missing archive manifest')
    _metadata(members, set(files), comment)
    try:
        document = _schema(json.loads(files[MANIFEST], object_pairs_hook=_unique))
        require(canonical_bytes(document) == files[MANIFEST], 'noncanonical archive manifest')
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError('invalid archive manifest JSON') from None
    require(set(files) == set(document['members']) | {MANIFEST},
            'archive inventory membership drift')
    require(all(len(files[name]) == facts['size'] and sha256(files[name]).hexdigest()
                == facts['sha256'] for name, facts in document['members'].items()),
            'archive inventory content drift')
    return document
