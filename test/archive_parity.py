"""Independent inventory oracle for extracted application acceptance probes."""
from hashlib import sha256
import json
from zipfile import ZipFile


def corrupt_zip_crc(path):
    """Change a real member's declared CRC while retaining its compressed bytes."""
    data = bytearray(path.read_bytes())
    central = data.index(b'PK\x01\x02')
    data[central + 16] ^= 1
    path.write_bytes(data)


def extract_with_parity(project_root, output, extracted, expected):
    """Check exact source bytes, manifest and optional staging before app execution."""
    with ZipFile(output) as archive:
        assert len(archive.namelist()) == len(expected) + 1
        assert set(archive.namelist()) == expected | {'archive-manifest.json'}
        manifest = json.loads(archive.read('archive-manifest.json'))
        assert manifest['schema_version'] == 1
        assert set(manifest['members']) == expected
        for name in expected:
            data = (project_root / name).read_bytes()
            assert archive.read(name) == data
            assert manifest['members'][name] == {
                'size': len(data), 'sha256': sha256(data).hexdigest(),
                'mode': '100755' if name == 'pip3.12' else '100644'}
            assert archive.getinfo(name).external_attr >> 16 == int(
                manifest['members'][name]['mode'], 8)
        archive.extractall(extracted)
    expected = expected | {'archive-manifest.json'}
    assert {path.relative_to(extracted).as_posix()
            for path in extracted.rglob('*') if path.is_file()} == expected
    staging = project_root / 'build/yandex-function'
    if staging.exists():
        assert {path.relative_to(staging).as_posix()
                for path in staging.rglob('*') if path.is_file()} == expected
        for name in expected:
            assert (staging / name).read_bytes() == (extracted / name).read_bytes()
