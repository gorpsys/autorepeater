"""Read local ZIP metadata for the shared pure archive format verifier."""
from zipfile import ZipFile

from scripts import cloud_archive_manifest as inventory


def verify_local_headers(archive: ZipFile) -> None:
    """Inspect bounded headers of an open archive without trusting their size fields."""
    source = archive.fp
    assert source is not None
    for member in archive.infolist():
        source.seek(member.header_offset)
        header = source.read(30 + len(member.filename.encode('utf-8')))
        inventory.verify_local_header(member, header)
