"""Offline process limits and serialization guards."""
import subprocess
import os
import signal
from uuid import UUID
from unittest.mock import create_autospec

import pytest

from scripts import sandbox_e2e


class ProcessSpec(subprocess.Popen):
    """Popen creates pid dynamically; expose it without starting a process."""
    pid = 456


@pytest.mark.parametrize('value', ['0', '3601', '-1', 'garbage'])
def test_reject_invalid_timeout(value):
    """Invalid timeout values fail before launching pytest or opening accounts."""
    with pytest.raises(SystemExit):
        sandbox_e2e.main(['--timeout', value])


def test_ci_requires_parent_serialization(monkeypatch):
    """CI cannot clean or trade without an explicit parent serialization contract."""
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.delenv('SANDBOX_CI_SERIALIZED', raising=False)
    with pytest.raises(ValueError, match='serialization'):
        sandbox_e2e.namespace()


def test_timeout_terminates_group_then_runs_bounded_cleanup(monkeypatch):
    """A hanging quota loop cannot prevent the external total deadline."""
    child = create_autospec(ProcessSpec, instance=True, spec_set=True)
    child.pid = 456
    child.wait.side_effect = [subprocess.TimeoutExpired('pytest', 1), 143]
    start = create_autospec(subprocess.Popen, spec_set=True, return_value=child)
    cleanup = create_autospec(subprocess.run, spec_set=True)
    cleanup.return_value.returncode = 0
    kill = create_autospec(sandbox_e2e.os.killpg, spec_set=True)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', start)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'run', cleanup)
    monkeypatch.setattr(sandbox_e2e.os, 'killpg', kill)
    assert sandbox_e2e.run_bounded(['python', 'pytest'], 1, 'local', 'a' * 32) == 124
    assert start.call_args.kwargs['start_new_session'] is True
    kill.assert_called_once_with(456, sandbox_e2e.signal.SIGTERM)
    assert cleanup.call_args.kwargs['timeout'] == 60
    assert cleanup.call_args.args[0][-2:] == ['--run-id', 'a' * 32]


def test_success_preserves_exit_without_cleanup_subprocess(monkeypatch):
    """Normal teardown belongs to pytest; its failure code must survive."""
    child = create_autospec(subprocess.Popen, instance=True, spec_set=True)
    child.wait.return_value = 3
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', lambda *_, **__: child)
    cleanup = create_autospec(subprocess.run, spec_set=True)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'run', cleanup)
    assert sandbox_e2e.run_bounded(['python', 'pytest'], 900, 'local', 'a' * 32) == 3
    cleanup.assert_not_called()


@pytest.mark.parametrize('failure', ['kill', 'gone', 'cleanup', 'timeout', 'interrupt'])
def test_interruption_cleanup_failures_remain_failed(monkeypatch, capsys, failure):
    """A hung child or failed timeout cleanup can never produce a green run."""
    child = create_autospec(ProcessSpec, instance=True, spec_set=True)
    child.pid = 456
    waits = [KeyboardInterrupt() if failure == 'interrupt'
             else subprocess.TimeoutExpired('pytest', 1)]
    waits.extend([subprocess.TimeoutExpired('pytest', 15), 137] if failure == 'kill' else [143])
    child.wait.side_effect = waits
    kill = create_autospec(os.killpg, spec_set=True)
    if failure == 'gone':
        kill.side_effect = ProcessLookupError()
    cleanup = create_autospec(subprocess.run, spec_set=True)
    cleanup.return_value.returncode = int(failure == 'cleanup')
    if failure == 'timeout':
        cleanup.side_effect = subprocess.TimeoutExpired('cleanup', 60)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'run', cleanup)
    monkeypatch.setattr(sandbox_e2e.os, 'killpg', kill)
    assert sandbox_e2e.run_bounded(['python', 'pytest'], 1, 'local', 'a' * 32) == 124
    if failure == 'kill':
        assert kill.call_args_list[-1].args == (456, signal.SIGKILL)
    if failure in ('timeout', 'cleanup'):
        assert 'cleanup failed' in capsys.readouterr().out


def test_signal_during_wait_runs_cleanup(monkeypatch):
    """The supervisor forwards interruption through the bounded cleanup path."""
    child = create_autospec(ProcessSpec, instance=True, spec_set=True)
    child.pid = 456
    handlers = {}

    def install(signum, handler):
        handlers[signum] = handler
        return signal.SIG_DFL

    def interrupted_wait(timeout=None):
        if timeout == 900:
            handlers[signal.SIGTERM]()
        return 143
    child.wait.side_effect = interrupted_wait
    monkeypatch.setattr(sandbox_e2e.signal, 'signal', install)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(sandbox_e2e.os, 'killpg', lambda *_: None)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'run',
                        lambda *_, **__: subprocess.CompletedProcess([], 0))
    assert sandbox_e2e.run_bounded(['python', 'pytest'], 900, 'local', 'a' * 32) == 124
    assert handlers[signal.SIGTERM] == signal.SIG_DFL


@pytest.mark.parametrize('selected_timeout', [None, 12])
def test_main_default_command_and_environment(monkeypatch, selected_timeout):
    """Only the live subtree is collected; explicit args and deadline reach the child."""
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('SANDBOX_TOKEN', 'offline-marker')
    monkeypatch.setattr(sandbox_e2e.fcntl, 'flock', lambda *_: None)
    bounded = create_autospec(sandbox_e2e.run_bounded, spec_set=True, return_value=7)
    monkeypatch.setattr(sandbox_e2e, 'run_bounded', bounded)
    ids = create_autospec(lambda: UUID('a' * 32), spec_set=True,
                          return_value=UUID('a' * 32))
    monkeypatch.setenv('E2E_NAMESPACE', 'previous')
    monkeypatch.setenv('E2E_RUN_ID', 'previous')
    arguments = ([] if selected_timeout is None else ['--timeout', str(selected_timeout)])
    assert sandbox_e2e.main([*arguments, '--', '-k', 'INDEX'], run_id_factory=ids) == 7
    ids.assert_called_once_with()
    command, timeout, namespace, run_id = bounded.call_args.args
    assert timeout == (1800 if selected_timeout is None else selected_timeout)
    assert namespace == 'local' and run_id == 'a' * 32
    assert 'e2e/' in command and '--confcutdir=e2e' in command
    assert command[-2:] == ['-k', 'INDEX']
    assert 'offline-marker' not in ' '.join(command)
    assert os.environ['E2E_NAMESPACE'] == 'local'


@pytest.mark.parametrize('failure', ['token', 'lock', 'launch'])
def test_main_failure_before_live_work(monkeypatch, capsys, failure):
    """Missing credentials, overlapping runs and process errors fail without details."""
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('SANDBOX_TOKEN', 'offline-marker')
    if failure == 'token':
        monkeypatch.delenv('SANDBOX_TOKEN')

    def lock(*_):
        if failure == 'lock':
            raise BlockingIOError()

    def launch(*_):
        raise OSError('private')
    monkeypatch.setattr(sandbox_e2e.fcntl, 'flock', lock)
    monkeypatch.setattr(sandbox_e2e, 'run_bounded', launch)
    assert sandbox_e2e.main([]) == 2
    assert 'private' not in capsys.readouterr().err


def test_ci_namespace_attestation(monkeypatch):
    """An attested CI run cannot accidentally clean the local namespace."""
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('SANDBOX_CI_SERIALIZED', '1')
    assert sandbox_e2e.namespace() == 'ci'
