"""Offline contracts for the repository-wide Make test commands."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import create_autospec

import pytest

from scripts import make_offline


REPOSITORY = Path(__file__).resolve().parents[1]


def run_git(*arguments, cwd):
    """Run one git command and fail the test if git reports an error."""
    result = subprocess.run(['git', *arguments], cwd=cwd, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(name='git_repository')
def git_repository_fixture(tmp_path):
    """Create an ordinary repository with tracked and ignored Python files."""
    root = tmp_path / 'project'
    root.mkdir()
    run_git('init', cwd=root)
    run_git('config', 'user.name', 'Offline Tests', cwd=root)
    run_git('config', 'user.email', 'offline@example.test', cwd=root)
    (root / '.gitignore').write_text('generated/\n', encoding='utf-8')
    (root / 'tracked.py').write_text('\n', encoding='utf-8')
    (root / 'nested').mkdir()
    (root / 'nested' / 'tracked.py').write_text('\n', encoding='utf-8')
    (root / 'generated').mkdir()
    (root / 'generated' / 'ignored.py').write_text('\n', encoding='utf-8')
    run_git('add', 'tracked.py', 'nested/tracked.py', '.gitignore', cwd=root)
    run_git('commit', '-m', 'base', cwd=root)
    return root


def test_safe_python_files_selects_tracked_new_and_filters_dangerous_files(
        git_repository):
    """Selection keeps ordinary tracked/new files and excludes generated or unsafe names."""
    (git_repository / 'new.py').write_text('\n', encoding='utf-8')
    (git_repository / 'generated' / 'new.py').write_text('\n', encoding='utf-8')
    (git_repository / 'forbidden').mkdir()
    (git_repository / 'forbidden' / '.env.py').write_text('\n', encoding='utf-8')
    target = git_repository / 'tracked.py'
    (git_repository / 'linked.py').symlink_to(target)

    selected = make_offline.list_safe_python_files(git_repository)

    relative = [path.relative_to(git_repository).as_posix() for path in selected]
    assert relative == ['nested/tracked.py', 'new.py', 'tracked.py']
    assert all(path.is_file() and not path.is_symlink() for path in selected)


def test_safe_python_files_does_not_read_before_rejection(git_repository, monkeypatch):
    """A prohibited path is removed from argv without opening it."""
    calls = []

    def read_text(self, *_arguments, **_keywords):
        """Fail if protected content is opened."""
        calls.append(str(self))
        return ''
    monkeypatch.setattr(Path, 'read_text', read_text)
    (git_repository / 'forbidden').mkdir(exist_ok=True)
    (git_repository / 'forbidden' / '.env.py').write_text('\n', encoding='utf-8')

    selected = make_offline.list_safe_python_files(git_repository)

    assert not calls
    assert [path.name for path in selected] == ['tracked.py', 'tracked.py']


def test_safe_python_files_requires_git(git_repository, monkeypatch):
    """A git failure is reported as a command failure, never silently empty."""
    original_run = subprocess.run

    def failing_run(arguments, **_kwargs):
        """Fail only repository-wide file enumeration."""
        if arguments[0] == 'git' and 'ls-files' in arguments:
            raise subprocess.CalledProcessError(128, arguments)
        return original_run(arguments, check=True, capture_output=True)

    monkeypatch.setattr(make_offline.subprocess, 'run', failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        make_offline.list_safe_python_files(git_repository)


def create_read_text_spy():
    """Build a Path.read_text replacement compatible with instance calls."""
    def spy(self, *arguments, **keywords):
        """Reject every content read."""
        raise AssertionError(f'checked path was read: {self}')

    return spy


def write_python_script(path, log_path, behavior='log'):
    """Create a wrapper that records exact arguments before command behavior."""
    log_path.touch()
    path.parent.mkdir(parents=True, exist_ok=True)
    forwarding = ('import os, sys\n'
                  'guard = os.path.join(os.path.dirname(os.environ["HELPER"]), '
                  '"make_environment.py")\n'
                  'if sys.argv[1] in (os.environ["HELPER"], guard):\n'
                  '    os.execv(os.environ["REAL_PYTHON"], '
                  '[os.environ["REAL_PYTHON"], *sys.argv[1:]])\n')
    log_body = ('with open(os.environ["LOG_PATH"], "ab") as log:\n'
                '    for value in sys.argv[1:]:\n'
                '        log.write(value.encode() + b"\\0")\n'
                '    log.write(b"\\n")\n')
    body = log_body + ('raise SystemExit(2)\n' if behavior == 'fail' else '')
    path.write_text('#!/usr/bin/env python3\n' + forwarding + body,
                    encoding='utf-8')
    path.chmod(0o755)
    return path


@pytest.fixture(name='fake_tools')
def fake_tools_fixture(tmp_path):
    """Provide a safe selected Python without installing coverage executables in PATH."""
    bin_dir = tmp_path / 'tool bin'
    log_path = tmp_path / 'commands.log'
    python = write_python_script(bin_dir / 'python', log_path)
    environment = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                       REAL_PYTHON=sys.executable,
                       HELPER=str(REPOSITORY / 'scripts' / 'make_offline.py'),
                       LOG_PATH=str(log_path))
    return python, log_path, environment


def run_make(target, cwd, environment, arguments=()):
    """Run the real repository Makefile inside a temporary repository."""
    return subprocess.run(
        ['make', '--no-print-directory', '-f', str(REPOSITORY / 'Makefile'),
         target, *arguments],
        cwd=cwd, env=environment, capture_output=True, text=True,
        check=False, timeout=20)


def command_log(log_path):
    """Parse NUL-delimited command records without shell interpretation."""
    records = log_path.read_bytes().split(b'\n')
    return [[part.decode(errors='replace') for part in record.split(b'\0') if part]
            for record in records if record]


def test_make_lint_uses_quoted_python_and_safe_files(tmp_path, git_repository):
    """lint selects safe files and invokes pylint before both flake8 checks."""
    bin_dir = tmp_path / 'python bin'
    log_path = tmp_path / 'commands.log'
    python = write_python_script(bin_dir / 'selected python', log_path)
    environment = dict(os.environ, PYLINTHOME=str(tmp_path / 'pylint home'),
                       REAL_PYTHON=sys.executable,
                       HELPER=str(REPOSITORY / 'scripts' / 'make_offline.py'),
                       LOG_PATH=str(log_path))

    result = run_make('lint', git_repository, environment,
                      [f'PYTHON={python}'])

    assert result.returncode == 0, result.stdout + result.stderr
    selected = make_offline.list_safe_python_files(git_repository)
    expected_python = [str(path) for path in selected]
    assert f'"{python}"' in result.stdout
    records = command_log(log_path)
    assert records[0] == ['-m', 'pylint', *expected_python]
    assert records[1] == ['-m', 'flake8', *expected_python,
                          *make_offline.FLAKE8_FATAL_ARGUMENTS]
    assert records[2] == ['-m', 'flake8', *expected_python,
                          *make_offline.FLAKE8_FULL_ARGUMENTS]
    assert records[3] == ['-m', 'mypy', '--config-file', str(git_repository / 'pyproject.toml')]
    assert Path(tmp_path / 'pylint home').exists()


def test_make_lint_pylint_failure_skips_flake8(tmp_path, git_repository):
    """A pylint failure stops lint before any flake8 check."""
    bin_dir = tmp_path / 'python bin'
    log_path = tmp_path / 'commands.log'
    python = write_python_script(bin_dir / 'python', log_path, behavior='fail')
    result = run_make('lint', git_repository,
                      dict(os.environ, REAL_PYTHON=sys.executable,
                           PYLINTHOME=str(tmp_path / 'pylint home'),
                           HELPER=str(REPOSITORY / 'scripts' / 'make_offline.py'),
                           LOG_PATH=str(log_path)),
                      [f'PYTHON={python}'])

    assert result.returncode != 0
    records = command_log(log_path)
    assert records and records[0][:2] == ['-m', 'pylint']
    assert len(records) == 1


def test_make_test_runs_only_test_directory(git_repository, fake_tools):
    """test invokes the selected Python with pytest and exactly test/."""
    python, log_path, environment = fake_tools
    result = run_make('test', git_repository, environment, [f'PYTHON={python}'])

    assert result.returncode == 0, result.stdout + result.stderr
    assert command_log(log_path) == [
        ['-m', 'pytest', 'test/', '-q'],
    ]


def test_make_coverage_runs_single_suite_and_chained_checks(
        git_repository, fake_tools):
    """coverage runs pytest once, then coverage report and diff-cover in order."""
    python, log_path, environment = fake_tools
    environment = dict(environment, DIFF_BASE='feature-base')
    result = run_make('coverage', git_repository, environment, [f'PYTHON={python}'])

    assert result.returncode == 0, result.stdout + result.stderr
    assert command_log(log_path) == [
        ['-m', 'pytest', '--cov', '--cov-report=term-missing', '--cov-report=xml', '-q'],
        ['-m', 'coverage', 'report', '--fail-under=90'],
        ['-m', 'diff_cover.diff_cover_tool', 'coverage.xml', '--compare-branch=feature-base',
         '--diff-range-notation=..', '--fail-under=90'],
    ]


def test_make_coverage_without_base_uses_origin_master(
        git_repository, fake_tools, monkeypatch):
    """An absent DIFF_BASE uses the documented local comparison branch."""
    monkeypatch.delenv('DIFF_BASE', raising=False)
    python, log_path, environment = fake_tools
    environment = dict(environment)
    environment.pop('DIFF_BASE', None)

    result = run_make('coverage', git_repository, environment, [f'PYTHON={python}'])

    assert result.returncode == 0, result.stdout + result.stderr
    assert command_log(log_path)[-1] == [
        '-m', 'diff_cover.diff_cover_tool', 'coverage.xml', '--compare-branch=origin/master',
        '--diff-range-notation=..', '--fail-under=90',
    ]


@pytest.mark.parametrize('base', ['', '0' * 40])
def test_make_coverage_empty_or_zero_base_uses_empty_git_tree(
        git_repository, fake_tools, base):
    """Invalid SHA inputs retain the existing empty-tree comparison fallback."""
    python, log_path, environment = fake_tools
    environment = dict(environment, DIFF_BASE=base)
    result = run_make('coverage', git_repository, environment, [f'PYTHON={python}'])

    assert result.returncode == 0, result.stdout + result.stderr
    records = command_log(log_path)
    empty_tree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
    assert records[-1] == ['-m', 'diff_cover.diff_cover_tool', 'coverage.xml',
                           f'--compare-branch={empty_tree}',
                           '--diff-range-notation=..', '--fail-under=90']


def test_make_coverage_pytest_failure_stops_and_keeps_artifact(
        git_repository, fake_tools):
    """Failing pytest produces a non-zero status and does not invoke later checks."""
    python, log_path, environment = fake_tools
    python = write_python_script(python, log_path, behavior='fail')
    (git_repository / 'coverage.xml').write_text('<coverage/>', encoding='utf-8')

    result = run_make('coverage', git_repository, dict(
        environment, PATH=environment['PATH']), [f'PYTHON={python}'])

    assert result.returncode == 2
    assert command_log(log_path) == [
        ['-m', 'pytest', '--cov', '--cov-report=term-missing', '--cov-report=xml', '-q'],
    ]
    assert (git_repository / 'coverage.xml').read_text(encoding='utf-8') == '<coverage/>'


def test_workflows_delegate_to_make_without_duplicate_pytest():
    """CI installs existing tools and uses the Make entry points exactly once."""
    pylint_workflow = (REPOSITORY / '.github/workflows/pylint.yml').read_text(
        encoding='utf-8')
    python_workflow = (REPOSITORY / '.github/workflows/python-app.yml').read_text(
        encoding='utf-8')
    requirements = (REPOSITORY / 'requirements-dev.txt').read_text(encoding='utf-8')
    required_tools = ('pylint', 'flake8', 'pytest', 'pytest-cov==7.1.0',
                      'diff-cover==10.6.0', 'mypy==1.18.2')
    for tool in required_tools:
        assert tool in requirements

    for workflow in (pylint_workflow, python_workflow):
        assert 'make setup' in workflow
        assert 'python-version: "3.12"' in workflow
        assert 'pytest --cov' not in workflow
        assert 'pytest test/' not in workflow

    assert 'make lint' in pylint_workflow
    assert 'make lint' in python_workflow
    assert 'make coverage' in python_workflow
    assert 'github.event.pull_request.base.sha || github.event.before' in python_workflow
    assert '--diff-range-notation=..' in make_offline.COVERAGE_DIFF_ARGUMENTS
    assert make_offline.DEFAULT_DIFF_BASE == 'origin/master'


def test_workflow_job_names_are_distinct_for_branch_protection():
    """GitHub checks expose stable names instead of ambiguous build names."""
    pylint_workflow = (REPOSITORY / '.github/workflows/pylint.yml').read_text(
        encoding='utf-8')
    python_workflow = (REPOSITORY / '.github/workflows/python-app.yml').read_text(
        encoding='utf-8')

    assert '  lint:\n    name: Lint\n' in pylint_workflow
    assert ('  offline-checks:\n'
            '    name: Offline checks\n') in python_workflow


@pytest.mark.parametrize('statuses', [(0, 0, 0, 0), (7,), (0, 8), (0, 0, 9), (0, 0, 0, 10)])
def test_lint_stops_on_each_failed_stage(tmp_path, monkeypatch, statuses):
    """Any tool finding, including the full style check, stops the Make target."""
    files = [tmp_path / 'tracked.py', tmp_path / 'new.py']
    select = create_autospec(make_offline.list_safe_python_files, return_value=files)
    command = create_autospec(make_offline.run_command, side_effect=[
        subprocess.CompletedProcess([], status) for status in statuses])
    monkeypatch.setattr(make_offline, 'list_safe_python_files', select)
    monkeypatch.setattr(make_offline, 'run_command', command)
    home = tmp_path / 'pylint-cache'
    monkeypatch.setenv('PYLINTHOME', str(home))

    assert make_offline.run_lint(tmp_path, sys.executable) == statuses[-1]

    select.assert_called_once_with(tmp_path)
    expected = [
        [sys.executable, '-m', 'pylint', *map(str, files)],
        [sys.executable, '-m', 'flake8', *map(str, files),
         *make_offline.FLAKE8_FATAL_ARGUMENTS],
        [sys.executable, '-m', 'flake8', *map(str, files),
         *make_offline.FLAKE8_FULL_ARGUMENTS],
        [sys.executable, '-m', 'mypy', '--config-file', str(tmp_path / 'pyproject.toml')],
    ]
    assert [call.args[:2] for call in command.call_args_list] == [
        (arguments, tmp_path) for arguments in expected[:len(statuses)]]
    assert command.call_args_list[0].args[2]['PYLINTHOME'] == str(home)
    assert home.is_dir()


def test_full_flake8_rejects_style_findings(tmp_path):
    """The actual full-check argv must fail on style issues, not only syntax errors."""
    source = tmp_path / 'style_case.py'
    source.write_text('value=1\n', encoding='utf-8')
    result = subprocess.run([
        sys.executable, '-m', 'flake8', str(source), *make_offline.FLAKE8_FULL_ARGUMENTS],
        cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert 'E225' in result.stdout
    assert '--exit-zero' not in make_offline.FLAKE8_FULL_ARGUMENTS


def test_offline_test_status_and_subprocess_environment(tmp_path, monkeypatch):
    """The offline entry point preserves subprocess status and explicit environment."""
    result = subprocess.CompletedProcess([], 6)
    run = create_autospec(subprocess.run, return_value=result)
    monkeypatch.setattr(make_offline.subprocess, 'run', run)

    assert make_offline.run_test(tmp_path, sys.executable) == 6
    run.assert_called_once_with(
        [sys.executable, '-m', 'pytest', 'test/', '-q'], cwd=str(tmp_path),
        check=False, env=None)
    run.reset_mock()
    environment = {'OFFLINE_TEST': '1'}
    assert make_offline.run_command(['command'], tmp_path, environment) is result
    run.assert_called_once_with(['command'], cwd=str(tmp_path), check=False,
                                env=environment)


@pytest.mark.parametrize('statuses,expected', [
    ((3,), 3),
    ((0, 4), 4),
    ((0, 0, 5), 5),
    ((0, 0, 0), 0),
])
def test_coverage_failures_stop_subsequent_checks(
        tmp_path, monkeypatch, statuses, expected):
    """All tools use the selected interpreter; any failure stops later checks."""
    command = create_autospec(make_offline.run_command, side_effect=[
        subprocess.CompletedProcess([], status) for status in statuses])
    monkeypatch.setattr(make_offline, 'run_command', command)
    monkeypatch.setenv('DIFF_BASE', 'feature-base')

    assert make_offline.run_coverage(tmp_path, sys.executable) == expected

    commands = [call.args[0] for call in command.call_args_list]
    assert commands == [
        [sys.executable, '-m', 'pytest', '--cov', '--cov-report=term-missing',
         '--cov-report=xml', '-q'],
        [sys.executable, '-m', 'coverage', 'report', '--fail-under=90'],
        [sys.executable, '-m', 'diff_cover.diff_cover_tool', 'coverage.xml',
         '--compare-branch=feature-base',
         '--diff-range-notation=..', '--fail-under=90'],
    ][:len(statuses)]
    assert all(call.args[1] == tmp_path for call in command.call_args_list)


@pytest.mark.parametrize('base,expected', [
    (None, 'origin/master'),
    ('', '4b825dc642cb6eb9a060e54bf8d69288fbee4904'),
    ('0' * 40, '4b825dc642cb6eb9a060e54bf8d69288fbee4904'),
    ('feature-base', 'feature-base'),
])
def test_coverage_base_selection(git_repository, monkeypatch, base, expected):
    """Missing local base differs from an explicitly empty CI base."""
    if base is None:
        monkeypatch.delenv('DIFF_BASE', raising=False)
    else:
        monkeypatch.setenv('DIFF_BASE', base)
    command = create_autospec(make_offline.run_command,
                              return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(make_offline, 'run_command', command)

    assert make_offline.run_coverage(git_repository, sys.executable) == 0
    assert command.call_args_list[-1].args == (
        [sys.executable, '-m', 'diff_cover.diff_cover_tool', 'coverage.xml',
         f'--compare-branch={expected}',
         '--diff-range-notation=..', '--fail-under=90'], git_repository)


@pytest.mark.parametrize('target', ['lint', 'test', 'coverage', 'typecheck'])
def test_offline_dispatch_preserves_command_status(tmp_path, monkeypatch, target):
    """Each Make action dispatches once and retains its failure status."""
    handlers = {}
    for name in ('lint', 'test', 'coverage', 'typecheck'):
        handler = create_autospec(getattr(make_offline, f'run_{name}'), return_value=6)
        monkeypatch.setattr(make_offline, f'run_{name}', handler)
        handlers[name] = handler

    assert make_offline.main([
        '--python', sys.executable, '--root', str(tmp_path), target]) == 6
    handlers[target].assert_called_once_with(tmp_path, sys.executable)
    for name, handler in handlers.items():
        if name != target:
            handler.assert_not_called()


def test_unknown_offline_command_fails(capsys):
    """An unsupported command cannot silently report success."""
    assert make_offline.main(['--python', sys.executable, 'unknown']) == 2
    assert capsys.readouterr().err == 'Unknown offline command: unknown\n'
