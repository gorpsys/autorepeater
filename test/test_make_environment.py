"""Dependency checks never access credentials or start live work."""
from unittest.mock import create_autospec
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import make_environment


@pytest.mark.parametrize('target', ['lint', 'test', 'coverage', 'e2e', 'typecheck'])
def test_ready_environment(target, monkeypatch, capsys):
    """Each target checks its own modules, using the interpreter running the check."""
    probe = create_autospec(make_environment.importlib.util.find_spec, return_value=object())
    monkeypatch.setattr(make_environment.importlib.util, 'find_spec', probe)
    assert make_environment.main([target]) == 0
    assert [call.args[0] for call in probe.call_args_list] == list(
        make_environment.MODULES[target])
    assert not capsys.readouterr().err


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
    assert 'make setup' in message and 'requirements-dev.txt' in message


def test_module_entry_reports_failure(tmp_path):
    """The standard-library-only guard works even in Python without installed tools."""
    script = Path(make_environment.__file__)
    result = subprocess.run([sys.executable, '-S', str(script), 'e2e'], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert 'pytest' in result.stderr and 'make setup' in result.stderr
    assert 'Traceback' not in result.stderr
