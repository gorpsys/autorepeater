"""Dependency checks never access credentials or start live work."""
from unittest.mock import create_autospec
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import make_environment
from scripts import install_dependencies


@pytest.mark.parametrize('target', ['lint', 'test', 'coverage', 'e2e', 'typecheck'])
def test_ready_environment(target, monkeypatch, capsys):
    """Each target checks its own modules, using the interpreter running the check."""
    probe = create_autospec(make_environment.importlib.util.find_spec, return_value=object())
    monkeypatch.setattr(make_environment.importlib.util, 'find_spec', probe)
    guard = create_autospec(install_dependencies.check_environment, spec_set=True)
    monkeypatch.setattr(install_dependencies, 'check_environment', guard)
    assert make_environment.main([target]) == 0
    assert [call.args[0] for call in probe.call_args_list] == list(
        make_environment.MODULES[target])
    assert not capsys.readouterr().err
    guard.assert_called_once_with(Path(make_environment.__file__).resolve().parents[1])


@pytest.mark.parametrize('target, missing', [
    ('e2e', 'pytest'), ('test', 't_tech'), ('lint', 'pylint'), ('coverage', 'diff_cover'),
    ('lint', 'mypy'), ('typecheck', 'mypy'), ('test', 'mypy'), ('coverage', 'mypy'),
])
def test_missing_dependency_reports_selected_interpreter(target, missing, monkeypatch, capsys):
    """A failure explains installation before the token wrapper or any API call."""
    monkeypatch.setattr(make_environment.importlib.util, 'find_spec',
                        lambda name: None if name == missing else object())
    monkeypatch.setattr(make_environment.sys, 'executable', '/test interpreter/python')
    assert make_environment.main([target]) == 2
    message = capsys.readouterr().err
    assert missing in message and '/test interpreter/python' in message
    assert 'make setup' in message and 'scripts.install_dependencies --development' in message
    assert 'pip install' not in message


def test_receipt_failure_blocks_ready_modules(monkeypatch, capsys):
    """Available imports do not attest the selected SDK artifact."""
    probe = create_autospec(make_environment.importlib.util.find_spec, spec_set=True,
                            return_value=object())
    guard = create_autospec(install_dependencies.check_environment, spec_set=True,
                            side_effect=ValueError('stale receipt'))
    monkeypatch.setattr(make_environment.importlib.util, 'find_spec', probe)
    monkeypatch.setattr(install_dependencies, 'check_environment', guard)
    assert make_environment.main(['e2e']) == 2
    assert 'runtime receipt' in capsys.readouterr().err


def test_module_entry_reports_failure(tmp_path):
    """The standard-library-only guard works even in Python without installed tools."""
    script = Path(make_environment.__file__)
    result = subprocess.run([sys.executable, '-S', str(script), 'e2e'], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert 'pytest' in result.stderr and 'make setup' in result.stderr
    assert 'Traceback' not in result.stderr
