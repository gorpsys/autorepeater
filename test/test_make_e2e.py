"""Make and pytest isolation contracts, without sandbox credentials or a channel."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def make_process_environment():
    """Test child Make defaults independently of the outer Make that launched pytest."""
    inherited = {'PYTHON', 'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES', 'MAKELEVEL'}
    return {key: value for key, value in os.environ.items() if key not in inherited}


@pytest.mark.parametrize('target', ['lint', 'test', 'coverage', 'e2e',
                                    'claude-yandex-archive'])
@pytest.mark.parametrize('local_venv', [False, True])
def test_make_selects_local_environment_for_all_targets(tmp_path, target, local_venv):
    """A local venv is used without activation, with a plain python3 fallback."""
    if local_venv:
        python = tmp_path / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
    result = subprocess.run(['make', '-n', '-f', str(ROOT / 'Makefile'), target],
                            cwd=tmp_path, env=make_process_environment(), capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    expected = '.venv/bin/python' if local_venv else 'python3'
    assert expected in result.stdout


@pytest.mark.parametrize('target', ['lint', 'test', 'coverage', 'e2e'])
def test_make_checks_environment_before_work(target):
    """The dependency guard precedes tools and, for e2e, credential access."""
    result = subprocess.run(['make', '-n', target, f'PYTHON={sys.executable}'],
                            cwd=ROOT, env=make_process_environment(),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert 'scripts/make_environment.py' in result.stdout.splitlines()[0]
    assert target in result.stdout.splitlines()[0]


def test_make_e2e_reuses_token_wrapper_and_finite_default():
    """A dry run proves the command path without reading the local secret."""
    result = subprocess.run(['make', '-n', 'e2e', f'PYTHON={sys.executable}'],
                            cwd=ROOT, env=make_process_environment(),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert '-m scripts.sandbox_token --token-file ".sandbox_token" --' in result.stdout
    assert '-m scripts.sandbox_e2e --timeout "1800"' in result.stdout


def test_default_pytest_collection_never_includes_live_subtree():
    """Default pytest collection cannot discover live scenarios or their fixtures."""
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q'],
                            cwd=ROOT, env={'PATH': os.environ['PATH']},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'test/test_autorepeater.py::' in result.stdout
    assert 'e2e/test_sandbox.py::' not in result.stdout


def test_infrastructure_cli_missing_token_is_safe_failure():
    """Both helper module entries fail without falling back to production credentials."""
    for module, args in [('scripts.sandbox_e2e', []), ('scripts.sandbox_lifecycle', ['probe'])]:
        result = subprocess.run([sys.executable, '-m', module, *args], cwd=ROOT,
                                env={'PATH': os.environ['PATH'],
                                     'INVEST_TOKEN': 'offline-production-marker'},
                                capture_output=True, text=True, check=False)
        assert result.returncode in (1, 2)
        assert 'SANDBOX_TOKEN is required' in result.stderr
        assert 'offline-production-marker' not in result.stderr + result.stdout
        assert 'Traceback' not in result.stderr
