"""Offline validation of exact recovered bytes before installation or packaging."""
import base64
import csv
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import struct
import subprocess
from unittest.mock import create_autospec
import warnings
import zipfile

import pytest

from scripts import sdk_wheel as sdk


ROOT = Path(__file__).resolve().parents[1]
DIST = 't_tech_investments-1.51.0.dist-info/'


def manifest_data():
    """Read repository policy, never a private recovery path."""
    return json.loads((ROOT / 'vendor/t-tech-investments/manifest.json').read_text())


def licence():
    """Preserve CRLF and the original copyright notice."""
    return (ROOT / 'vendor/t-tech-investments/LICENSE').read_bytes()


def wheel(files=None, record=None, extra=()):
    """Small valid wheel with real licence and correctly hashed RECORD."""
    payload = {
        't_tech/invest/__init__.py': b'__version__ = "1.51.0"\n',
        DIST + 'METADATA': (b'Metadata-Version: 2.4\nName: t-tech-investments\n'
                            b'Version: 1.51.0\nLicense-Expression: Apache-2.0\n'
                            b'License-File: LICENSE\n\n'),
        DIST + 'WHEEL': (b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\n'
                         b'Tag: py3-none-any\n\n'),
        DIST + 'licenses/LICENSE': licence(),
    }
    if files:
        payload.update(files)
    output = StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    for name, data in payload.items():
        digest = base64.urlsafe_b64encode(sha256(data).digest()).decode().rstrip('=')
        writer.writerow((name, 'sha256=' + digest, str(len(data))))
    writer.writerow((DIST + 'RECORD', '', ''))
    payload[DIST + 'RECORD'] = output.getvalue().encode() if record is None else record
    result = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
            for name, data in extra:
                archive.writestr(name, data)
    data = result.getvalue()
    policy = replace(sdk.parse_manifest(manifest_data()), size=len(data),
                     sha256=sha256(data).hexdigest())
    return data, policy


def validate(data, policy):
    """Pass owned immutable bytes into the same validation boundary."""
    return sdk.validate_wheel(data, policy, licence())


def test_real_recovered_wheel():
    """Code matching is evidence, not independent official artifact verification."""
    result = sdk.load_vendored_wheel(ROOT)
    assert len(result.data) == 316190
    assert sha256(result.data).hexdigest() == sdk.SDK_HASH
    assert result.manifest.official_hash_verified is False
    assert not result.manifest.approvals
    assert b'Copyright 2023 Tinkoff' in result.license_bytes
    assert result.data == (ROOT / 'vendor/t-tech-investments' / sdk.FILENAME).read_bytes()
    assert result.license_bytes == licence()
    with pytest.raises(FrozenInstanceError):
        result.data = b'changed'


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', 2), ('name', 'another'),
    ('version', '1.52.0'), ('filename', '../sdk.whl'), ('filename', 'other.whl'),
    ('size', True), ('size', 0), ('size', 2 * 1024 * 1024 + 1),
    ('size', '316190'), ('sha256', 'A' * 64), ('sha256', '0' * 63),
    ('tag', 'cp312-none-any'), ('license', 'MIT'), ('official_hash_verified', 1),
    ('official_hash_verified', True), ('approvals', None),
    ('project_url', 'https://user:secret@opensource.tbank.ru/x'),
    ('index_url', 'http://opensource.tbank.ru/x'), ('recovery_source', '/tmp/private'),
])
def test_invalid_manifest(field, value):
    """Bad identity, types and provenance fail without credentials in diagnostics."""
    data = manifest_data()
    data[field] = value
    with pytest.raises(sdk.WheelError) as caught:
        sdk.parse_manifest(data)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('value', [None, [], 1, {}, {'unknown': 1}])
def test_manifest_schema(value):
    """The schema rejects foreign objects and unknown fields."""
    with pytest.raises(sdk.WheelError):
        sdk.parse_manifest(value)


def approval():
    """An example explicit pair, never written into repository approvals."""
    return {'version': '1.51.0', 'reserve_sha256': sdk.SDK_HASH,
            'index_sha256': '1' * 64, 'approved_by': 'reviewer',
            'approved_at_utc': '2026-10-12T12:00:00Z',
            'evidence': 'https://github.com/example/repo/issues/1'}


def test_exact_approval_schema():
    """Only an exact immutable pair with dated evidence can be represented."""
    data = manifest_data()
    data['approvals'] = [approval()]
    policy = sdk.parse_manifest(data)
    assert policy.approvals[0].index_sha256 == '1' * 64
    assert policy.approvals[0].reserve_sha256 == sdk.SDK_HASH


@pytest.mark.parametrize('changes', [
    {'sha256': '2' * 64},
    {'identity': sdk.WheelIdentity(sdk.NAME, '1.52.0', sdk.FILENAME, sdk.TAG, 'Apache-2.0')},
])
def test_approval_cannot_follow_changed_reserve(changes):
    """A recorded official pair is bound to both reserve digest and version."""
    data = manifest_data()
    data['approvals'] = [approval()]
    manifest = replace(sdk.parse_manifest(data), **changes)
    with pytest.raises(sdk.WheelError, match='approval_required'):
        sdk.check_approval(manifest, '1' * 64)


@pytest.mark.parametrize('field,value', [
    ('version', '1.52.0'), ('reserve_sha256', '2' * 64), ('index_sha256', 'x' * 64),
    ('approved_by', ''), ('approved_by', True), ('approved_at_utc', 'yesterday'),
    ('approved_at_utc', '2026-02-30T12:00:00Z'), ('evidence', '/tmp/private'),
    ('evidence', 'https://user:password@example.org/x'),
    ('evidence', 'https://@example.org/x'), ('evidence', 'https://example.org/x y'),
    ('evidence', 'https://example.org:bad/x'), ('evidence', 'http://example.org/x'),
    ('evidence', 'https:///x'), ('evidence', 'https://example.org:444/x'),
    ('evidence', None),
])
def test_invalid_approval(field, value):
    """Procedural permission cannot become a broad mismatch bypass."""
    data = manifest_data()
    item = approval()
    item[field] = value
    data['approvals'] = [item]
    with pytest.raises(sdk.WheelError):
        sdk.parse_manifest(data)


@pytest.mark.parametrize('items', [[None], [approval(), approval()], [{}]])
def test_approval_missing_or_duplicate(items):
    """Incomplete entries and repeated pairs fail closed."""
    data = manifest_data()
    data['approvals'] = items
    with pytest.raises(sdk.WheelError):
        sdk.parse_manifest(data)


def test_manifest_nested_types_and_extra_fields():
    """Provenance numbers cannot be coerced; future schema fields need review."""
    data = manifest_data()
    data['recovery_source']['matched_code_files'] = 125.0
    with pytest.raises(sdk.WheelError):
        sdk.parse_manifest(data)
    data = manifest_data()
    data['allow_hash_mismatch'] = True
    with pytest.raises(sdk.WheelError):
        sdk.parse_manifest(data)


def test_valid_synthetic_wheel():
    """All ZIP and metadata checks run even when the outer hash is correct."""
    data, policy = wheel()
    assert validate(data, policy).data is data


@pytest.mark.parametrize('mutation', ['hash', 'size', 'license'])
def test_outer_integrity(mutation):
    """Hash and size are checked before ZIP parsing, licence before acceptance."""
    data, policy = wheel()
    if mutation == 'hash':
        policy = replace(policy, sha256='0' * 64)
    elif mutation == 'size':
        policy = replace(policy, size=policy.size + 1)
    with pytest.raises(sdk.WheelError):
        sdk.validate_wheel(data, policy, b'changed' if mutation == 'license' else licence())


@pytest.mark.parametrize('name', [
    '../escape.py', '/absolute.py', 'a/../b.py', 'a/./b.py', 'a//b.py',
    'a\\b.py', 'C:/escape.py', 'a\x00tail.py', 'a/.env', 'a/.idea/x',
    'other-1.0.dist-info/METADATA',
])
def test_unsafe_zip_entries(name):
    """No path escape, hidden credential file or foreign dist-info is accepted."""
    baseline, baseline_policy = wheel(files={'t_tech/safe.py': b'bad'})
    validate(baseline, baseline_policy)
    if '\x00' in name:
        data, _ = wheel(files={'t_tech/x': b'bad'})
        with zipfile.ZipFile(BytesIO(data)) as archive:
            record = archive.read(DIST + 'RECORD')
        data, policy = wheel(files={'t_tech/x_tail.py': b'bad'}, record=record)
        data = data.replace(b't_tech/x_tail.py', b't_tech/x\x00tail.py')
        policy = replace(policy, sha256=sha256(data).hexdigest())
    else:
        data, policy = wheel(files={name: b'bad'})
    with zipfile.ZipFile(BytesIO(data)) as archive:
        contents = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    sdk._verify_record(contents)  # pylint: disable=protected-access
    with pytest.raises(sdk.WheelError, match='^invalid SDK ZIP path/duplicate$'):
        validate(data, policy)


def test_duplicate_zip_entry():
    """ZIP readers must not choose among colliding member names."""
    data, policy = wheel(extra=(('t_tech/invest/__init__.py', b'other'),))
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


def test_symlink_zip_entry():
    """Unix symlink entries cannot redirect installation."""
    info = zipfile.ZipInfo('t_tech/link')
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    data, policy = wheel(extra=((info, b'target'),))
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


@pytest.mark.parametrize('field,value', [
    ('METADATA', b'Name: bad\nVersion: 1.51.0\n'),
    ('METADATA', b'Name: t-tech-investments\nVersion: 1.52.0\n'),
    ('METADATA', b'Name: t-tech-investments\nName: t-tech-investments\n'),
    ('METADATA', b'\xffinvalid'),
    ('WHEEL', b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-none-any\n'),
    ('WHEEL', b'Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'),
    ('WHEEL', b'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n'),
    ('licenses/LICENSE', b'Apache without copyright'),
])
def test_metadata_and_license(field, value):
    """Hash-consistent wrong metadata and removed attribution remain fatal."""
    data, policy = wheel(files={DIST + field: value})
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


@pytest.mark.parametrize('record', [
    b'', b'\xff', b'bad,row\n', b'../bad,sha256=bad,1\n',
    (DIST + 'RECORD,,1\n').encode(),
])
def test_invalid_record(record):
    """RECORD is a complete one-to-one inventory, never an optional hint."""
    data, policy = wheel(record=record)
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


def test_zip_limits():
    """Central-directory limits are checked before decompression."""
    baseline, baseline_policy = wheel(files={f't_tech/{i}.py': b'' for i in range(10)})
    validate(baseline, baseline_policy)
    data, policy = wheel(files={f't_tech/{i}.py': b'' for i in range(513)})
    with zipfile.ZipFile(BytesIO(data)) as archive:
        contents = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    sdk._verify_record(contents)  # pylint: disable=protected-access
    with pytest.raises(sdk.WheelError, match='^invalid SDK ZIP limits$'):
        validate(data, policy)
    data, policy = wheel(files={'t_tech/huge': b'0' * (32 * 1024 * 1024 + 1)})
    with pytest.raises(sdk.WheelError, match='^invalid SDK ZIP limits$'):
        validate(data, policy)


@pytest.mark.parametrize('damage', ['crc', 'encrypted', 'not_zip'])
def test_damaged_zip(damage):
    """Correct outer hashes do not excuse CRC, encryption or invalid ZIP."""
    data, policy = wheel()
    value = bytearray(data)
    if damage == 'not_zip':
        value = bytearray(b'not a zip')
    else:
        offset = value.index(b'PK\x01\x02')
        if damage == 'encrypted':
            flags = struct.unpack_from('<H', value, offset + 8)[0]
            struct.pack_into('<H', value, offset + 8, flags | 1)
        else:
            struct.pack_into('<I', value, offset + 16, 0)
    data = bytes(value)
    policy = replace(policy, size=len(data), sha256=sha256(data).hexdigest())
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


def copy_vendor(tmp_path):
    """A temporary copy never mutates the shared interpreter or original wheel."""
    directory = tmp_path / 'vendor/t-tech-investments'
    directory.mkdir(parents=True)
    for name in (sdk.FILENAME, 'manifest.json', 'LICENSE'):
        (directory / name).write_bytes((ROOT / 'vendor/t-tech-investments' / name).read_bytes())
    return directory


@pytest.mark.parametrize('name', [sdk.FILENAME, 'manifest.json', 'LICENSE'])
@pytest.mark.parametrize('damage', ['missing', 'symlink'])
def test_file_boundaries(tmp_path, name, damage):
    """Missing files and symlink policy/licence/artifact fail before use."""
    directory = copy_vendor(tmp_path)
    target = directory / name
    target.unlink()
    if damage == 'symlink':
        target.symlink_to(ROOT / 'vendor/t-tech-investments' / name)
    with pytest.raises(sdk.WheelError):
        sdk.load_vendored_wheel(tmp_path)


def test_owned_bytes_survive_path_replacement(tmp_path):
    """The returned bytes are the validated snapshot, not a mutable filename."""
    directory = copy_vendor(tmp_path)
    result = sdk.load_vendored_wheel(tmp_path)
    (directory / sdk.FILENAME).write_bytes(b'changed after validation')
    (directory / 'LICENSE').write_bytes(b'changed after validation')
    assert sha256(result.data).hexdigest() == sdk.SDK_HASH
    assert result.license_bytes == licence()


@pytest.mark.parametrize('field,value', [
    ('name', 'other'), ('version', '2.0.0'), ('filename', '../other.whl'),
    ('tag', 'other'), ('license', 'MIT'),
])
def test_typed_policy_identity(field, value):
    """Typed callers cannot relabel validated SDK bytes as a different artifact."""
    data, policy = wheel()
    with pytest.raises(sdk.WheelError):
        validate(data, replace(policy, identity=replace(policy.identity, **{field: value})))


@pytest.mark.parametrize('field', ['METADATA', 'WHEEL', 'RECORD', 'licenses/LICENSE'])
def test_required_zip_members(field):
    """Missing required members are safe structure errors even with a correct hash."""
    data, policy = wheel()
    result = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(result, 'w') as target:
        for name in source.namelist():
            if name != DIST + field:
                target.writestr(name, source.read(name))
    data = result.getvalue()
    policy = replace(policy, size=len(data), sha256=sha256(data).hexdigest())
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


@pytest.mark.parametrize('damage', [
    'hash', 'size', 'algorithm', 'missing', 'duplicate', 'unknown', 'self_hash',
    'noncanonical_size', 'malformed',
])
def test_record_integrity(damage):
    """All rows, including the unhashed self row, have exact identities and sizes."""
    data, _ = wheel()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        text = archive.read(DIST + 'RECORD').decode()
    rows = list(csv.reader(StringIO(text)))
    if damage == 'hash':
        rows[0][1] = 'sha256=' + '0' * 43
    elif damage == 'size':
        rows[0][2] = '1'
    elif damage == 'algorithm':
        rows[0][1] = rows[0][1].replace('sha256=', 'md5=')
    elif damage == 'missing':
        rows.pop(0)
    elif damage == 'duplicate':
        rows.append(rows[0])
    elif damage == 'unknown':
        rows[0][0] = 't_tech/unknown.py'
    elif damage == 'self_hash':
        rows[-1][1] = rows[0][1]
    elif damage == 'noncanonical_size':
        rows[0][2] = '0' + rows[0][2]
    output = StringIO(newline='')
    csv.writer(output).writerows(rows)
    record = b'"unterminated' if damage == 'malformed' else output.getvalue().encode()
    data, policy = wheel(record=record)
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


def test_zip_file_directory_collision():
    """A file cannot also be the parent of another install path."""
    data, policy = wheel(files={'t_tech/invest': b'bad'})
    with pytest.raises(sdk.WheelError):
        validate(data, policy)


@pytest.mark.parametrize('kind', ['directory', 'fifo', 'empty', 'too_large'])
def test_special_or_unbounded_local_file(tmp_path, kind):
    """No blocking FIFO read, directory read or unbounded policy allocation."""
    directory = copy_vendor(tmp_path)
    path = directory / 'manifest.json'
    path.unlink()
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.write_bytes(b'' if kind == 'empty' else b'0' * 65537)
    with pytest.raises(sdk.WheelError):
        sdk.load_vendored_wheel(tmp_path)


@pytest.mark.parametrize('component', ['root', 'vendor', 'sdk_directory'])
def test_parent_symlinks(tmp_path, component):
    """Every traversed directory is opened with no-follow semantics."""
    directory = copy_vendor(tmp_path / 'original')
    root = tmp_path / 'original'
    if component == 'root':
        root = tmp_path / 'link'
        root.symlink_to(tmp_path / 'original', target_is_directory=True)
    elif component == 'vendor':
        root = tmp_path / 'linked_root'
        root.mkdir()
        (root / 'vendor').symlink_to(directory.parent, target_is_directory=True)
    else:
        root = tmp_path / 'linked_root'
        (root / 'vendor').mkdir(parents=True)
        (root / 'vendor/t-tech-investments').symlink_to(directory, target_is_directory=True)
    with pytest.raises(sdk.WheelError):
        sdk.load_vendored_wheel(root)


@pytest.mark.parametrize('text', ['{"schema_version":1,"schema_version":1}',
                                  'not json', '{"approvals": NaN}'])
def test_bad_local_json(tmp_path, text):
    """Ambiguous or malformed JSON cannot change policy semantics."""
    directory = copy_vendor(tmp_path)
    (directory / 'manifest.json').write_text(text)
    with pytest.raises(sdk.WheelError):
        sdk.load_vendored_wheel(tmp_path)


@pytest.mark.parametrize('field,value', [('size', 316189), ('sha256', '0' * 64)])
def test_reserve_pin_cannot_be_redefined(tmp_path, field, value):
    """A rewritten manifest cannot broaden the user's exact-byte permission."""
    directory = copy_vendor(tmp_path)
    data = manifest_data()
    data[field] = value
    (directory / 'manifest.json').write_text(json.dumps(data))
    with pytest.raises(sdk.WheelError, match='reserve pin'):
        sdk.load_vendored_wheel(tmp_path)


def test_file_size_changes_during_read(tmp_path, monkeypatch):
    """A stale stat cannot justify accepting different bytes from an open file."""
    directory = copy_vendor(tmp_path)
    info = (directory / 'manifest.json').stat()
    fake = create_autospec(os.fstat, spec_set=True)
    fake.return_value = os.stat_result(tuple(info)[:6] + (info.st_size - 1,) + tuple(info)[7:])
    monkeypatch.setattr(os, 'fstat', fake)
    with pytest.raises(sdk.WheelError, match='file size'):
        sdk.load_vendored_wheel(tmp_path)


def test_validation_has_no_install_effects(tmp_path, monkeypatch):
    """Even a corrupt wheel never invokes pip or a subprocess."""
    run = create_autospec(subprocess.run, spec_set=True)
    monkeypatch.setattr(subprocess, 'run', run)
    directory = copy_vendor(tmp_path)
    (directory / sdk.FILENAME).write_bytes(b'corrupt')
    with pytest.raises(sdk.WheelError):
        sdk.load_vendored_wheel(tmp_path)
    run.assert_not_called()
