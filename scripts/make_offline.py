"""Build offline Make commands from repository state using exact argv."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


DEFAULT_DIFF_BASE = 'origin/master'
TEST_PYTEST_ARGUMENTS = ['--cov', '--cov-report=term-missing',
                         '--cov-report=xml', '-q']
COVERAGE_REPORT_ARGUMENTS = ['report', '--fail-under=90']
COVERAGE_DIFF_ARGUMENTS = ['coverage.xml', '--diff-range-notation=..',
                           '--fail-under=90']
FLAKE8_FATAL_ARGUMENTS = ['--count', '--select=E9,F63,F7,F82',
                          '--show-source', '--statistics']
FLAKE8_FULL_ARGUMENTS = ['--count', '--max-complexity=10', '--max-line-length=127',
                         '--statistics']
PROTECTED_PARTS = {
    '.cache', '.config', '.cursor', '.env', '.gnupg', '.idea', '.kube',
    '.mozilla', '.netrc', '.pki', '.pypirc', '.ssh', '.vscode',
}
PROTECTED_PATH_PREFIXES = (
    'Library/Keychains', 'Library/Caches/JetBrains',
    'Library/Logs/JetBrains', 'Library/Preferences/jetbrains',
    '.cargo/credentials.toml', '.docker/config.json', '.local/share/JetBrains',
)


def list_safe_python_files(root: Path) -> list[Path]:
    """Return tracked and nonignored untracked Python files without unsafe reads."""
    listing = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '--cached', '--others',
         '--exclude-standard', '-z', '--', '*.py'],
        check=True, capture_output=True)
    selected = []
    for raw_name in listing.stdout.split(b'\0'):
        if not raw_name:
            continue
        relative = Path(os.fsdecode(raw_name))
        if is_protected(relative):
            continue
        absolute = root / relative
        if has_symlink_path(absolute) or not absolute.is_file():
            continue
        selected.append(absolute)
    return sorted(selected)


def is_protected(relative: Path) -> bool:
    """Reject known sensitive components and path-like credentials."""
    parts = relative.parts
    if any(part.startswith('.env') or part in PROTECTED_PARTS for part in parts):
        return True
    posix_name = relative.as_posix()
    return posix_name.startswith(PROTECTED_PATH_PREFIXES)


def has_symlink_path(path: Path) -> bool:
    """Check the file and every parent component before any content read."""
    return any(item.is_symlink() for item in path.parents) or path.is_symlink()


def run_command(arguments: list[str], root: Path,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess without shell interpretation and propagate its status."""
    return subprocess.run(arguments, cwd=str(root), check=False, env=environment)


def run_lint(root: Path, python: str) -> int:
    """Run pylint, then syntax and full flake8 checks; every finding blocks success."""
    files = [str(path) for path in list_safe_python_files(root)]
    pylint_home = os.environ.get('PYLINTHOME',
                                 str(Path(tempfile.gettempdir()) / 'autorepeater-pylint'))
    pylint_environment = dict(os.environ, PYLINTHOME=pylint_home)
    Path(pylint_home).mkdir(parents=True, exist_ok=True)
    result = run_command([python, '-m', 'pylint', *files], root,
                         pylint_environment)
    if result.returncode != 0:
        return result.returncode

    for arguments in (FLAKE8_FATAL_ARGUMENTS, FLAKE8_FULL_ARGUMENTS):
        result = run_command(
            [python, '-m', 'flake8', *files, *arguments], root)
        if result.returncode != 0:
            return result.returncode
    return run_typecheck(root, python)


def run_typecheck(root: Path, python: str) -> int:
    """Check the explicit initial module scope using the repository's mypy config."""
    return run_command([python, '-m', 'mypy', '--config-file', str(root / 'pyproject.toml')],
                       root).returncode


def run_test(root: Path, python: str) -> int:
    """Run the complete offline test/ suite; root-level e2e remains excluded."""
    return run_command([python, '-m', 'pytest', 'test/', '-q'], root).returncode


def diff_base() -> str | None:
    """Return the requested diff base or a safe non-empty fallback."""
    base = os.environ.get('DIFF_BASE', DEFAULT_DIFF_BASE)
    if not base or base == '0' * 40:
        return None
    return base


def run_coverage(root: Path, python: str) -> int:
    """Run the offline test suite once and enforce total and diff coverage."""
    pytest_result = run_command(
        [python, '-m', 'pytest', *TEST_PYTEST_ARGUMENTS], root)
    if pytest_result.returncode != 0:
        return pytest_result.returncode

    result = run_command([python, '-m', 'coverage', *COVERAGE_REPORT_ARGUMENTS], root)
    if result.returncode != 0:
        return result.returncode

    selected_base = diff_base()
    if selected_base is None:
        hash_result = subprocess.run(
            ['git', '-C', str(root), 'hash-object', '-t', 'tree', '/dev/null'],
            check=True, capture_output=True, text=True)
        selected_base = hash_result.stdout.strip()
    diff_arguments = [*COVERAGE_DIFF_ARGUMENTS]
    diff_arguments.insert(1, f'--compare-branch={selected_base}')
    result = run_command([python, '-m', 'diff_cover.diff_cover_tool', *diff_arguments], root)
    return result.returncode


def main(arguments: list[str]) -> int:
    """Dispatch one offline Make command."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True)
    parser.add_argument('--root', type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument('command')
    parsed = parser.parse_args(arguments)
    if parsed.command == 'lint':
        return run_lint(parsed.root, parsed.python)
    if parsed.command == 'test':
        return run_test(parsed.root, parsed.python)
    if parsed.command == 'coverage':
        return run_coverage(parsed.root, parsed.python)
    if parsed.command == 'typecheck':
        return run_typecheck(parsed.root, parsed.python)
    print(f'Unknown offline command: {parsed.command}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
