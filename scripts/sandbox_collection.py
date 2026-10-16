"""Collect the whole tested checkout without live fixtures or selection options."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.sandbox_retry_evidence import RetryFailure, checked_sha, read_collection


class CollectionRecorder:
    """A separate collect-only process records deselection/errors before acceptance."""

    def __init__(self, path: Path, sha: str) -> None:
        self.path = path
        self.sha = sha
        self.deselected = False

    def pytest_deselected(self, items) -> None:
        """Any filtering invalidates full-suite acceptance."""
        if items:
            self.deselected = True

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        """Never emit a valid manifest for a partial or erroneous collection."""
        if (exitstatus != 0 or session.testsfailed or self.deselected
                or not session.config.option.collectonly or not session.items):
            return
        self.path.write_text(json.dumps({
            'version': 1, 'sha': self.sha,
            'nodeids': [item.nodeid for item in session.items],
        }), encoding='utf-8')


def pytest_configure(config) -> None:
    """Loaded explicitly only by the fixed collect-only subprocess below."""
    path = os.environ.get('E2E_COLLECTION_REPORT')
    if path:
        config.pluginmanager.register(CollectionRecorder(
            Path(path), checked_sha(os.environ.get('E2E_TESTED_SHA', ''))))


def collect_suite(root: Path, sha: str, output: Path) -> None:
    """Own the full command; never accept caller pytest args or inherited filters."""
    sha = checked_sha(sha)
    actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                            capture_output=True, text=True, timeout=10).stdout.strip()
    if (actual != sha or output.exists() or output.is_symlink()
            or any(parent.is_symlink() for parent in output.parents)):
        raise RetryFailure('fresh collection of exact checkout required')
    clean = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', '*.py', '*.json',
                            '*.toml', '*.yml', '*.yaml', '*.ini', '*.cfg',
                            'Makefile', 'requirements*.txt',
                            ':(exclude)**/.env', ':(exclude)**/.env.*',
                            ':(exclude)**/.idea/**'], cwd=root, check=False, timeout=10)
    untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '--',
                                '*.py', '*.json', '*.toml', '*.yml', '*.yaml', '*.ini', '*.cfg',
                                ':(exclude)**/.env', ':(exclude)**/.env.*',
                                ':(exclude)**/.idea/**'], cwd=root, check=True,
                               capture_output=True, text=True, timeout=10)
    if clean.returncode != 0 or untracked.stdout.strip():
        raise RetryFailure('collection requires committed source files')
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTEST_ADDOPTS='', PYTEST_PLUGINS='',
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', E2E_TESTED_SHA=sha,
                       E2E_COLLECTION_REPORT=str(output.resolve()))
    environment.pop('E2E_ATTEMPT_REPORT', None)
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-p', 'scripts.sandbox_collection',
        '--collect-only', '-q', '-o', 'addopts=', '--confcutdir=e2e', 'e2e/',
    ], cwd=root, env=environment, check=False, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RetryFailure('full sandbox collection failed')
    read_collection(output, sha)


def main(argv: list[str] | None = None) -> int:
    """The offline collector never imports a client or reads sandbox credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        collect_suite(Path.cwd(), args.sha, args.output)
    except (RetryFailure, OSError, subprocess.SubprocessError):
        print('Full exact-SHA sandbox collection failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
