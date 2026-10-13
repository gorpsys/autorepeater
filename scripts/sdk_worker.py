"""Generic stdlib supervision of SDK workers; no source-policy imports."""
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Any


MAX_JSON_BYTES = 3 * 1024 * 1024


class WorkerError(ValueError):
    """Fixed safe worker/protocol failure."""


class WorkerTimeout(TimeoutError):
    """A confirmed terminated worker exceeded its allocated time."""
    def __init__(self, phase: str | None = None, index_sha256: str | None = None) -> None:
        super().__init__('SDK worker timeout')
        self.phase = phase
        self.index_sha256 = index_sha256


def _require(condition: bool) -> None:
    if not condition:
        raise WorkerError('invalid SDK worker result')


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def worker_command(action: str, project_root: Path, argument: str) -> list[str]:
    """Use this feature tree's stdlib helper, independent of cwd/PYTHONPATH."""
    root = str(Path(__file__).resolve().parents[1])
    bootstrap = ('import sys,resource; '
                 f'resource.setrlimit(resource.RLIMIT_FSIZE, '
                 f'({MAX_JSON_BYTES}, {MAX_JSON_BYTES})); '
                 f'sys.path.insert(0, {root!r}); '
                 'from scripts.sdk_wheel import worker_main; worker_main()')
    return [sys.executable, '-I', '-S', '-c', bootstrap, action,
            str(project_root.absolute()), argument]


def _read_result(path: Path, limit: int = MAX_JSON_BYTES) -> object:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit)
        data = stream.read(limit + 1)
        _require(len(data) == info.st_size and len(data) <= limit)
    return json.loads(data, object_pairs_hook=_pairs)


def publish_phase(phase: str, index_sha256: str | None) -> None:
    """Atomically publish only fixed stage facts inside the owned worker directory."""
    with Path('phase-next.json').open('x', encoding='ascii') as stream:
        json.dump({'phase': phase, 'index_sha256': index_sha256}, stream)
    os.replace('phase-next.json', 'phase.json')


def _timeout(path: Path) -> WorkerTimeout:
    try:
        item = _read_result(path, 256)
    except FileNotFoundError:
        return WorkerTimeout()
    except (OSError, ValueError):
        raise WorkerError('invalid SDK worker phase') from None
    if not isinstance(item, dict) or set(item) != {'phase', 'index_sha256'}:
        raise WorkerError('invalid SDK worker phase')
    phase, digest = item['phase'], item['index_sha256']
    _require(phase in ('reserve_validation', 'index_request', 'index_validation',
                       'wheel_request', 'wheel_validation'))
    _require(digest is None or isinstance(digest, str) and
             re.fullmatch(r'[0-9a-f]{64}', digest) is not None)
    _require(phase != 'wheel_request' or digest is not None)
    _require(phase != 'index_request' or digest is None)
    return WorkerTimeout(phase, digest)


def _stop(process: subprocess.Popen[bytes], group: bool, grace: float) -> None:
    if process.poll() is None:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
    process.wait()
    if group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_worker(action: str, project_root: Path, argument: str, *, timeout: float,
               temp_root: Path | None = None) -> object:
    """Kill/wait on deadline or cancellation; only owned bounded JSON leaves scope."""
    _require(action in ('select', 'reserve', 'request') and 0 < timeout <= 60)
    grace = min(0.2, timeout / 5)
    # Request children inherit the selection group so outer cancellation kills all.
    group = action != 'request'
    with tempfile.TemporaryDirectory(prefix='sdk-worker-', dir=temp_root) as directory:
        path = Path(directory) / 'result.json'
        with path.open('xb') as output, subprocess.Popen(
                worker_command(action, project_root, argument), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, start_new_session=group,
                cwd=directory) as process:
            timed_out = False
            try:
                try:
                    process.wait(timeout=timeout - grace)
                except subprocess.TimeoutExpired:
                    timed_out = True
            finally:
                _stop(process, group, grace)
        if timed_out:
            raise _timeout(Path(directory) / 'phase.json')
        _require(process.returncode == 0)
        try:
            return _read_result(path)
        except (OSError, ValueError):
            raise WorkerError('invalid SDK worker result') from None
