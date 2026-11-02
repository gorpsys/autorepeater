"""The single retry path discovers the suite and consumes test-owned decisions."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from scripts import retry_evidence, sandbox_e2e, sandbox_retry
from scripts.retry_results import PhaseResult
from scripts.retry_settings import RunSettings
from scripts.sandbox_retry_report import AttemptRecorder


SHA = 'a' * 40
FIRST = 'e2e/test_any.py::test_first'
SECOND = 'e2e/test_any.py::test_second'


def write_attempt(root, number, outcomes):
    """Construct original result artifacts without running live fixtures."""
    directory = root / 'attempts' / f'{number:02}'
    recorder = AttemptRecorder(directory / 'result.json')
    recorder.selected = tuple(outcomes)
    recorder.entered = recorder.cleaned = list(outcomes)
    recorder.session_cleanup = True
    for node, (outcome, allowed) in outcomes.items():
        for when in ('setup', 'call', 'teardown'):
            recorder.phase(node, when, outcome if when == 'call' else 'passed', None,
                           retryable=allowed if when == 'call' else False)
    returncode = int(any(outcome != 'passed' for outcome, _ in outcomes.values()))
    recorder.finish(returncode)
    retry_evidence.write_junit({node: outcome for node, (outcome, _) in outcomes.items()},
                               directory / 'junit.xml')
    return returncode


def test_recorder_persists_only_explicit_retry_decisions(tmp_path, monkeypatch):
    """No protocol environment selector is needed to record the sole result format."""
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.selected = (FIRST,)
    recorder.entered = recorder.cleaned = [FIRST]
    recorder.session_cleanup = True
    for when in ('setup', 'call', 'teardown'):
        recorder.phase(FIRST, when, 'failed' if when == 'call' else 'passed', None,
                       retryable=when == 'call')
    recorder.finish(1)
    data = json.loads(recorder.path.read_text())
    assert data['version'] == 1 and data['sha'] == SHA
    assert data['phases'][1] == asdict(PhaseResult(FIRST, 'call', 'failed', True))
    assert not any('grpc' in str(phase) or 'stale' in str(phase) for phase in data['phases'])


def test_test_policy_can_deny_all_retries_after_auth_failure(tmp_path):
    """A later auth failure withdraws earlier grants without teaching CI about auth."""
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.phase(FIRST, 'call', 'failed', None, retryable=True)
    recorder.retry_blocked = True
    recorder.finish(1)
    assert json.loads(recorder.path.read_text())['phases'][0]['retryable'] is False


def test_runner_retries_only_permitted_subset(tmp_path, monkeypatch):
    """A permanent failure stays red while an unrelated eligible failure retries."""
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    settings = RunSettings(timeout_seconds=4200, max_attempts=6, retry_delay_seconds=0)
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        status = write_attempt(tmp_path, len(calls), {
            FIRST: ('failed', False), SECOND: ('failed', True)
        } if len(calls) == 1 else {SECOND: ('passed', False)})
        return SimpleNamespace(returncode=status)

    runner = create_autospec(subprocess.run, spec_set=True, side_effect=run)
    runtime = sandbox_retry.RetryRuntime(runner, lambda: 0, lambda _: None)
    assert sandbox_retry.run_attempts(['e2e/'], tmp_path, 4200, runtime=runtime) == 1
    assert len(calls) == 2
    assert SECOND in calls[1][0] and FIRST not in calls[1][0]
    assert calls[0][1]['env']['E2E_RUN_SETTINGS'] == settings.to_json()
    with pytest.raises(ValueError, match='failed'):
        retry_evidence.verify_history(tmp_path, settings, expected_sha=SHA)


@pytest.mark.parametrize('fault', ['deadline', 'timeout', 'limit', 'passed', 'settings',
                                   'reuse', 'selector', 'ci-sha', 'empty', 'delay'])
def test_runner_deadline_and_error_paths(tmp_path, monkeypatch, fault):
    """Finite budgets, a nonempty selection and the fixed CI SHA remain required."""
    settings = RunSettings(timeout_seconds=100, max_attempts=2,
                           retry_delay_seconds=100 if fault == 'delay' else 0)
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    monkeypatch.delenv('E2E_TESTED_SHA', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    if fault == 'reuse':
        (tmp_path / 'attempts').mkdir()
    if fault == 'ci-sha':
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if fault == 'timeout':
            raise subprocess.TimeoutExpired('offline', 100)
        status = write_attempt(tmp_path, len(calls), {} if fault == 'empty' else {
            FIRST: ('passed', False) if fault == 'passed' else ('failed', True)})
        return SimpleNamespace(returncode=status)

    runner = create_autospec(subprocess.run, spec_set=True, side_effect=run)
    clock = iter([0, 200] if fault == 'deadline' else [0] * 20)
    runtime = sandbox_retry.RetryRuntime(runner, lambda: next(clock), lambda _: None)
    if fault in ('settings', 'reuse', 'selector', 'ci-sha', 'empty'):
        with pytest.raises(ValueError):
            sandbox_retry.run_attempts([] if fault == 'selector' else ['e2e/'], tmp_path,
                                       90 if fault == 'settings' else 100, runtime=runtime)
    else:
        result = sandbox_retry.run_attempts(['e2e/'], tmp_path, 100, runtime=runtime)
        assert result == (124 if fault in ('deadline', 'timeout') else
                          0 if fault == 'passed' else 1)


def test_runner_keeps_requested_final_xml_and_configured_delays(tmp_path, monkeypatch):
    """A local output contains all final cases and retries keep one shared deadline."""
    settings = RunSettings(timeout_seconds=100, retry_delay_seconds=3)
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.delenv('E2E_TESTED_SHA', raising=False)
    calls, sleeps = [], []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        status = write_attempt(tmp_path, len(calls), {
            FIRST: ('failed', True) if len(calls) == 1 else ('passed', False)})
        return SimpleNamespace(returncode=status)

    clock = iter([0, 1, 2, 5])
    runner = create_autospec(subprocess.run, spec_set=True, side_effect=run)
    final = tmp_path / 'local.xml'
    assert sandbox_retry.run_attempts(
        ['e2e/', '--junitxml', str(final)], tmp_path, 100,
        runtime=sandbox_retry.RetryRuntime(runner, lambda: next(clock), sleeps.append)) == 0
    assert calls[0][1]['timeout'] == 99 and calls[1][1]['timeout'] == 95
    assert sleeps == [3]
    assert retry_evidence.xml_states(final, (FIRST,)) == {FIRST: 'passed'}


@pytest.mark.parametrize('arguments,valid', [
    (['e2e/', '--junitxml', 'final.xml'], True),
    (['e2e/', '--junit-xml=final.xml'], True),
    (['e2e/', '--junitxml'], False), (['--junitxml=a.txt'], False),
    (['--junitxml=a.xml', '--junitxml=b.xml'], False),
])
def test_junit_path_selection_is_unambiguous(tmp_path, arguments, valid):
    """Per-attempt original outputs cannot be replaced by conflicting options."""
    if valid:
        remaining, path = sandbox_retry.junit_arguments(arguments, tmp_path)
        assert remaining == ['e2e/'] and path == Path('final.xml')
    else:
        with pytest.raises(ValueError):
            sandbox_retry.junit_arguments(arguments, tmp_path)


@pytest.mark.parametrize('returncode', [124, None])
def test_supervisor_uses_authorized_termination_and_cleanup_budgets(monkeypatch, returncode):
    """Configured emergency budgets reach shutdown and cleanup without a protocol switch."""
    # pylint: disable=protected-access
    settings = RunSettings(timeout_seconds=100, termination_grace_seconds=7,
                           termination_kill_seconds=3, cleanup_timeout_seconds=12)
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    child = create_autospec(subprocess.Popen, instance=True, spec_set=True)
    child.wait.side_effect = (subprocess.TimeoutExpired('offline', 100)
                              if returncode is None else None)
    child.wait.return_value = returncode
    start = create_autospec(subprocess.Popen, spec_set=True, return_value=child)
    stop = create_autospec(sandbox_e2e._terminate_process_group, spec_set=True)
    cleanup = create_autospec(sandbox_e2e._cleanup_run, spec_set=True)
    monkeypatch.setattr(sandbox_e2e.subprocess, 'Popen', start)
    monkeypatch.setattr(sandbox_e2e, '_terminate_process_group', stop)
    monkeypatch.setattr(sandbox_e2e, '_cleanup_run', cleanup)
    assert sandbox_e2e.run_bounded(['offline'], 100, 'local', 'a' * 32) == 124
    stop.assert_called_once_with(child, settings=settings)
    cleanup.assert_called_once_with('local', 'a' * 32, timeout=12)


def offline_suite(directory):
    """Use real pytest hooks with offline lifecycle proof instead of live fixtures."""
    directory.mkdir()
    source = Path(__file__).resolve().parents[1] / 'e2e' / 'conftest.py'
    (directory / 'conftest.py').write_text(source.read_text() + '''
@pytest.fixture
def proof(request):
    recorder = attempt_recorder(request.session)
    recorder.entered.append(request.node.nodeid)
    try:
        yield
    finally:
        recorder.cleaned.append(request.node.nodeid)
        recorder.session_cleanup = True
''', encoding='utf-8')


@pytest.mark.parametrize('mode', ['retry', 'permanent', 'skip', 'xfail', 'xpass', 'empty'])
def test_real_pytest_processes_discover_directory_and_obey_flags(tmp_path, mode):
    """New nested tests join the run without a workflow inventory or collection manifest."""
    offline_suite(tmp_path / 'e2e')
    if mode != 'empty':
        (tmp_path / 'e2e/test_first.py').write_text('def test_first(proof):\n    pass\n')
        nested = tmp_path / 'e2e/nested'
        nested.mkdir()
        code = f'''import os
from pathlib import Path
import pytest
from grpc import StatusCode
from t_tech.invest import RequestError
MODE = {mode!r}
class TestExtra:
    @pytest.mark.parametrize('example', ['one', 'two'])
    @pytest.mark.xfail(MODE in ('xfail', 'xpass'), strict=False, reason='offline test')
    def test_gate(self, proof, example):
        if MODE == 'skip':
            pytest.skip('not acceptance')
        if MODE in ('permanent', 'xfail'):
            assert False
        if MODE == 'retry' and Path(os.environ['E2E_ARTIFACT_DIR']).name == '01':
            raise RequestError(StatusCode.UNAVAILABLE, 'offline transport', None)
'''
        (nested / 'test_added.py').write_text(code, encoding='utf-8')
    settings = RunSettings(timeout_seconds=30, max_attempts=2, retry_delay_seconds=0)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       PYTEST_ADDOPTS='', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       E2E_SUPERVISED='1', E2E_RUN_ID='a' * 32, E2E_NAMESPACE='local',
                       E2E_TESTED_SHA=SHA, E2E_ARTIFACT_DIR=str(tmp_path),
                       E2E_RUN_SETTINGS=settings.to_json())
    process = subprocess.run([sys.executable, '-m', 'scripts.sandbox_retry',
                              '--timeout', '30', '--', 'e2e/', '--confcutdir=e2e', '-q'],
                             cwd=tmp_path, env=environment, capture_output=True,
                             text=True, check=False, timeout=40)
    assert process.returncode == (0 if mode == 'retry' else 1), process.stdout + process.stderr
    first = retry_evidence.read_result(tmp_path / 'attempts/01/result.json')
    assert len(first.selected) == (0 if mode == 'empty' else 3)
    if mode in ('skip', 'xfail', 'xpass'):
        assert any(phase.outcome == 'skipped' for phase in first.phases)
    if mode == 'permanent':
        failed = [phase for phase in first.phases if phase.outcome == 'failed']
        assert len(failed) == 2 and all(not phase.retryable for phase in failed)
        assert not (tmp_path / 'attempts/02').exists()
    if mode == 'retry':
        assert retry_evidence.verify_history(tmp_path, settings, expected_sha=SHA) == 3
        second = retry_evidence.read_result(tmp_path / 'attempts/02/result.json')
        assert len(first.selected) == 3 and len(second.selected) == 2
        assert all('nested/test_added.py::TestExtra::' in node for node in second.selected)
        assert not (tmp_path / 'collection.json').exists()


@pytest.mark.parametrize('fault', ['supervision', 'timeout', 'run-id', 'evidence'])
def test_retry_cli_fails_closed(monkeypatch, capsys, fault):
    """No direct unbounded invocation or unsafe evidence can start another attempt."""
    monkeypatch.setenv('E2E_SUPERVISED', '1')
    monkeypatch.setenv('E2E_RUN_ID', 'a' * 32)
    monkeypatch.delenv('E2E_ARTIFACT_DIR', raising=False)
    if fault == 'supervision':
        monkeypatch.delenv('E2E_SUPERVISED')
    if fault == 'run-id':
        monkeypatch.delenv('E2E_RUN_ID')
    run = create_autospec(sandbox_retry.run_attempts, spec_set=True,
                          side_effect=ValueError('private evidence'))
    monkeypatch.setattr(sandbox_retry, 'run_attempts', run)
    assert sandbox_retry.main(['--timeout', '0' if fault == 'timeout' else '10',
                               '--', 'e2e/']) == 1
    assert 'private' not in capsys.readouterr().err


@pytest.mark.parametrize('separator', [True, False])
def test_retry_cli_preserves_supervisor_arguments(monkeypatch, tmp_path, separator):
    """The fixed run directory and total budget reach the only controller path."""
    monkeypatch.setenv('E2E_SUPERVISED', '1')
    monkeypatch.setenv('E2E_RUN_ID', 'a' * 32)
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    run = create_autospec(sandbox_retry.run_attempts, spec_set=True, return_value=0)
    monkeypatch.setattr(sandbox_retry, 'run_attempts', run)
    assert sandbox_retry.main(['--timeout', '10', *(['--'] if separator else []), 'e2e/']) == 0
    run.assert_called_once_with(['e2e/'], tmp_path, 10)
