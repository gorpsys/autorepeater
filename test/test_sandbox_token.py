"""Offline checks for the local sandbox credential launcher."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import create_autospec

import pytest

from scripts import sandbox_token


@pytest.fixture(autouse=True)
def no_environment_token(monkeypatch):
    """Never consume the developer's credentials in offline tests."""
    monkeypatch.delenv('SANDBOX_TOKEN', raising=False)


def test_environment_has_priority(tmp_path, monkeypatch):
    """CI credentials do not require reading a local file or terminal."""
    monkeypatch.setenv('SANDBOX_TOKEN', 'environment-token')
    reader = create_autospec(Path.read_text, spec_set=True)
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True)
    monkeypatch.setattr(Path, 'read_text', reader)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    assert sandbox_token.load_token(tmp_path / 'token') == 'environment-token'
    reader.assert_not_called()
    prompt.assert_not_called()


@pytest.mark.parametrize('contents', ['file-token', 'file-token\n', 'file-token\r\n'])
@pytest.mark.parametrize('environment', [None, '', ' \t '])
def test_file_token(tmp_path, monkeypatch, contents, environment):
    """A single local token may have an ordinary final newline."""
    if environment is not None:
        monkeypatch.setenv('SANDBOX_TOKEN', environment)
    path = tmp_path / 'token'
    path.write_text(contents, encoding='utf-8')
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    assert sandbox_token.load_token(path) == 'file-token'
    prompt.assert_not_called()


@pytest.mark.parametrize('contents', [None, '', '\n \t\n'])
def test_missing_or_empty_file_prompts(tmp_path, monkeypatch, contents):
    """Only absent or blank credentials permit an interactive fallback."""
    path = tmp_path / 'token'
    if contents is not None:
        path.write_text(contents, encoding='utf-8')
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True,
                             return_value='entered-token')
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    assert sandbox_token.load_token(path) == 'entered-token'
    prompt.assert_called_once_with('Sandbox token: ')
    assert 'SANDBOX_TOKEN' not in os.environ


def test_noninteractive_missing_token_is_an_error(tmp_path, monkeypatch):
    """CI must not hang on input or silently skip its tests."""
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    with pytest.raises(ValueError, match='SANDBOX_TOKEN'):
        sandbox_token.load_token(tmp_path / 'missing')
    prompt.assert_not_called()


@pytest.mark.parametrize('error', [PermissionError(), IsADirectoryError(),
                                   UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')])
def test_bad_file_does_not_fall_back(tmp_path, monkeypatch, error):
    """A broken or unreadable file is not equivalent to an absent one."""
    reader = create_autospec(Path.read_text, spec_set=True, side_effect=error)
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True)
    monkeypatch.setattr(Path, 'read_text', reader)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    with pytest.raises(ValueError, match='cannot read'):
        sandbox_token.load_token(tmp_path / 'token')
    prompt.assert_not_called()


@pytest.mark.parametrize('origin,value', [
    (origin, value)
    for origin in ('environment', 'file', 'prompt')
    for value in ('two tokens', 'first\nsecond', 'token\0')
    if not (origin == 'environment' and '\0' in value)
])
def test_invalid_token_is_not_disclosed(tmp_path, monkeypatch, origin, value):
    """Malformed values never appear in diagnostics or a child environment."""
    path = tmp_path / 'token'
    if origin == 'environment':
        monkeypatch.setenv('SANDBOX_TOKEN', value)
    elif origin == 'file':
        path.write_text(value, encoding='utf-8')
    else:
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(sandbox_token.getpass, 'getpass', lambda _: value)
    with pytest.raises(ValueError, match='single token') as raised:
        sandbox_token.load_token(path)
    assert value not in str(raised.value)


@pytest.mark.parametrize('error', [EOFError(), KeyboardInterrupt(),
                                   sandbox_token.getpass.GetPassWarning('unsafe input')])
def test_prompt_failure_stops_launch(tmp_path, monkeypatch, error):
    """Input failure or unavailable echo protection cannot launch tests."""
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True, side_effect=error)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    with pytest.raises(ValueError, match='cannot securely read'):
        sandbox_token.load_token(tmp_path / 'token')


def test_empty_prompt_is_rejected(tmp_path, monkeypatch):
    """An empty answer is not a valid sandbox credential."""
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', lambda _: '')
    with pytest.raises(ValueError, match='single token'):
        sandbox_token.load_token(tmp_path / 'token')


def test_main_passes_token_only_in_child_environment(monkeypatch):
    """The command arguments and parent environment do not gain the token."""
    monkeypatch.setenv('SANDBOX_TOKEN', 'environment-token')
    execute = create_autospec(os.execvpe, spec_set=True)
    monkeypatch.setattr(os, 'execvpe', execute)
    assert sandbox_token.main(['--', 'child', 'argument']) == 0
    execute.assert_called_once_with('child', ['child', 'argument'], dict(os.environ))


def test_missing_command_does_not_prompt(monkeypatch):
    """Command validation happens before credential acquisition."""
    prompt = create_autospec(sandbox_token.getpass.getpass, spec_set=True)
    monkeypatch.setattr(sandbox_token.getpass, 'getpass', prompt)
    with pytest.raises(SystemExit) as raised:
        sandbox_token.main([])
    assert raised.value.code == 2
    prompt.assert_not_called()


@pytest.mark.parametrize('failure', ['missing_token', 'missing_executable'])
def test_main_reports_safe_errors(tmp_path, monkeypatch, capsys, failure):
    """Failure reports never include credentials or arbitrary OS error text."""
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    if failure == 'missing_executable':
        monkeypatch.setenv('SANDBOX_TOKEN', 'environment-token')
        execute = create_autospec(os.execvpe, spec_set=True,
                                  side_effect=OSError('sensitive OS detail'))
        monkeypatch.setattr(os, 'execvpe', execute)
    result = sandbox_token.main(['--token-file', str(tmp_path / 'missing'), '--', 'child'])
    assert result == (2 if failure == 'missing_token' else 127)
    output = capsys.readouterr()
    assert not output.out
    assert output.err
    assert 'environment-token' not in output.err
    assert 'sensitive OS detail' not in output.err


def test_module_entrypoint(monkeypatch):
    """The module supports make's python -m launcher."""
    monkeypatch.setenv('SANDBOX_TOKEN', 'environment-token')
    monkeypatch.setattr(sys, 'argv', ['sandbox_token', '--', 'child'])
    execute = create_autospec(os.execvpe, spec_set=True)
    monkeypatch.setattr(os, 'execvpe', execute)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(Path(sandbox_token.__file__)), run_name='__main__')
    assert raised.value.code == 0
    execute.assert_called_once()


def test_make_credential_prefix(tmp_path):
    """Make reads the file at runtime without printing its contents."""
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'token file'
    path.write_text('file-token\n', encoding='utf-8')
    makefile = tmp_path / 'probe.mk'
    makefile.write_text(
        'include Makefile\n'
        'probe:\n'
        '\t@$(SANDBOX_RUN) $(PYTHON) -c '
        '"import os; assert os.environ[\'SANDBOX_TOKEN\'] == \'file-token\'"\n',
        encoding='utf-8')
    result = subprocess.run(
        ['make', '-f', str(makefile), 'probe', f'PYTHON={sys.executable}',
         f'SANDBOX_TOKEN_FILE={path}'], cwd=root, env=dict(os.environ),
        capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'file-token' not in result.stdout + result.stderr


def test_secret_path_is_ignored():
    """The chosen repository-root credential file cannot be added normally."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['git', 'check-ignore', '.sandbox_token'], cwd=root,
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == '.sandbox_token'
