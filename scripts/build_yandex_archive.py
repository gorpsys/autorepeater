"""Build the cloud bundle from flat modules and the bundled JSON config tree."""
import argparse
from collections.abc import Iterator
from email.parser import BytesParser
from io import BytesIO
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

if TYPE_CHECKING:
    from scripts.sdk_wheel import ValidatedWheel


def _config_files(directory: Path) -> Iterator[Path]:
    for path in sorted(directory.iterdir()):
        if path.name.startswith('.') or path.is_symlink():
            continue
        if path.is_dir():
            yield from _config_files(path)
        elif path.is_file() and path.suffix == '.json':
            yield path


def archive_files(project_root: str | Path) -> list[Path]:
    """Select only application roots, flat Python modules and nonhidden JSON."""
    root = Path(project_root)
    files = [root / name for name in (
        'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt')]
    files.extend(path for path in sorted((root / 'autorepeater').glob('*.py'))
                 if not path.name.startswith('.') and not path.is_symlink())
    files.extend(_config_files(root / 'autorepeater/configs'))
    return files


def _vendored_entries(root: Path) -> dict[str, bytes]:
    """Prepare locked requirements and owned SDK bytes before touching the output."""
    # Direct script/runpy entrypoints do not put the script's project on sys.path.
    helper_root = str(Path(__file__).resolve().parents[1])
    if helper_root not in sys.path:
        sys.path.insert(0, helper_root)
    from scripts import runtime_lock, sdk_wheel  # pylint: disable=import-outside-toplevel

    document = runtime_lock.load_lock(root)
    runtime = runtime_lock.render_requirements(document)['requirements-runtime.txt'].encode()
    runtime_lock.require((root / 'requirements-runtime.txt').read_bytes() == runtime,
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


def _write_archive(root: Path, files: list[Path], entries: dict[str, bytes], output: Path) -> None:
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for path in files:
            name = path.relative_to(root).as_posix()
            if name not in entries:
                archive.write(path, name)
        for name, data in entries.items():
            archive.writestr(name, data)


def _check_archive(root: Path, files: list[Path], entries: dict[str, bytes], output: Path) -> None:
    expected = {path.relative_to(root).as_posix() for path in files} | set(entries)
    with ZipFile(output) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected or archive.testzip() is not None:
            raise ValueError('invalid cloud archive members or CRC')
        if any(archive.read(name) != data for name, data in entries.items()):
            raise ValueError('cloud archive content drift')


def build_archive(project_root: str | Path, output_path: str | Path | None = None, *,
                  use_vendored_sdk: bool = False, offline_archive: bool = False) -> Path:
    """Replace the ZIP, preserving selected files' relative paths and bytes."""
    root = Path(project_root)
    output = Path(output_path) if output_path is not None else root / 'build/yandex-function.zip'
    files = archive_files(root)
    if offline_archive:
        helper_root = str(Path(__file__).resolve().parents[1])
        if helper_root not in sys.path:
            sys.path.insert(0, helper_root)
        from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
        files = [p for p in files if p.name not in ('requirements.txt', 'requirements-runtime.txt')]
        if any(p.is_symlink() or any(parent.is_symlink() for parent in p.parents) for p in files):
            raise ValueError('unsafe application path')
        application = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
        entries = {**cloud_dependencies.prepare_payload(
            root, application, use_vendored_sdk=use_vendored_sdk), **application}
        files = []
    else:
        entries = _vendored_entries(root) if use_vendored_sdk else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    if use_vendored_sdk or offline_archive:
        with TemporaryDirectory(prefix='.yandex-archive-', dir=output.parent) as directory:
            staged = Path(directory) / output.name
            _write_archive(root, files, entries, staged)
            _check_archive(root, files, entries, staged)
            staged.replace(output)
    else:
        _write_archive(root, files, entries, output)
    return output


def main(argv: list[str] | None = None) -> None:
    """Build from the script's project unless another source/output is explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--use-vendored-sdk', action='store_true')
    parser.add_argument('--offline-archive', action='store_true')
    args = parser.parse_args(argv)
    if args.use_vendored_sdk or args.offline_archive:
        logging.basicConfig(level=logging.WARNING)
    build_archive(args.project_root, args.output, use_vendored_sdk=args.use_vendored_sdk,
                  offline_archive=args.offline_archive)


if __name__ == '__main__':
    main()
