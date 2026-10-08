"""Offline subprocess tests prove cleanup never races a surviving pytest child."""
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import create_autospec

import pytest

from scripts import sandbox_e2e


def test_termination_reaps_group_even_when_leader_exits(tmp_path, monkeypatch):
    """A controller can die while its SIGTERM-ignoring child remains runnable."""
    ready = tmp_path / 'ready'
    code = (
        'import os, signal, time; from pathlib import Path; '
        'pid=os.fork(); '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN if pid == 0 else signal.SIG_DFL); '
        f'Path({str(ready)!r}).write_text(str(os.getpid())) if pid == 0 else None; '
        'time.sleep(60)'
    )
    with subprocess.Popen([sys.executable, '-c', code], start_new_session=True) as child:
        try:
            import time  # pylint: disable=import-outside-toplevel
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists()
            monkeypatch.setattr(sandbox_e2e, 'GROUP_GRACE_SECONDS', .1, raising=False)
            sandbox_e2e._terminate_process_group(child)  # pylint: disable=protected-access
            descendant = Path('/proc') / ready.read_text() / 'stat'
            if descendant.exists():
                state = descendant.read_text().rsplit(')', 1)[1].split()[0]
                assert state in ('Z', 'X'), 'cleanup must not race a live descendant'
        finally:
            try:
                sandbox_e2e.os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('state,group,active', [
    ('S', 456, True), ('Z', 456, False), ('X', 456, False), ('S', 789, False),
])
def test_process_group_probe_uses_own_group_not_unicode_name(
        tmp_path, monkeypatch, state, group, active):
    """Non-ASCII process names and already-dead group members cannot distort confirmation."""
    directory = tmp_path / '123'
    directory.mkdir()
    (directory / 'stat').write_bytes(
        b'123 (\xff name)) ' + f'{state} 1 {group} 0'.encode('ascii'))
    (tmp_path / '456').mkdir()  # A process disappearing during the scan is harmless.
    (tmp_path / 'not-process').mkdir()
    monkeypatch.setattr(sandbox_e2e, 'Path', lambda _: tmp_path)
    assert sandbox_e2e._group_active(456) is active  # pylint: disable=protected-access


def test_unconfirmed_group_never_runs_cleanup(monkeypatch):
    """Even SIGKILL is not a substitute for observing that every live member stopped."""
    class ProcessSpec(subprocess.Popen):
        """Expose the dynamic pid on a process autospec without launching anything."""
        pid = 456

    child = create_autospec(ProcessSpec, instance=True, spec_set=True)
    child.wait.side_effect = [subprocess.TimeoutExpired('controller', 1), 143]
    cleanup = create_autospec(sandbox_e2e._cleanup_run,  # pylint: disable=protected-access
                              spec_set=True)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(sandbox_e2e, '_wait_group', lambda *_: False)
    monkeypatch.setattr(sandbox_e2e.os, 'killpg', lambda *_: None)
    monkeypatch.setattr(sandbox_e2e, '_cleanup_run', cleanup)
    with pytest.raises(OSError, match='cleanup unsafe'):
        sandbox_e2e.run_bounded(['controller'], 1, 'local', 'a' * 32)
    cleanup.assert_not_called()


def test_controller_hard_timeout_cleanup_waits_for_group(monkeypatch):
    """A child exit 124 still requires terminating remaining descendants before API cleanup."""
    child = create_autospec(subprocess.Popen, instance=True, spec_set=True)
    child.wait.return_value = 124
    events = []
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', lambda *_, **__: child)
    monkeypatch.setattr(sandbox_e2e, '_terminate_process_group', lambda _: events.append('stopped'))
    monkeypatch.setattr(sandbox_e2e, '_cleanup_run', lambda *_: events.append('cleanup'))
    assert sandbox_e2e.run_bounded(['controller'], 1, 'local', 'a' * 32) == 124
    assert events == ['stopped', 'cleanup']
