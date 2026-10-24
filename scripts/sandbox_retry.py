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

from scripts import retry_evidence, retry_results
from scripts.retry_settings import RunSettings


def junit_arguments(arguments: list[str], root: Path) -> tuple[list[str], Path]:
    """Own per-attempt JUnit paths; the requested path receives only merged final cases."""
    remaining = []
    paths = []
    iterator = iter(arguments)
    for argument in iterator:
        if argument in ('--junitxml', '--junit-xml'):
            value = next(iterator, '')
            if not value:
                raise ValueError('JUnit output path required')
            paths.append(Path(value))
        elif argument.startswith(('--junitxml=', '--junit-xml=')):
            paths.append(Path(argument.split('=', 1)[1]))
        else:
            remaining.append(argument)
    if len(paths) > 1 or (paths and paths[0].suffix != '.xml'):
        raise ValueError('one XML output path required')
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


def prepare_run(arguments: list[str], root: Path, timeout: int
                ) -> tuple[list[str], Path, RunSettings, str | None]:
    """Validate the single launch snapshot before creating attempt processes."""
    settings = RunSettings.from_json(os.environ.get('E2E_RUN_SETTINGS'))
    if timeout != settings.timeout_seconds:
        raise ValueError('supervisor and retry timeout differ')
    arguments, final_path = junit_arguments(arguments, root)
    if arguments.count('e2e/') != 1 or (root / 'attempts').exists():
        raise ValueError('fresh retry directory and one suite selector required')
    root.mkdir(parents=True, exist_ok=True)
    sha = os.environ.get('E2E_TESTED_SHA')
    if sha is not None:
        retry_evidence.checked_sha(sha)
    if os.environ.get('GITHUB_ACTIONS') == 'true' and sha is None:
        raise ValueError('CI requires the exact tested SHA')
    (root / 'run-settings.json').write_text(settings.to_json(), encoding='utf-8')
    return arguments, final_path, settings, sha


def run_attempts(arguments: list[str], root: Path, timeout: int, *,
                 runtime: RetryRuntime | None = None) -> int:
    """Consume test-owned retry flags within one immutable launch budget."""
    runtime = RetryRuntime() if runtime is None else runtime
    deadline = runtime.clock() + timeout
    arguments, final_path, settings, sha = prepare_run(arguments, root, timeout)
    history: list[dict[str, int]] = []
    nodes = None
    for number in range(1, settings.max_attempts + 1):
        remaining = deadline - runtime.clock()
        if remaining <= 0:
            return 124
        directory = root / 'attempts' / f'{number:02}'
        directory.mkdir(parents=True)
        print(f'Sandbox attempt {number}/{settings.max_attempts}; '
              f'shared time remaining {int(remaining)}s', flush=True)
        try:
            history.append({'number': number, 'returncode': runtime.run(
                attempt_arguments(arguments, nodes, directory), env=dict(
                    os.environ, E2E_ATTEMPT_REPORT=str(directory / 'result.json'),
                    E2E_ARTIFACT_DIR=str(directory), E2E_RUN_SETTINGS=settings.to_json()),
                timeout=remaining, check=False).returncode})
        except subprocess.TimeoutExpired:
            return 124
        (root / 'retry.json').write_text(
            json.dumps({'version': 1, 'attempts': history}), encoding='utf-8')
        states = retry_evidence.inspect_history(root, settings, expected_sha=sha)
        retry_evidence.write_junit(states, root / 'junit.xml')
        if final_path != root / 'junit.xml':
            retry_evidence.write_junit(states, final_path)
        if all(state == 'passed' for state in states.values()):
            return 0
        report = retry_evidence.read_result(directory / 'result.json')
        nodes = retry_results.retry_nodes(report.phases, report.selected)
        if not nodes or deadline - runtime.clock() <= settings.retry_delay_seconds:
            return 1
        if number < settings.max_attempts:
            runtime.sleep(settings.retry_delay_seconds)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Only sandbox_e2e may establish serialization, credential and group ownership."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, required=True)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if os.environ.get('E2E_SUPERVISED') != '1' or args.timeout < 1:
            raise ValueError('sandbox retry requires the bounded supervisor')
        root = Path(os.environ.get(
            'E2E_ARTIFACT_DIR', '/tmp/autorepeater-e2e-' + os.environ['E2E_RUN_ID']))
        arguments = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
        return run_attempts(arguments, root, args.timeout)
    except (ValueError, OSError, KeyError):
        print('Sandbox retry evidence invalid or cleanup incomplete; stopping', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
