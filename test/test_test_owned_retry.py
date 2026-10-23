"""The launch snapshot and test-owned boolean drive the new retry protocol."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec
from test.test_retry_evidence import FIRST, SECOND, SHA, mark_original_skipped, write_attempt
from test.test_sandbox_dynamic import offline_legacy_suite
from test.test_sandbox_retry import NODE, attempt, store

import pytest

from scripts import sandbox_e2e, sandbox_github, sandbox_retry
from scripts.retry_evidence import Collection, verify_history
from scripts.retry_results import PhaseResult
from scripts.retry_settings import RunSettings
from scripts.sandbox_retry_report import AttemptRecorder
from scripts import sandbox_retry_evidence as legacy


def test_recorder_persists_only_explicit_retry_decisions(tmp_path, monkeypatch):
    """Broker errors and clock thresholds are absent from the consumer contract."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v4')
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.selected = (FIRST,)
    recorder.entered = recorder.cleaned = [FIRST]
    recorder.session_cleanup = True
    for when in ('setup', 'call', 'teardown'):
        recorder.phase(FIRST, when, 'failed' if when == 'call' else 'passed', None,
                       retryable=when == 'call')
    recorder.finish(1)
    data = json.loads((tmp_path / 'result.json').read_text())
    assert data['version'] == 1 and data['sha'] == SHA
    assert data['phases'][1] == asdict(PhaseResult(FIRST, 'call', 'failed', True))
    assert not any('grpc' in str(phase) or 'stale' in str(phase) for phase in data['phases'])


def test_test_policy_can_deny_all_retries_after_auth_failure(tmp_path, monkeypatch):
    """An auth failure invalidates every earlier grant without teaching CI about auth."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v4')
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.phase(FIRST, 'call', 'failed', None, retryable=True)
    recorder.retry_blocked = True
    recorder.finish(1)
    assert json.loads(recorder.path.read_text())['phases'][0]['retryable'] is False


def test_generic_runner_retries_only_permitted_subset(tmp_path, monkeypatch):
    """A permanent failure stays red while an unrelated allowed retry runs."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v4')
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    settings = RunSettings(timeout_seconds=4200, max_attempts=6, retry_delay_seconds=0)
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    (tmp_path / 'collection.json').write_text(json.dumps({
        'version': 1, 'sha': SHA, 'nodeids': [FIRST, SECOND]}))
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        entry = write_attempt(tmp_path, len(calls), {
            FIRST: ('failed', False), SECOND: ('failed', True)
        } if len(calls) == 1 else {SECOND: ('passed', False)})
        return SimpleNamespace(returncode=entry['returncode'])

    runner = create_autospec(sandbox_retry.subprocess.run, spec_set=True, side_effect=run)
    runtime = sandbox_retry.RetryRuntime(runner, lambda: 0, lambda _: None)
    assert sandbox_retry.run_attempts(['e2e/'], tmp_path, 4200, runtime=runtime) == 1
    assert len(calls) == 2
    assert SECOND in calls[1][0] and FIRST not in calls[1][0]
    assert calls[0][1]['env']['E2E_RUN_SETTINGS'] == settings.to_json()
    with pytest.raises(ValueError, match='failed'):
        verify_history(tmp_path, Collection(SHA, (FIRST, SECOND)), settings)


def test_workflow_can_authorize_new_budgets_before_merge():
    """Dispatch configuration is canonicalized once for producer and verifier."""
    settings = sandbox_github.select_settings({
        'inputs': {'run_settings': '{"timeout_seconds":4200,"max_attempts":6}'}
    }, 'workflow_dispatch')
    assert settings.timeout_seconds == 4200 and settings.max_attempts == 6
    assert settings.run_step_minutes == 72
    assert sandbox_github.select_settings({}, 'push').timeout_seconds == 3600


@pytest.mark.parametrize('digest_mode', ['missing', 'wrong', 'original'])
def test_manifest_cannot_be_replaced_along_with_green_subset(tmp_path, digest_mode):
    """The hash fixed before live is independent of the manifest uploaded afterwards."""
    settings = RunSettings()
    manifest = tmp_path / 'collection.json'
    manifest.write_text(json.dumps({'version': 1, 'sha': SHA, 'nodeids': [FIRST, SECOND]}))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    manifest.write_text(json.dumps({'version': 1, 'sha': SHA, 'nodeids': [FIRST]}))
    entry = write_attempt(tmp_path, 1, {FIRST: ('passed', False)})
    (tmp_path / 'retry.json').write_text(json.dumps({'version': 1, 'attempts': [entry]}))
    (tmp_path / 'run-settings.json').write_text(settings.to_json())
    from scripts.retry_evidence import write_junit  # pylint: disable=import-outside-toplevel
    write_junit({FIRST: 'passed'}, tmp_path / 'junit.xml')
    with pytest.raises(sandbox_github.GitHubFailure, match='manifest'):
        sandbox_github.evidence_result(
            tmp_path / 'junit.xml', None, 'retry-v4', collection=manifest, sha=SHA,
            settings=settings, collection_digest={
                'missing': '', 'wrong': '0' * 64, 'original': digest,
            }[digest_mode])


@pytest.mark.parametrize('protocol', ['retry-v2', 'retry-v3'])
def test_legacy_skip_mismatch_cannot_be_repaired_by_retry(tmp_path, protocol):
    """The migration path rejects the same original JUnit skip laundering as v4."""
    reports = [replace(attempt(failed=(NODE,)), sha=SHA), replace(attempt(), sha=SHA)]
    for number, report in enumerate(reports, 1):
        store(tmp_path, number, report)
        if protocol == 'retry-v3':
            (tmp_path / f'attempts/{number:02}/report.json').write_text(
                json.dumps(asdict(report)))
    path = tmp_path / 'attempts/01/junit.xml'
    mark_original_skipped(path)
    legacy.write_final_junit({NODE: 'passed'}, tmp_path / 'junit.xml')
    with pytest.raises(legacy.RetryFailure, match='skipped'):
        legacy.verify_history(tmp_path, expected=legacy.SuiteCollection(SHA, (NODE,)),
                              protocol=protocol)


@pytest.mark.parametrize('protocol', ['retry-v2', 'retry-v3'])
def test_explicit_empty_manifest_digest_does_not_enable_legacy_fallback(
        tmp_path, monkeypatch, protocol):
    """A lost or masked workflow output is failure, not permission to trust the artifact."""
    report = replace(attempt(), sha=SHA)
    store(tmp_path, 1, report)
    if protocol == 'retry-v3':
        (tmp_path / 'attempts/01/report.json').write_text(json.dumps(asdict(report)))
    legacy.write_final_junit({NODE: 'passed'}, tmp_path / 'junit.xml')
    manifest = tmp_path / 'collection.json'
    manifest.write_text(json.dumps({'version': 1, 'sha': SHA, 'nodeids': [NODE]}))
    output = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('EXPECTED_COLLECTION_DIGEST', '')
    assert sandbox_github.main([
        'evidence', '--junit', str(tmp_path / 'junit.xml'), '--evidence-format', protocol,
        '--sha', SHA, '--collection', str(manifest)]) == 1
    assert not output.exists()


def test_new_protocol_is_independent_of_legacy_market_proof():
    """New launches consume a generic boolean contract by default."""
    assert sandbox_github.select_protocol({}, 'push') == 'retry-v4'
    assert sandbox_github.select_protocol({'inputs': {}}, 'workflow_dispatch') == 'retry-v4'


@pytest.mark.parametrize('raw', [
    '{"timeout_seconds":22000}', '{"max_attempts":0}', '{"retry_delay_seconds":true}',
    '{"unknown":1}', '{"max_attempts":2,"max_attempts":3}',
])
def test_invalid_dispatch_budgets_fail_before_live(raw):
    """Unsafe or ambiguous dispatch settings cannot start the credential-bearing job."""
    with pytest.raises((ValueError, sandbox_github.GitHubFailure)):
        sandbox_github.select_settings({'inputs': {'run_settings': raw}}, 'workflow_dispatch')


@pytest.mark.parametrize('fault', ['deadline', 'timeout', 'limit', 'passed', 'settings',
                                   'reuse', 'selector', 'ci-collection'])
def test_generic_runner_deadline_and_error_paths(tmp_path, monkeypatch, fault):
    """Finite budgets and invalid launch state remain hard stop conditions."""
    settings = RunSettings(timeout_seconds=100, max_attempts=2, retry_delay_seconds=0)
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v4')
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    monkeypatch.delenv('E2E_TESTED_SHA', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    if fault == 'reuse':
        (tmp_path / 'attempts').mkdir()
    if fault == 'ci-collection':
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if fault == 'timeout':
            raise subprocess.TimeoutExpired('offline', 100)
        entry = write_attempt(tmp_path, len(calls), {
            FIRST: ('passed', False) if fault == 'passed' else ('failed', True)})
        return SimpleNamespace(returncode=entry['returncode'])

    runner = create_autospec(subprocess.run, spec_set=True, side_effect=run)
    clock = iter([0, 200] if fault == 'deadline' else [0] * 20)
    runtime = sandbox_retry.RetryRuntime(runner, lambda: next(clock), lambda _: None)
    if fault in ('settings', 'reuse', 'selector', 'ci-collection'):
        with pytest.raises(ValueError):
            sandbox_retry.run_attempts([] if fault == 'selector' else ['e2e/'], tmp_path,
                                       90 if fault == 'settings' else 100, runtime=runtime)
    else:
        result = sandbox_retry.run_attempts(['e2e/'], tmp_path, 100, runtime=runtime)
        assert result == (124 if fault in ('deadline', 'timeout') else
                          0 if fault == 'passed' else 1)


@pytest.mark.parametrize('returncode', [124, None])
def test_supervisor_uses_authorized_termination_and_cleanup_budgets(monkeypatch, returncode):
    """Configured emergency budgets reach shutdown and cleanup, not just job metadata."""
    # pylint: disable=protected-access
    settings = RunSettings(timeout_seconds=100, termination_grace_seconds=7,
                           termination_kill_seconds=3, cleanup_timeout_seconds=12)
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v4')
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


@pytest.mark.parametrize('mode', ['retry', 'permanent', 'skip', 'xfail', 'xpass'])
def test_real_pytest_processes_obey_status_protocol_without_live_fixtures(tmp_path, mode):
    """Exercise actual hooks, subprocesses, retry selection and independent verification."""
    offline_legacy_suite(tmp_path / 'e2e', legacy)
    code = f'''import os
from pathlib import Path
import pytest
from grpc import StatusCode
from t_tech.invest import RequestError
MODE = {mode!r}
@pytest.mark.xfail(MODE in ('xfail', 'xpass'), strict=False, reason='offline classification')
def test_gate(proof):
    if MODE == 'skip':
        pytest.skip('not acceptance')
    if MODE in ('permanent', 'xfail'):
        assert False
    if MODE == 'retry' and Path(os.environ['E2E_ARTIFACT_DIR']).name == '01':
        raise RequestError(StatusCode.UNAVAILABLE, 'offline simulated transport', None)
'''
    (tmp_path / 'e2e/test_gate.py').write_text(code, encoding='utf-8')
    nodes = (*legacy.EXPECTED_NODEIDS, 'e2e/test_gate.py::test_gate')
    (tmp_path / 'collection.json').write_text(json.dumps({
        'version': 1, 'sha': SHA, 'nodeids': nodes}))
    settings = RunSettings(timeout_seconds=30, max_attempts=2, retry_delay_seconds=0)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       PYTEST_ADDOPTS='', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       E2E_SUPERVISED='1', E2E_EVIDENCE_PROTOCOL='retry-v4',
                       E2E_RUN_ID='a' * 32, E2E_NAMESPACE='local',
                       E2E_TESTED_SHA=SHA, E2E_ARTIFACT_DIR=str(tmp_path),
                       E2E_RUN_SETTINGS=settings.to_json())
    process = subprocess.run([sys.executable, '-m', 'scripts.sandbox_retry',
                              '--timeout', '30', '--', 'e2e/', '--confcutdir=e2e', '-q'],
                             cwd=tmp_path, env=environment, capture_output=True,
                             text=True, check=False, timeout=40)
    assert process.returncode == (0 if mode == 'retry' else 1), process.stdout + process.stderr
    if mode == 'retry':
        assert verify_history(tmp_path, Collection(SHA, nodes), settings) == len(nodes)
        assert sandbox_github.evidence_result(
            tmp_path / 'junit.xml', None, 'retry-v4',
            collection=tmp_path / 'collection.json', sha=SHA,
            settings=settings, collection_digest=hashlib.sha256(
                (tmp_path / 'collection.json').read_bytes()).hexdigest()).passed == len(nodes)
    else:
        with pytest.raises(ValueError):
            verify_history(tmp_path, Collection(SHA, nodes), settings)
