"""Build the cloud bundle from flat modules and the bundled JSON config tree."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def _config_files(directory):
    for path in sorted(directory.iterdir()):
        if path.name.startswith('.') or path.is_symlink():
            continue
        if path.is_dir():
            yield from _config_files(path)
        elif path.is_file() and path.suffix == '.json':
            yield path


def archive_files(project_root):
    """Select only application roots, flat Python modules and nonhidden JSON."""
    root = Path(project_root)
    files = [root / name for name in ('main.py', 'handler.py', 'requirements.txt')]
    files.extend(path for path in sorted((root / 'autorepeater').glob('*.py'))
                 if not path.name.startswith('.') and not path.is_symlink())
    files.extend(_config_files(root / 'autorepeater/configs'))
    return files


def build_archive(project_root, output_path=None):
    """Replace the ZIP, preserving selected files' relative paths and bytes."""
    root = Path(project_root)
    output = Path(output_path) if output_path is not None else root / 'build/yandex-function.zip'
    files = archive_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    return output


def main(argv=None):
    """Build from the script's project unless another source/output is explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    build_archive(args.project_root, args.output)


if __name__ == '__main__':
    main()
