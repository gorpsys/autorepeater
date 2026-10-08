"""Independent offline evidence proves retries cannot hide permanent failures."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts import sandbox_retry_evidence as evidence

NODE = 'e2e/test_sandbox.py::test_full_balanced_imoex_oblg_gold'
OTHER = 'e2e/test_sandbox.py::test_child_own_drift_below_component_limit'


def attempt(nodes=(NODE,), failed=(), *, status='DEADLINE_EXCEEDED'):
    """Build original phase facts, independently of controller execution."""
    phases = []
    for node in nodes:
        phases.append(evidence.PhaseFact(node, 'setup', 'passed', (), None))
        phases.append(evidence.PhaseFact(
            node, 'call', 'failed' if node in failed else 'passed',
            ('SandboxFailure', 'RequestError') if node in failed else (),
            status if node in failed else None))
        phases.append(evidence.PhaseFact(node, 'teardown', 'passed', (), None))
    return evidence.AttemptReport(tuple(nodes), tuple(phases), tuple(nodes), tuple(nodes),
                                  True, 0, int(bool(failed)))


def store(root: Path, number: int, report, *, returncode=None):
    """Original artifacts and actual exit code are retained for every round."""
    directory = root / 'attempts' / f'{number:02}'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'report.json').write_text(json.dumps(asdict(report)), encoding='utf-8')
    suite = ET.Element('testsuite', tests=str(len(report.selected)), errors='0',
                       failures=str(len(evidence.failed_nodes(report))), skipped='0')
    for node in report.selected:
        case = ET.SubElement(suite, 'testcase', classname='e2e.test_sandbox',
                             name=node.split('::')[1])
        if node in evidence.failed_nodes(report):
            ET.SubElement(case, 'failure', message='fixed safe failure')
    ET.ElementTree(suite).write(directory / 'junit.xml')
    (directory / 'cleanup.log').write_text(
        'Sandbox scenario cleanup complete\n' * len(report.cleaned) +
        ('Sandbox session cleanup complete\n' if report.session_cleanup else ''), encoding='utf-8')
    manifest = root / 'retry.json'
    history = (json.loads(manifest.read_text()) if manifest.exists()
               else {'version': 1, 'attempts': []})
    history['attempts'].append({'number': number, 'returncode': report.exit_status
                               if returncode is None else returncode})
    manifest.write_text(json.dumps(history), encoding='utf-8')


def test_only_failed_cases_are_retried_and_final_success_is_derived(tmp_path):
    """Passed cases remain untouched while a transient failure gets fresh execution."""
    store(tmp_path, 1, attempt((NODE, OTHER), (NODE,)))
    store(tmp_path, 2, attempt())
    final = evidence.inspect_history(tmp_path)
    assert final == {NODE: 'passed', OTHER: 'passed'}
    evidence.write_final_junit(final, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, require_full=False) == 2


@pytest.mark.parametrize('status', ['UNAVAILABLE', 'DEADLINE_EXCEEDED'])
def test_three_additional_attempts_are_allowed(tmp_path, status):
    """The default limit means one original plus three reruns, not three total."""
    for number in range(1, 4):
        store(tmp_path, number, attempt(failed=(NODE,), status=status))
    store(tmp_path, 4, attempt())
    states = evidence.inspect_history(tmp_path)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, require_full=False) == 1


@pytest.mark.parametrize('fault', [
    'extra-round', 'passed-rerun', 'wrong-selection', 'permanent', 'cleanup',
    'missing-phase', 'exit-mismatch', 'collection', 'skip', 'tampered-junit', 'missing-marker',
])
def test_history_cannot_launder_failure(tmp_path, fault):
    """The verifier derives legality from originals, never just the merged green XML."""
    first = attempt((NODE, OTHER), (NODE,))
    if fault == 'permanent':
        first = attempt((NODE, OTHER), (NODE,), status='INTERNAL')
    if fault == 'cleanup':
        first = replace(first, cleaned=())
    if fault == 'missing-phase':
        first = replace(first, phases=first.phases[1:])
    if fault == 'collection':
        first = replace(first, collection_errors=1)
    if fault == 'skip':
        first = replace(first, phases=(replace(first.phases[0], outcome='skipped'),
                                       *first.phases[1:]))
    store(tmp_path, 1, first, returncode=0 if fault == 'exit-mismatch' else None)
    if fault == 'tampered-junit':
        path = tmp_path / 'attempts/01/junit.xml'
        tree = ET.parse(path)
        case = tree.getroot()[0]
        case.remove(case[0])
        tree.write(path)
    if fault == 'missing-marker':
        (tmp_path / 'attempts/01/cleanup.log').write_text('', encoding='utf-8')
    if fault == 'extra-round':
        for number in range(2, 6):
            store(tmp_path, number, attempt(failed=(NODE,)))
    else:
        second = attempt((OTHER,)) if fault == 'wrong-selection' else attempt(
            (NODE, OTHER) if fault == 'passed-rerun' else (NODE,))
        store(tmp_path, 2, second)
    with pytest.raises(evidence.RetryFailure):
        evidence.inspect_history(tmp_path)


def test_ci_rejects_subset_but_local_k_selection_is_valid(tmp_path):
    """Local selection is explicit; it cannot produce a CI full-suite acceptance."""
    store(tmp_path, 1, attempt())
    evidence.write_final_junit(evidence.inspect_history(tmp_path), tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, require_full=False) == 1
    with pytest.raises(evidence.RetryFailure, match='full'):
        evidence.verify_history(tmp_path, require_full=True)


def test_merged_junit_alone_is_not_trusted(tmp_path):
    """Missing history or an invented final pass cannot give a green verdict."""
    store(tmp_path, 1, attempt(failed=(NODE,)))
    evidence.write_final_junit({NODE: 'passed'}, tmp_path / 'junit.xml')
    with pytest.raises(evidence.RetryFailure):
        evidence.verify_history(tmp_path, require_full=False)


def test_controller_runs_only_failed_nodes_with_one_budget(tmp_path):
    """Process arguments, remaining deadline and fresh artifact paths are explicit."""
    import subprocess  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    calls = []
    sleeps = []

    def run(command, *, env, timeout, check):
        calls.append((command, env, timeout, check))
        number = len(calls)
        report = attempt((NODE, OTHER), (NODE,)) if number == 1 else attempt()
        store(tmp_path, number, report)
        # The controller, not the fake child, owns the manifest.
        (tmp_path / 'retry.json').unlink()
        return subprocess.CompletedProcess(command, report.exit_status)

    clock = iter([0, 1, 2, 13])
    runtime = sandbox_retry.RetryRuntime(run, lambda: next(clock), sleeps.append)
    assert sandbox_retry.run_attempts(
        ['e2e/', '-k', 'test_', '--confcutdir=e2e'], tmp_path, 100, runtime=runtime) == 0
    assert calls[0][0][3:5] == ['e2e/', '-k']
    assert calls[1][0][3] == NODE and 'e2e/' not in calls[1][0]
    assert calls[0][2] == 99 and calls[1][2] == 87
    assert calls[0][1]['E2E_ATTEMPT_REPORT'] != calls[1][1]['E2E_ATTEMPT_REPORT']
    assert sleeps == [10]
    assert evidence.verify_history(tmp_path, require_full=False) == 2


def test_controller_stops_on_assertion_without_repeating(tmp_path):
    """A generic failed case cannot be repaired by running it again."""
    import subprocess  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    calls = []

    def run(command, **_):
        calls.append(command)
        store(tmp_path, 1, attempt(failed=(NODE,), status=None))
        (tmp_path / 'retry.json').unlink()
        return subprocess.CompletedProcess(command, 1)
    assert sandbox_retry.run_attempts(
        ['e2e/'], tmp_path, 100, runtime=sandbox_retry.RetryRuntime(run, lambda: 0)) == 1
    assert len(calls) == 1


def test_controller_hard_timeout_never_retries(tmp_path):
    """Unknown partial setup belongs to the supervisor's fixed-run cleanup."""
    import subprocess  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel

    def run(command, **_):
        raise subprocess.TimeoutExpired(command, 100)
    assert sandbox_retry.run_attempts(
        ['e2e/'], tmp_path, 100, runtime=sandbox_retry.RetryRuntime(run, lambda: 0)) == 124


def test_all_session_setup_timeouts_can_retry_without_case_cleanup(tmp_path):
    """No case entered on cached read-only session failure; all 17 can start afresh."""
    phases = tuple(phase for node in evidence.EXPECTED_NODEIDS for phase in (
        evidence.PhaseFact(node, 'setup', 'failed', ('SandboxFailure', 'RequestError'),
                           'DEADLINE_EXCEEDED'),
        evidence.PhaseFact(node, 'teardown', 'passed', (), None)))
    first = evidence.AttemptReport(evidence.EXPECTED_NODEIDS, phases, (), (), True, 0, 1)
    store(tmp_path, 1, first)
    store(tmp_path, 2, attempt(evidence.EXPECTED_NODEIDS))
    states = evidence.inspect_history(tmp_path, require_full=True)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path) == 17


@pytest.mark.parametrize('field,value', [
    ('nodeid', []), ('when', []), ('outcome', {}), ('transport_chain', [1]),
    ('transport_chain', ['unknown']), ('transport_chain', 'RequestError'),
    ('grpc_status', []), ('grpc_status', True),
])
def test_invalid_phase_fields_are_safe_failures(field, value):
    """Malformed artifact types must not escape as programming errors."""
    raw = asdict(attempt().phases[0])
    raw['transport_chain'] = []
    raw[field] = value
    with pytest.raises(evidence.RetryFailure):
        evidence.phase_fact(raw)


@pytest.mark.parametrize('change', [
    {'session_cleanup': False}, {'entered': (OTHER,)}, {'collection_errors': -1},
    {'exit_status': 2}, {'phases': ()},
])
def test_incomplete_report_never_allows_retry(change):
    """Cleanup, cancellation, collection and phase boundaries are independent gates."""
    with pytest.raises(evidence.RetryFailure):
        evidence.validate_report(replace(attempt(), **change), 0)


@pytest.mark.parametrize('change', [
    {'when': 'teardown'}, {'outcome': 'passed'}, {'grpc_status': None},
    {'transport_chain': ('other', 'RequestError')},
    {'transport_chain': ('DataAccessError',)},
    {'transport_chain': ('RequestError', 'DataAccessError')},
    {'transport_chain': ('RequestError', 'RequestError')},
])
def test_non_transport_phase_is_not_retryable(change):
    """Statuses alone cannot turn assertion/auth/teardown failures into retries."""
    phase = attempt(failed=(NODE,)).phases[1]
    assert not evidence.transient(replace(phase, **change))


@pytest.mark.parametrize('mode', ['exhausted', 'four-failures', 'backoff-budget'])
def test_controller_stops_at_shared_limits(tmp_path, mode):
    """The controller never extends the supervisor's total trading window."""
    import subprocess  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    calls = []
    pauses = []

    def run(command, **_):
        calls.append(command)
        store(tmp_path, len(calls), attempt(failed=(NODE,)))
        (tmp_path / 'retry.json').unlink()
        return subprocess.CompletedProcess(command, 1)
    clock = iter([0, 100]) if mode == 'exhausted' else None
    runtime = sandbox_retry.RetryRuntime(
        run, (lambda: next(clock)) if clock else (lambda: 0), pauses.append)
    result = sandbox_retry.run_attempts(
        ['e2e/'], tmp_path, 10 if mode == 'backoff-budget' else 100, runtime=runtime)
    assert result == (124 if mode == 'exhausted' else 1)
    assert len(calls) == {'exhausted': 0, 'four-failures': 4, 'backoff-budget': 1}[mode]
    assert pauses == ([10, 10, 10] if mode == 'four-failures' else [])


@pytest.mark.parametrize('arguments,valid', [
    (['e2e/', '--junitxml', 'final.xml'], True),
    (['e2e/', '--junit-xml=final.xml'], True),
    (['e2e/', '--junitxml'], False), (['--junitxml=a.txt'], False),
    (['--junitxml=a.xml', '--junitxml=b.xml'], False),
])
def test_junit_path_selection_is_unambiguous(tmp_path, arguments, valid):
    """Per-attempt original outputs cannot be replaced by a conflicting pytest option."""
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    if valid:
        remaining, path = sandbox_retry.junit_arguments(arguments, tmp_path)
        assert remaining == ['e2e/'] and path == Path('final.xml')
    else:
        with pytest.raises(evidence.RetryFailure):
            sandbox_retry.junit_arguments(arguments, tmp_path)


@pytest.mark.parametrize('fault', ['reuse', 'missing-selector'])
def test_controller_requires_fresh_root_and_suite_selector(tmp_path, fault):
    """Prior evidence and empty collection cannot silently become a successful run."""
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    if fault == 'reuse':
        (tmp_path / 'attempts').mkdir()
    with pytest.raises(evidence.RetryFailure):
        sandbox_retry.run_attempts(['e2e/'] if fault == 'reuse' else [], tmp_path, 10)


def test_controller_writes_requested_final_xml(tmp_path):
    """A local alternate output contains final unique cases, not just the last subset."""
    import subprocess  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    final = tmp_path / 'local.xml'

    def run(command, **_):
        store(tmp_path, 1, attempt())
        (tmp_path / 'retry.json').unlink()
        return subprocess.CompletedProcess(command, 0)
    assert sandbox_retry.run_attempts(
        ['e2e/', '--junitxml', str(final)], tmp_path, 100,
        runtime=sandbox_retry.RetryRuntime(run, lambda: 0)) == 0
    assert evidence.xml_states(final) == {NODE: 'passed'}


@pytest.mark.parametrize('fault', ['supervision', 'timeout', 'run-id', 'evidence'])
def test_retry_cli_fails_closed(monkeypatch, capsys, fault):
    """No direct unbounded invocation and no missing reports can start another attempt."""
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv('E2E_SUPERVISED', '1')
    monkeypatch.setenv('E2E_RUN_ID', 'a' * 32)
    if fault == 'supervision':
        monkeypatch.delenv('E2E_SUPERVISED')
    if fault == 'run-id':
        monkeypatch.delenv('E2E_RUN_ID')

    def invalid(*_):
        raise evidence.RetryFailure('private evidence')
    monkeypatch.setattr(sandbox_retry, 'run_attempts', invalid)
    assert sandbox_retry.main(['--timeout', '0' if fault == 'timeout' else '10',
                               '--', 'e2e/']) == 1
    assert 'private' not in capsys.readouterr().err


@pytest.mark.parametrize('separator', [True, False])
def test_retry_cli_preserves_supervisor_arguments(monkeypatch, tmp_path, separator):
    """The child uses the fixed run directory and the supplied total budget."""
    from unittest.mock import create_autospec  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv('E2E_SUPERVISED', '1')
    monkeypatch.setenv('E2E_RUN_ID', 'a' * 32)
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    run = create_autospec(sandbox_retry.run_attempts, spec_set=True, return_value=0)
    monkeypatch.setattr(sandbox_retry, 'run_attempts', run)
    assert sandbox_retry.main(['--timeout', '10', *(['--'] if separator else []), 'e2e/']) == 0
    run.assert_called_once_with(['e2e/'], tmp_path, 10)


@pytest.mark.parametrize('fault', ['missing', 'oversized', 'json', 'symlink'])
def test_retry_artifact_read_is_bounded_and_safe(tmp_path, monkeypatch, fault):
    """Missing, redirected and damaged artifacts never become acceptance evidence."""
    path = tmp_path / 'report.json'
    if fault == 'oversized':
        monkeypatch.setattr(evidence, 'MAX_BYTES', 2)
        path.write_text('123', encoding='utf-8')
    if fault == 'json':
        path.write_text('{', encoding='utf-8')
    if fault == 'symlink':
        target = tmp_path / 'target'
        target.write_text('{}', encoding='utf-8')
        path.symlink_to(target)
    with pytest.raises(evidence.RetryFailure):
        evidence.read_json(path)


@pytest.mark.parametrize('fault', [
    'unknown-field', 'phase-type', 'cleanup-type', 'bool-int', 'duplicate-node', 'passed-error',
])
def test_retry_report_rejects_coercions(tmp_path, fault):
    """Own DTOs require exact original shape, not truthiness or transport text."""
    raw = json.loads(json.dumps(asdict(attempt())))
    if fault == 'unknown-field':
        raw['success'] = True
    if fault == 'phase-type':
        raw['phases'] = False
    if fault == 'cleanup-type':
        raw['session_cleanup'] = 'true'
    if fault == 'bool-int':
        raw['exit_status'] = False
    if fault == 'duplicate-node':
        raw['selected'] *= 2
    if fault == 'passed-error':
        raw['phases'][0]['transport_chain'] = ['RequestError']
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(evidence.RetryFailure):
        evidence.read_report(path)


@pytest.mark.parametrize('fault', [
    'missing-call', 'failed-setup-call', 'outside-phase', 'no-live-proof',
    'exit-outcomes', 'teardown',
])
def test_phase_lifecycle_must_match_fixture_evidence(fault):
    """A passing call cannot cover a failed setup, teardown or absent actual fixture."""
    report = attempt()
    if fault == 'missing-call':
        report = replace(report, phases=(report.phases[0], report.phases[2]))
    if fault == 'failed-setup-call':
        report = replace(report, phases=(replace(report.phases[0], outcome='failed'),
                                         *report.phases[1:]))
    if fault == 'outside-phase':
        report = replace(report, phases=(*report.phases, replace(report.phases[0], nodeid=OTHER)))
    if fault == 'no-live-proof':
        report = replace(report, entered=(), cleaned=())
    if fault == 'exit-outcomes':
        report = replace(report, exit_status=1)
    if fault == 'teardown':
        report = replace(report, phases=(*report.phases[:2],
                                         replace(report.phases[2], outcome='failed')))
    with pytest.raises(evidence.RetryFailure):
        evidence.validate_report(report, report.exit_status)


@pytest.mark.parametrize('xml', [
    '<!DOCTYPE x><testsuite/>', '<bad', '<other/>', '<testsuites/>',
    '<testsuite tests="1" failures="0" errors="0" skipped="0">'
    '<testcase name="unknown" classname="e2e.test_sandbox"/></testsuite>',
])
def test_invalid_xml_cannot_hide_in_originals(tmp_path, xml):
    """XML shape and node identity are checked independently from the JSON report."""
    path = tmp_path / 'junit.xml'
    path.write_text(xml, encoding='utf-8')
    with pytest.raises(evidence.RetryFailure):
        evidence.xml_states(path)


@pytest.mark.parametrize('fault', [
    'wrong-junit', 'marker-encoding', 'sequence', 'directories', 'merged-disagrees',
])
def test_original_attempts_correlate_with_history_and_final_xml(tmp_path, fault):
    """Original reports, case results, finalizers and the bounded history must agree."""
    store(tmp_path, 1, attempt())
    original = tmp_path / 'attempts/01'
    if fault == 'wrong-junit':
        evidence.write_final_junit({NODE: 'failed'}, original / 'junit.xml')
    if fault == 'marker-encoding':
        (original / 'cleanup.log').write_bytes(b'\xff')
    if fault == 'sequence':
        (tmp_path / 'retry.json').write_text(json.dumps({
            'version': 1, 'attempts': [{'number': 2, 'returncode': 0}]}), encoding='utf-8')
    if fault == 'directories':
        (tmp_path / 'attempts/02').mkdir()
    evidence.write_final_junit(
        {NODE: 'failed' if fault == 'merged-disagrees' else 'passed'}, tmp_path / 'junit.xml')
    with pytest.raises(evidence.RetryFailure):
        evidence.verify_history(tmp_path, require_full=False)


def test_retry_module_entrypoint_fails_safely_without_supervisor():
    """The package CLI imports from a clean environment without touching live APIs."""
    import os  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    environment = dict(os.environ)
    for name in ('PYTHONPATH', 'E2E_SUPERVISED', 'SANDBOX_TOKEN', 'INVEST_TOKEN'):
        environment.pop(name, None)
    result = subprocess.run([sys.executable, '-m', 'scripts.sandbox_retry', '--timeout', '10'],
                            env=environment, capture_output=True, text=True, check=False,
                            timeout=10)
    assert result.returncode == 1
    assert 'cleanup incomplete' in result.stderr and 'Traceback' not in result.stderr


def test_retry_python_entrypoint_returns_failure_without_supervisor(monkeypatch):
    """Executing the script entrypoint must propagate its safe nonzero exit code."""
    import runpy  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    from scripts import sandbox_retry  # pylint: disable=import-outside-toplevel
    monkeypatch.delenv('E2E_SUPERVISED', raising=False)
    monkeypatch.setattr(sys, 'argv', ['sandbox_retry', '--timeout', '10'])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(sandbox_retry.__file__, run_name='__main__')
    assert caught.value.code == 1


@pytest.mark.parametrize('accepted', [True, False])
def test_github_cli_independently_verifies_retry_artifacts(tmp_path, monkeypatch, accepted):
    """Full original selection remains mandatory after a later subset finally passes."""
    from scripts import sandbox_github  # pylint: disable=import-outside-toplevel
    first = attempt(evidence.EXPECTED_NODEIDS, (NODE,))
    store(tmp_path, 1, first)
    store(tmp_path, 2, attempt())
    states = evidence.inspect_history(tmp_path)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    if not accepted:
        (tmp_path / 'attempts/01/cleanup.log').write_text('', encoding='utf-8')
    output = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    result = sandbox_github.main(['evidence', '--junit', str(tmp_path / 'junit.xml')])
    assert result == (0 if accepted else 1)
    if accepted:
        assert output.read_text() == 'complete=true\npassed=17\n'
    else:
        assert not output.exists()


@pytest.mark.parametrize('format_name,path', [
    ('single-v1', 'junit.xml'), ('retry-v1', 'other.xml'),
])
def test_github_evidence_format_never_implicitly_falls_back(tmp_path, format_name, path):
    """Legacy markers require an explicit log and retry evidence a fixed final filename."""
    from scripts import sandbox_github  # pylint: disable=import-outside-toplevel
    with pytest.raises(sandbox_github.GitHubFailure):
        sandbox_github.evidence_result(tmp_path / path, None, format_name)


def test_github_helper_package_smoke_has_no_pythonpath_dependency(tmp_path):
    """Fresh Actions runners can load the trusted helper with only the stdlib."""
    import os  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([
        sys.executable, '-m', 'scripts.sandbox_github', 'evidence',
        '--junit', str(tmp_path / 'junit.xml')], env=environment,
        capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 1
    assert 'no success status' in result.stderr and 'ModuleNotFoundError' not in result.stderr
