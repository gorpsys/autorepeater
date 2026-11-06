"""Independent inventory documents and metadata probes for the ready ZIP contract."""
from copy import deepcopy
from pathlib import Path
from hashlib import sha256
from test.archive_parity import corrupt_zip_crc
import json
import os
import platform
import time
from unittest.mock import create_autospec
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo
import zlib

import pytest

from scripts import cloud_archive_manifest as manifest
from scripts import build_yandex_archive as builder
from scripts import cloud_archive_inputs as inputs

inventory = manifest
TLS_ENTRIES = {'pip3.12': b'wrapper', 'cloud-ca/manifest.json': b'{"ca":true}',
               'cloud-ca/ca.pem': b'CA', 'cloud-ca/LICENSE': b'license'}
EXPECTED = {
    'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt',
    'autorepeater/__init__.py', 'autorepeater/configs/index.json',
    'autorepeater/configs/deep/another.json',
}


@pytest.fixture(name='project')
def fixture_project(tmp_path):
    """Use a private minimal tree with zero-sized and nested application inputs."""
    for name in EXPECTED:
        path = tmp_path / 'project' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'' if name.endswith('__init__.py') else b'original bytes')
    return tmp_path / 'project'


FILES = {'handler.py': b'handler\x00\xff', 'empty.json': b'', 'pip3.12': b'wrapper'}


def document(files=None):
    """Build the test oracle independently of the production formatter."""
    return {
        'schema_version': 1, 'mode': 'local', 'profile': 'default',
        'sdk_policy': 'cloud-index',
        'source': {'head': None, 'dirty': None, 'git_state': 'unavailable'},
        'zip_format': {'version': 1, 'compression': 'deflate', 'compresslevel': 9,
                       'python': platform.python_version(), 'zlib': zlib.ZLIB_RUNTIME_VERSION},
        'members': {name: {'size': len(data), 'sha256': sha256(data).hexdigest(),
                           'mode': '100755' if name == 'pip3.12' else '100644'}
                    for name, data in (FILES if files is None else files).items()},
    }


def encoded(value):
    """Use the documented canonical encoding rather than the production serializer."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def infos(files):
    """Specify the ZIP metadata contract independently of its writer."""
    result = []
    for name in sorted(files, key=lambda name: name.encode('utf-8')):
        info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.compress_type = ZIP_DEFLATED
        info.external_attr = (0o100755 if name == 'pip3.12' else 0o100644) << 16
        info.flag_bits = 0x800 if not name.isascii() else 0
        result.append(info)
    return result


def ready(value=None):
    """Return an independently declared artifact, including its metadata inventory."""
    files = {**FILES, 'archive-manifest.json': encoded(document() if value is None else value)}
    return files, infos(files)


def test_canonical_inventory_and_toolchain():
    """Every final byte has one size/hash/mode record, without a manifest self hash."""
    data = manifest.render(FILES, profile='default', sdk_policy='cloud-index')
    assert data == encoded(document())
    files, metadata = ready()
    assert manifest.verify(files, metadata) == document()
    assert 'archive-manifest.json' not in json.loads(data)['members']


@pytest.mark.parametrize('path,value', [
    (('schema_version',), True), (('schema_version',), 2), (('extra',), 'SENTINEL'),
    (('mode',), 'other'), (('profile',), []), (('sdk_policy',), 'auto'),
    (('source',), None), (('source', 'extra'), 'SENTINEL'),
    (('source', 'head'), 'a' * 40), (('source', 'dirty'), False),
    (('source', 'git_state'), 'invalid'),
    (('zip_format', 'version'), True), (('zip_format', 'compression'), 'stored'),
    (('zip_format', 'compresslevel'), True), (('zip_format', 'compresslevel'), 8),
    (('zip_format', 'python'), '3.12'), (('zip_format', 'zlib'), None),
    (('zip_format', 'extra'), 'SENTINEL'), (('members',), []),
    (('members', 'handler.py', 'size'), True), (('members', 'handler.py', 'size'), -1),
    (('members', 'handler.py', 'size'), 1), (('members', 'handler.py', 'sha256'), 'A' * 64),
    (('members', 'handler.py', 'sha256'), []),
    (('members', 'handler.py', 'mode'), '100755'),
    (('members', 'pip3.12', 'mode'), '100644'),
    (('members', 'handler.py', 'extra'), 'SENTINEL'),
    (('members', 'archive-manifest.json'), {'size': 0, 'sha256': '0' * 64, 'mode': '100644'}),
])
def test_rejects_closed_schema_types_hashes_and_self_record(path, value):
    """Untrusted JSON never becomes a formatter-produced expected declaration."""
    declared = deepcopy(document())
    target = declared
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    files, metadata = ready(declared)
    with pytest.raises(manifest.ManifestError) as error:
        manifest.verify(files, metadata)
    assert 'SENTINEL' not in str(error.value)


@pytest.mark.parametrize('damage', [
    'missing', 'extra', 'bytes', 'duplicate', 'metadata-missing', 'self', 'duplicate-key',
    'nested-key', 'json', 'encoding', 'noncanonical', 'missing-field', 'mode', 'comment',
])
def test_membership_json_and_bytes_are_strict(damage):
    """CRC-valid rewrites, duplicate keys and incomplete inventories are rejected."""
    files, metadata = ready()
    if damage == 'missing':
        del files['handler.py']
    elif damage == 'extra':
        files['extra.py'] = b'other'
        metadata = infos(files)
    elif damage == 'bytes':
        files['handler.py'] = b'changed'
    elif damage == 'duplicate':
        metadata.append(metadata[0])
    elif damage == 'metadata-missing':
        metadata.pop()
    elif damage == 'self':
        del files['archive-manifest.json']
    elif damage in ('duplicate-key', 'nested-key', 'json', 'encoding', 'noncanonical',
                    'missing-field'):
        damage_manifest_json(files, damage)
    elif damage == 'mode':
        metadata[0].external_attr = 0o100755 << 16
    with pytest.raises(manifest.ManifestError):
        manifest.verify(files, metadata, comment=b'SENTINEL' if damage == 'comment' else b'')


def damage_manifest_json(files, damage):
    """Edit the serialized declaration by hand, preserving evidence of duplicate keys."""
    if damage in ('duplicate-key', 'nested-key'):
        key = b'"mode":"local"' if damage == 'duplicate-key' else b'"size":0'
        files['archive-manifest.json'] = files['archive-manifest.json'].replace(
            key, key + b',' + key, 1)
    elif damage in ('json', 'encoding', 'noncanonical'):
        variants = {'json': b'{SENTINEL', 'encoding': b'\xff',
                    'noncanonical': b' ' + files['archive-manifest.json']}
        files['archive-manifest.json'] = variants[damage]
    elif damage == 'missing-field':
        value = document()
        del value['members']['handler.py']['sha256']
        files['archive-manifest.json'] = encoded(value)


@pytest.mark.parametrize('attribute,value', [
    ('date_time', (2026, 1, 1, 0, 0, 0)), ('create_system', 0), ('compress_type', 0),
    ('extra', b'SENTINEL'), ('comment', b'SENTINEL'), ('flag_bits', 1),
    ('internal_attr', 1), ('create_version', 30), ('extract_version', 30),
])
def test_normalized_zip_metadata(attribute, value):
    """Fixed writer-controlled fields are checked independently of content hashes."""
    files, metadata = ready()
    setattr(metadata[0], attribute, value)
    with pytest.raises(manifest.ManifestError):
        manifest.verify(files, metadata)


@pytest.mark.parametrize('profile,policy', [
    ('default', 'cloud-index'), ('vendored', 'vendored'),
    ('offline', 'auto'), ('offline', 'vendored'),
])
def test_profile_policy_and_known_source(profile, policy):
    """Both local offline choices and validated known provenance fit schema one."""
    value = document()
    value.update(profile=profile, sdk_policy=policy)
    value['source'] = {'head': 'a' * 40, 'dirty': True, 'git_state': 'known'}
    files, metadata = ready(value)
    assert manifest.verify(files, metadata) == value


@pytest.mark.parametrize('source', [
    {'head': None, 'dirty': None, 'git_state': 'unavailable'},
    {'head': 'a' * 40, 'dirty': True, 'git_state': 'known'},
    {'head': 'A' * 40, 'dirty': False, 'git_state': 'known'},
    {'head': 'a' * 40, 'dirty': 0, 'git_state': 'known'},
])
def test_release_requires_clean_known_source(source):
    """The verifier enforces release facts even before the later Git integration."""
    value = document()
    value.update(mode='release', source=source)
    files, metadata = ready(value)
    with pytest.raises(manifest.ManifestError):
        manifest.verify(files, metadata)


def test_release_offline_auto_rejected_and_clean_known_release_accepted():
    """Policy/source combinations are a closed schema, including release offline."""
    value = document()
    value.update(mode='release', source={'head': 'a' * 40, 'dirty': False, 'git_state': 'known'})
    assert manifest.verify(*ready(value)) == value
    value.update(profile='offline', sdk_policy='auto')
    with pytest.raises(manifest.ManifestError):
        manifest.verify(*ready(value))


@pytest.mark.parametrize('files', [
    {'archive-manifest.json': b'forged'}, {'../escape': b''}, {'a': b'', 'a/b': b''},
    {'archive-manifest.json/child': b''},
])
def test_formatter_rejects_reserved_and_unsafe_collision(files):
    """The reserved manifest root belongs exclusively to the archive owner."""
    with pytest.raises(manifest.ManifestError):
        manifest.render(files, profile='default', sdk_policy='cloud-index')


@pytest.mark.parametrize('vendored,offline', [
    (False, False), (True, False), (False, True), (True, True),
])
def test_normalized_writer_reproduces_bytes_despite_filesystem_metadata(
        project, tmp_path, monkeypatch, vendored, offline):
    """Incidental clock, mode, cwd, umask and input creation order cannot change a ZIP."""
    outputs = [tmp_path / 'first.zip', tmp_path / 'second.zip']
    prepare_ports(monkeypatch)
    clock = create_autospec(time.time, spec_set=True, return_value=1700000000)
    monkeypatch.setattr(time, 'time', clock)
    builder.build_archive(project, outputs[0], use_vendored_sdk=vendored, offline_archive=offline)
    for name in reversed(sorted(EXPECTED)):
        path = project / name
        data = path.read_bytes()
        path.unlink()
        path.write_bytes(data)
        path.chmod(0o700)
        os.utime(path, (1800000000, 1800000000))
    clock.return_value = 1900000000
    monkeypatch.chdir(tmp_path)
    mask = os.umask(0o077)
    try:
        builder.build_archive(project, outputs[1], use_vendored_sdk=vendored,
                              offline_archive=offline)
    finally:
        os.umask(mask)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert sha256(outputs[0].read_bytes()).digest() == sha256(outputs[1].read_bytes()).digest()
    with ZipFile(outputs[0]) as archive:
        assert archive.namelist() == sorted(archive.namelist(), key=lambda n: n.encode('utf-8'))
        assert archive.comment == b''
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3 and info.compress_type == 8
            assert not info.extra and not info.comment and not info.internal_attr
            assert info.external_attr >> 16 == (0o100755 if info.filename == 'pip3.12'
                                                else 0o100644)


def prepare_ports(monkeypatch):
    """Isolate all package/TLS effects through their existing strict ports."""
    from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
    ports = []
    for owner, name, files in (
            (builder, '_default_entries', TLS_ENTRIES),
            (builder, '_vendored_entries', {
                'requirements.txt': b'transformed requirements',
                'requirements-runtime.txt': b'transformed runtime',
                'vendor/test.whl': b'wheel', 'vendor/manifest.json': b'{"vendor":true}'}),
            (cloud_dependencies, 'prepare_payload', {
                'dependency-manifest.json': b'{"payload":true}',
                'package/data': b'dependency bytes', 'THIRD_PARTY_NOTICES/test': b'license'})):
        port = create_autospec(getattr(owner, name), spec_set=True, return_value=dict(files))
        monkeypatch.setattr(owner, name, port)
        ports.append(port)
    return ports


FLAGS = [(False, False), (True, False), (False, True), (True, True)]


@pytest.mark.parametrize('vendored,offline', FLAGS)
def test_final_inventory_covers_all_profiles_and_snapshotted_bytes(
        project, tmp_path, monkeypatch, vendored, offline):
    """Hashes cover actual transformed bytes, binary configs, empty files and submanifests."""
    ports = prepare_ports(monkeypatch)
    config = project / 'autorepeater/configs/данные.json'
    config.write_bytes(b'\xef\xbb\xbf{ "bytes": "\xff" }\r\n')
    original = {p.relative_to(project).as_posix(): p.read_bytes()
                for p in builder.archive_files(project)}
    selected = ports[2 if offline else 1 if vendored else 0]
    prepared = selected.return_value

    def prepare(*_args, **_kwargs):
        (project / 'handler.py').write_bytes(b'changed after snapshot')
        config.write_bytes(b'changed after snapshot')
        return prepared

    selected.side_effect = prepare
    read = inputs.read_file
    reads = create_autospec(read, spec_set=True, side_effect=read)
    monkeypatch.setattr(inputs, 'read_file', reads)
    output = builder.build_archive(project, tmp_path / 'profile.zip',
                                   use_vendored_sdk=vendored, offline_archive=offline)
    assert reads.call_count == len(original)
    assert {c.args[0].relative_to(project).as_posix()
            for c in reads.call_args_list} == set(original)
    assert [port.call_count for port in ports] == [int(port is selected) for port in ports]
    expected = {n: data for n, data in original.items()
                if not offline or n not in ('requirements.txt', 'requirements-runtime.txt')}
    expected.update(prepared)
    assert_final_inventory(output, expected, vendored, offline)
    builder.build_archive(project, tmp_path / 'changed.zip', use_vendored_sdk=vendored,
                          offline_archive=offline)
    assert sha256(output.read_bytes()).digest() != sha256(
        (tmp_path / 'changed.zip').read_bytes()).digest()


def assert_final_inventory(output, expected, vendored, offline):
    """Recalculate size/hash/mode from bytes independently of the manifest formatter."""
    with ZipFile(output) as archive:
        declared = json.loads(archive.read('archive-manifest.json'))
        assert set(archive.namelist()) == set(expected) | {'archive-manifest.json'}
        assert set(declared['members']) == set(expected)
        assert declared['source'] == {'head': None, 'dirty': None, 'git_state': 'unavailable'}
        assert declared['profile'] == (
            'offline' if offline else 'vendored' if vendored else 'default')
        assert declared['sdk_policy'] == (
            'vendored' if vendored else 'auto' if offline else 'cloud-index')
        for name, data in expected.items():
            assert archive.read(name) == data
            assert declared['members'][name] == {
                'size': len(data), 'sha256': sha256(data).hexdigest(),
                'mode': '100755' if name == 'pip3.12' else '100644'}


@pytest.mark.parametrize('vendored,offline', FLAGS)
@pytest.mark.parametrize('name', [
    'handler.py', 'archive-manifest.json', 'archive-manifest.json/child',
    'autorepeater/configs/index.json/child', '../outside',
])
def test_prepared_entries_cannot_override_another_owner(
        project, monkeypatch, vendored, offline, name):
    """Only the two deliberate vendored requirements overrides are authorized."""
    ports = prepare_ports(monkeypatch)
    selected = ports[2 if offline else 1 if vendored else 0]
    selected.return_value[name] = b'SENTINEL'
    with pytest.raises(inventory.ManifestError) as error:
        builder.build_archive(project, use_vendored_sdk=vendored, offline_archive=offline)
    assert 'SENTINEL' not in str(error.value)
    assert not (project / 'build').exists()


@pytest.mark.parametrize('vendored,offline', FLAGS)
@pytest.mark.parametrize('stage', [
    'late-read', 'prepare', 'writestr', 'members', 'manifest', 'metadata', 'replace',
    'interrupt',
])
def test_all_profiles_preserve_old_zip_at_every_failure(
        project, monkeypatch, vendored, offline, stage):
    """The established staged/check/replace chain preserves the previous artifact."""
    ports = prepare_ports(monkeypatch)
    output = builder.build_archive(project)
    original = output.read_bytes()
    selected = ports[2 if offline else 1 if vendored else 0]
    if stage == 'late-read':
        read = inputs.read_file

        def failed_read(path, root):
            if path.name == 'another.json':
                raise inputs.InputError('unsafe application read failed')
            return read(path, root)

        monkeypatch.setattr(inputs, 'read_file', create_autospec(
            read, spec_set=True, side_effect=failed_read))
    elif stage == 'prepare':
        selected.side_effect = ValueError('preparation failed')
    elif stage in ('writestr', 'interrupt'):
        monkeypatch.setattr(ZipFile, 'writestr', create_autospec(
            ZipFile.writestr, spec_set=True,
            side_effect=KeyboardInterrupt if stage == 'interrupt' else OSError('write failed')))
    elif stage == 'members':
        monkeypatch.setattr(ZipFile, 'namelist', create_autospec(
            ZipFile.namelist, spec_set=True, return_value=['handler.py']))
    elif stage == 'manifest':
        monkeypatch.setattr(inventory, 'verify', create_autospec(
            inventory.verify, spec_set=True, side_effect=inventory.ManifestError('content drift')))
    elif stage == 'metadata':
        infolist = ZipFile.infolist

        def changed_metadata(archive):
            result = infolist(archive)
            result[0].date_time = (2026, 1, 1, 0, 0, 0)
            return result

        monkeypatch.setattr(ZipFile, 'infolist', create_autospec(
            infolist, spec_set=True, side_effect=changed_metadata))
    else:
        monkeypatch.setattr(Path, 'replace', create_autospec(
            Path.replace, spec_set=True, side_effect=OSError('replace failed')))
    with pytest.raises((ValueError, OSError, KeyboardInterrupt)):
        builder.build_archive(project, use_vendored_sdk=vendored, offline_archive=offline)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize('vendored,offline', FLAGS)
def test_local_header_failure_preserves_old_zip(project, monkeypatch, vendored, offline):
    """A closed ZIP with only local timestamp drift cannot reach atomic replace."""
    prepare_ports(monkeypatch)
    output = builder.build_archive(project)
    original = output.read_bytes()
    write = getattr(builder, '_write_archive')

    def drift(entries, path):
        write(entries, path)
        data = bytearray(path.read_bytes())
        data[10:12] = b'\x01\x00'
        path.write_bytes(data)

    monkeypatch.setattr(builder, '_write_archive', create_autospec(
        write, spec_set=True, side_effect=drift))
    with pytest.raises(inventory.ManifestError, match='invalid archive local ZIP header'):
        builder.build_archive(project, use_vendored_sdk=vendored, offline_archive=offline)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize('vendored,offline', FLAGS)
def test_crc_failure_preserves_old_zip(project, monkeypatch, vendored, offline):
    """A real CRC error is rejected during member reads, before atomic replacement."""
    prepare_ports(monkeypatch)
    output = builder.build_archive(project)
    original = output.read_bytes()
    write = getattr(builder, '_write_archive')

    def corrupt(entries, path):
        write(entries, path)
        corrupt_zip_crc(path)

    monkeypatch.setattr(builder, '_write_archive', create_autospec(
        write, spec_set=True, side_effect=corrupt))
    with pytest.raises(ValueError, match='invalid cloud archive CRC') as error:
        builder.build_archive(project, use_vendored_sdk=vendored, offline_archive=offline)
    assert isinstance(error.value.__cause__, BadZipFile)
    assert 'Bad CRC-32' in str(error.value.__cause__)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]
