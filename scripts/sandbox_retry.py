"""Bounded child controller: fresh pytest processes retry only proven transient cases."""
import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts.sandbox_retry_evidence import (
    MAX_ATTEMPTS, RetryFailure, inspect_history, read_report, retry_nodes, write_final_junit,
)


def junit_arguments(arguments: list[str], root: Path) -> tuple[list[str], Path]:
    """Own per-attempt JUnit paths; the requested path receives only merged final cases."""
    remaining = []
    paths = []
    iterator = iter(arguments)
    for argument in iterator:
        if argument in ('--junitxml', '--junit-xml'):
            value = next(iterator, '')
            if not value:
                raise RetryFailure('JUnit output path required')
            paths.append(Path(value))
        elif argument.startswith(('--junitxml=', '--junit-xml=')):
            paths.append(Path(argument.split('=', 1)[1]))
        else:
            remaining.append(argument)
    if len(paths) > 1 or (paths and paths[0].suffix != '.xml'):
        raise RetryFailure('one XML output path required')
    return remaining, paths[0] if paths else root / 'junit.xml'


def attempt_arguments(
        arguments: list[str], nodes: tuple[str, ...] | None, directory: Path) -> list[str]:
    """Replace the full-suite selector, preserving local -k and other pytest options."""
    selected: list[str] = []
    for argument in arguments:
        selected.extend(nodes if argument == 'e2e/' and nodes is not None else (argument,))
    return [sys.executable, '-m', 'pytest', *selected, f'--junitxml={directory / "junit.xml"}']


@dataclass(frozen=True)
class RetryRuntime:
    """Inject process/time operations without changing the shared deadline policy."""
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run_attempts(arguments: list[str], root: Path, timeout: int, *,
                 runtime: RetryRuntime | None = None) -> int:
    """One fixed run ID/process group and one deadline include all attempts and backoff."""
    runtime = RetryRuntime() if runtime is None else runtime
    deadline = runtime.clock() + timeout
    arguments, final_path = junit_arguments(arguments, root)
    if arguments.count('e2e/') != 1 or (root / 'attempts').exists():
        raise RetryFailure('fresh retry directory and one suite selector required')
    root.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, int]] = []
    nodes = None
    for number in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - runtime.clock()
        if remaining <= 0:
            return 124
        directory = root / 'attempts' / f'{number:02}'
        directory.mkdir(parents=True)
        environment = dict(os.environ, E2E_ATTEMPT_REPORT=str(directory / 'report.json'),
                           E2E_ARTIFACT_DIR=str(directory))
        print(f'Sandbox attempt {number}/{MAX_ATTEMPTS}; '
              f'shared time remaining {int(remaining)}s', flush=True)
        try:
            result = runtime.run(attempt_arguments(arguments, nodes, directory), env=environment,
                                 timeout=remaining, check=False)
        except subprocess.TimeoutExpired:
            return 124
        history.append({'number': number, 'returncode': result.returncode})
        (root / 'retry.json').write_text(
            json.dumps({'version': 1, 'attempts': history}), encoding='utf-8')
        states = inspect_history(root, require_full=os.environ.get('GITHUB_ACTIONS') == 'true')
        write_final_junit(states, root / 'junit.xml')
        if final_path != root / 'junit.xml':
            write_final_junit(states, final_path)
        if all(state == 'passed' for state in states.values()):
            return 0
        nodes = retry_nodes(read_report(directory / 'report.json'))
        if not nodes or deadline - runtime.clock() <= 10:
            return 1
        if number < MAX_ATTEMPTS:
            runtime.sleep(10)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Only sandbox_e2e may establish serialization, credential and group ownership."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, required=True)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if os.environ.get('E2E_SUPERVISED') != '1' or not 1 <= args.timeout <= 3600:
            raise RetryFailure('sandbox retry requires the bounded supervisor')
        root = Path(os.environ.get(
            'E2E_ARTIFACT_DIR', '/tmp/autorepeater-e2e-' + os.environ['E2E_RUN_ID']))
        arguments = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
        return run_attempts(arguments, root, args.timeout)
    except (RetryFailure, OSError, KeyError):
        print('Sandbox retry evidence invalid or cleanup incomplete; stopping', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
