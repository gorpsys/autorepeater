"""Serialize sandbox processes and bound even the application's quota retry loop."""
import argparse
from collections.abc import Callable
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import UUID, uuid4

GROUP_GRACE_SECONDS = 15
GROUP_KILL_SECONDS = 5


def namespace() -> str:
    """The CI parent owns GitHub concurrency, including cleanup and diagnostics."""
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        if os.environ.get('SANDBOX_CI_SERIALIZED') != '1':
            raise ValueError('CI parent serialization required: SANDBOX_CI_SERIALIZED=1')
        return 'ci'
    return 'local'


def timeout_seconds(value: str) -> int:
    """Total live execution starts at 1800s; no unlimited override is supported."""
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be an integer in 1..3600') from None
    if not 1 <= seconds <= 3600:
        raise argparse.ArgumentTypeError('timeout must be in 1..3600')
    return seconds


def _group_active(group_id: int) -> bool:
    """Linux /proc confirms runnable members, not merely the controller's exit."""
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            fields = (process / 'stat').read_bytes().rsplit(b')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == group_id and fields[0] not in (b'Z', b'X'):
            return True
    return False


def _wait_group(group_id: int, seconds: float) -> bool:
    """Zombies cannot trade; an unreadable or still-live group is not confirmed stopped."""
    deadline = time.monotonic() + seconds
    while _group_active(group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)
    return True


def _terminate_process_group(child: subprocess.Popen) -> None:
    """Confirm all owned descendants stop before cleanup, even if the leader already died."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=GROUP_KILL_SECONDS)
    except ProcessLookupError:
        child.wait(timeout=GROUP_KILL_SECONDS)
    if not _wait_group(child.pid, GROUP_GRACE_SECONDS):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_group(child.pid, GROUP_KILL_SECONDS):
            raise OSError('sandbox process group did not stop; cleanup unsafe')


def _cleanup_run(selected_namespace: str, run_id: str) -> None:
    """Attempt bounded cleanup of only this interrupted run's owned accounts."""
    try:
        result = subprocess.run([
            sys.executable, '-m', 'scripts.sandbox_lifecycle', 'cleanup',
            '--namespace', selected_namespace, '--run-id', run_id],
            check=False, timeout=60)
        if result.returncode:
            print('Timeout cleanup failed; owned orphans must be removed next run', flush=True)
    except (OSError, subprocess.TimeoutExpired):
        print('Timeout cleanup failed; owned orphans must be removed next run', flush=True)


def run_bounded(command: list[str], timeout: int, selected_namespace: str, run_id: str) -> int:
    """Terminate the entire process group, then attempt finite current-run cleanup."""
    child = subprocess.Popen(command, start_new_session=True)  # pylint: disable=consider-using-with
    previous = {}

    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        result = child.wait(timeout=timeout)
        if result == 124:
            _terminate_process_group(child)
            _cleanup_run(selected_namespace, run_id)
            return 124
        return result
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        print('Sandbox run interrupted or timed out; attempting cleanup', flush=True)
        _terminate_process_group(child)
        _cleanup_run(selected_namespace, run_id)
        return 124
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None, *,
         run_id_factory: Callable[[], UUID] | None = None) -> int:
    """Only explicit e2e collection runs live; the offline pytest default is test/."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=timeout_seconds, default=1800)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        selected_namespace = namespace()
        if not os.environ.get('SANDBOX_TOKEN'):
            raise ValueError('SANDBOX_TOKEN is required; launch through scripts.sandbox_token')
        with Path('/tmp/autorepeater-sandbox-e2e.lock').open('a', encoding='ascii') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('overlapping sandbox run forbidden') from None
            create_run_id = uuid4 if run_id_factory is None else run_id_factory
            run_id = create_run_id().hex
            os.environ['E2E_NAMESPACE'] = selected_namespace
            os.environ['E2E_RUN_ID'] = run_id
            os.environ['E2E_SUPERVISED'] = '1'
            additional = args.pytest_args
            if additional[:1] == ['--']:
                additional = additional[1:]
            return run_bounded([sys.executable, '-m', 'scripts.sandbox_retry',
                                '--timeout', str(args.timeout), '--', 'e2e/',
                                '--confcutdir=e2e', '-q', '--tb=no', '--show-capture=no',
                                '-p', 'no:cacheprovider', *additional],
                               args.timeout, selected_namespace, run_id)
    except (ValueError, OSError) as error:
        reason = str(error) if isinstance(error, ValueError) else 'cannot launch sandbox process'
        print(reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
