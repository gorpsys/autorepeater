"""Build the cloud bundle from flat modules and the bundled JSON config tree."""
import argparse
from dataclasses import asdict
from email.parser import BytesParser
from io import BytesIO
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, TYPE_CHECKING
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import cloud_archive_inputs as inputs
from scripts import cloud_archive_manifest as inventory
from scripts import cloud_archive_zip

if TYPE_CHECKING:
    from scripts.sdk_wheel import ValidatedWheel


def archive_files(project_root: str | Path) -> list[Path]:
    """Select only application roots, flat Python modules and nonhidden JSON."""
    return inputs.archive_files(project_root)


def _vendored_entries(root: Path, requirements: bytes) -> dict[str, bytes]:
    """Prepare locked requirements and owned SDK bytes before touching the output."""
    from scripts import runtime_lock, sdk_wheel  # pylint: disable=import-outside-toplevel

    document = runtime_lock.load_lock(root)
    runtime = runtime_lock.render_requirements(document)['requirements-runtime.txt'].encode()
    runtime_lock.require(requirements == runtime,
                         'generated requirements drift')
    prefix = 'vendor/t-tech-investments/'
    manifest = runtime_lock.read_json(root / prefix / 'manifest.json')
    policy = sdk_wheel.parse_manifest(manifest)
    selected = sdk_wheel.acquire_sdk(root, 'vendored')
    wheel = selected.wheel
    runtime_lock.require(selected.source == sdk_wheel.Source.VENDORED
                         and selected.reason == sdk_wheel.Reason.EXPLICIT_VENDORED,
                         'SDK selection policy drift')
    package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    runtime_lock.require(package['version'] == wheel.manifest.identity.version and any(
        a['sha256'] == wheel.manifest.sha256 and a['size'] == len(wheel.data)
        and a['filename'] == wheel.manifest.identity.filename
        and a['tag'] == wheel.manifest.identity.tag for a in package['artifacts']),
        'SDK artifact outside runtime lock')
    runtime_lock.require(wheel.manifest == policy, 'SDK selection policy drift')
    with ZipFile(BytesIO(wheel.data)) as archive:
        metadata = BytesParser().parsebytes(archive.read(
            f't_tech_investments-{wheel.manifest.identity.version}.dist-info/METADATA'))
    runtime_lock.require(sorted(set(metadata.get_all('Requires-Dist', [])))
                         == package['requires_dist'], 'SDK dependency metadata drift')
    return vendored_files(document, wheel, manifest)


def vendored_files(document: dict[str, Any], wheel: 'ValidatedWheel',
                   manifest: dict[str, Any]) -> dict[str, bytes]:
    """One pure formatter serves construction and comparison of the vendored bundle."""
    from scripts import runtime_lock  # pylint: disable=import-outside-toplevel
    prefix = 'vendor/t-tech-investments/'
    runtime = runtime_lock.render_requirements(document)['requirements-runtime.txt'].encode()
    requirements = (f'--index-url {document["sources"]["pypi"]}\n'
                    f'./{prefix}{wheel.manifest.identity.filename}'
                    f' --hash=sha256:{wheel.manifest.sha256}\n'
                    '-r requirements-runtime.txt\n')
    return {
        'requirements.txt': requirements.encode(), 'requirements-runtime.txt': runtime,
        prefix + wheel.manifest.identity.filename: wheel.data,
        prefix + 'LICENSE': wheel.license_bytes,
        prefix + 'manifest.json': runtime_lock.canonical_bytes(manifest),
    }


def _default_entries(root: Path) -> dict[str, bytes]:
    """Give cloud pip pinned CA bytes; the SDK itself is still installed from its index."""
    from scripts import cloud_build_tls  # pylint: disable=import-outside-toplevel
    return cloud_build_tls.prepare(root)


def _merge_entries(application: dict[str, bytes], prepared: dict[str, bytes], *,
                   vendored: bool = False) -> dict[str, bytes]:
    permitted = {'requirements.txt', 'requirements-runtime.txt'} if vendored else set()
    inventory.require(not (set(application) & set(prepared)) - permitted,
                      'cloud archive entry collision')
    entries = {**application, **prepared}
    inventory.check_names([*entries, inventory.MANIFEST])
    return entries


def _write_archive(entries: dict[str, bytes], output: Path) -> None:
    with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries, key=lambda name: name.encode('utf-8')):
            info = ZipInfo(name, date_time=inventory.TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = int(inventory.file_mode(name), 8) << 16
            archive.writestr(info, entries[name], compress_type=ZIP_DEFLATED, compresslevel=9)


def _check_archive(entries: dict[str, bytes], output: Path) -> None:
    expected = set(entries)
    with ZipFile(output) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError('invalid cloud archive members or CRC')
        try:
            actual = {name: archive.read(name) for name in names}
        except BadZipFile as error:
            raise ValueError('invalid cloud archive CRC') from error
        if actual != entries:
            raise ValueError('cloud archive content drift')
        inventory.verify(actual, archive.infolist(), comment=archive.comment)
        cloud_archive_zip.verify_local_headers(archive)


def _prepared_entries(root: Path, snapshot: inputs.ApplicationSnapshot, *,
                      use_vendored_sdk: bool, offline_archive: bool) -> dict[str, bytes]:
    """Combine the checked application snapshot with the existing payload owners."""
    application = {file.name: file.data for file in snapshot.files}
    if offline_archive:
        from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
        application = {n: data for n, data in application.items()
                       if n not in ('requirements.txt', 'requirements-runtime.txt')}
        return _merge_entries(application, cloud_dependencies.prepare_payload(
            root, dict(application), use_vendored_sdk=use_vendored_sdk))
    return _merge_entries(application, _vendored_entries(
        root, application['requirements-runtime.txt']) if use_vendored_sdk
        else _default_entries(root), vendored=use_vendored_sdk)


# Preserve the two existing and two additive public keyword-only options.
# pylint: disable-next=too-many-arguments
def build_archive(project_root: str | Path, output_path: str | Path | None = None, *,
                  use_vendored_sdk: bool = False, offline_archive: bool = False,
                  release: bool = False, expected_sha: str = '') -> Path:
    """Replace the ZIP, preserving selected files' relative paths and bytes."""
    root = inputs.project_path(project_root)
    if release and offline_archive and not use_vendored_sdk:
        raise ValueError('offline release requires vendored SDK')
    output = Path(output_path) if output_path is not None else root / 'build/yandex-function.zip'
    snapshot = inputs.snapshot(root, release=release, expected_sha=expected_sha)
    if release:
        inputs.recheck(root, snapshot, expected_sha=expected_sha)
    entries = _prepared_entries(root, snapshot, use_vendored_sdk=use_vendored_sdk,
                                offline_archive=offline_archive)
    profile = 'offline' if offline_archive else 'vendored' if use_vendored_sdk else 'default'
    policy = 'vendored' if use_vendored_sdk else 'auto' if offline_archive else 'cloud-index'
    entries[inventory.MANIFEST] = inventory.render(
        entries, mode='release' if release else 'local', profile=profile,
        sdk_policy=policy, source=asdict(snapshot.source))
    for file in snapshot.files:
        inputs.validate_path(file.path, root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.yandex-archive-', dir=output.parent) as directory:
        staged = Path(directory) / output.name
        _write_archive(entries, staged)
        _check_archive(entries, staged)
        if release:
            inputs.recheck(root, snapshot, expected_sha=expected_sha)
        staged.replace(output)
    return output


def main(argv: list[str] | None = None) -> None:
    """Build from the script's project unless another source/output is explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--use-vendored-sdk', action='store_true')
    parser.add_argument('--offline-archive', action='store_true')
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--expected-sha', default='')
    args = parser.parse_args(argv)
    if args.use_vendored_sdk or args.offline_archive:
        logging.basicConfig(level=logging.WARNING)
    build_archive(args.project_root, args.output, use_vendored_sdk=args.use_vendored_sdk,
                  offline_archive=args.offline_archive, release=args.release,
                  expected_sha=args.expected_sha)


if __name__ == '__main__':
    main()
