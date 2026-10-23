"""Original results must prove a full run without interpreting failure causes."""
from dataclasses import asdict
import json

import pytest

from scripts.retry_evidence import Collection, inspect_history, verify_history, write_junit
from scripts.retry_results import PhaseResult
from scripts.retry_settings import RunSettings


SHA = 'a' * 40
FIRST = 'e2e/test_any.py::test_first'
SECOND = 'e2e/test_any.py::test_second'


def write_attempt(root, number, outcomes):
    """Write independent structured originals with one phase flag per failure."""
    directory = root / 'attempts' / f'{number:02}'
    directory.mkdir(parents=True)
    selected = tuple(outcomes)
    phases = []
    states = {}
    for node, (outcome, retryable) in outcomes.items():
        phases.extend([
            asdict(PhaseResult(node, 'setup', 'passed', False)),
            asdict(PhaseResult(node, 'call', outcome, retryable)),
            asdict(PhaseResult(node, 'teardown', 'passed', False)),
        ])
        states[node] = outcome
    status = int(any(state != 'passed' for state in states.values()))
    report = {
        'version': 1, 'selected': selected, 'phases': phases,
        'entered': selected, 'cleaned': selected, 'session_cleanup': True,
        'collection_errors': 0, 'exit_status': status, 'sha': SHA,
    }
    (directory / 'result.json').write_text(json.dumps(report), encoding='utf-8')
    (directory / 'cleanup.log').write_text(
        'Sandbox scenario cleanup complete\n' * len(selected)
        + 'Sandbox session cleanup complete\n', encoding='utf-8')
    write_junit(states, directory / 'junit.xml')
    return {'number': number, 'returncode': status}


def write_history(root, entries, states, settings):
    """Effective configuration is immutable input shared with the verifier."""
    (root / 'retry.json').write_text(json.dumps({'version': 1, 'attempts': entries}),
                                    encoding='utf-8')
    (root / 'run-settings.json').write_text(settings.to_json(), encoding='utf-8')
    write_junit(states, root / 'junit.xml')


def test_retry_permission_is_the_test_owned_boolean(tmp_path):
    """An allowed retry needs no SDK class, transport code or market timestamp."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {
        FIRST: ('passed', False), SECOND: ('failed', True)}),
        write_attempt(tmp_path, 2, {SECOND: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed', SECOND: 'passed'}, settings)
    assert verify_history(tmp_path, Collection(SHA, (FIRST, SECOND)), settings) == 2


def test_permanent_failure_is_preserved_while_other_test_retries(tmp_path):
    """A later green subset cannot hide the permanent result from the first pass."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {
        FIRST: ('failed', False), SECOND: ('failed', True)}),
        write_attempt(tmp_path, 2, {SECOND: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'failed', SECOND: 'passed'}, settings)
    assert inspect_history(tmp_path, Collection(SHA, (FIRST, SECOND)), settings) == {
        FIRST: 'failed', SECOND: 'passed'}
    with pytest.raises(ValueError, match='failed'):
        verify_history(tmp_path, Collection(SHA, (FIRST, SECOND)), settings)


def test_attempt_limit_is_launch_configuration_not_a_master_constant(tmp_path):
    """More than four attempts can be permitted explicitly for this run."""
    settings = RunSettings(max_attempts=6)
    entries = [write_attempt(tmp_path, number, {
        FIRST: ('passed', False) if number == 6 else ('failed', True)})
        for number in range(1, 7)]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    assert verify_history(tmp_path, Collection(SHA, (FIRST,)), settings) == 1


def test_all_collected_tests_are_required_regardless_of_count(tmp_path):
    """The verifier is not coupled to seventeen or twenty-six named scenarios."""
    settings = RunSettings()
    selected = tuple(f'e2e/test_any.py::test_case_{index}' for index in range(31))
    entries = [write_attempt(tmp_path, 1, {node: ('passed', False) for node in selected})]
    write_history(tmp_path, entries, dict.fromkeys(selected, 'passed'), settings)
    assert verify_history(tmp_path, Collection(SHA, selected), settings) == 31


@pytest.mark.parametrize('retryable', [False, None, 1, 'true'])
def test_unpermitted_or_invalid_retry_cannot_pass(tmp_path, retryable):
    """A consumer never invents permission from error metadata or truthiness."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('failed', True)}),
               write_attempt(tmp_path, 2, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report['phases'][1]['retryable'] = retryable
    path.write_text(json.dumps(report), encoding='utf-8')
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError):
        verify_history(tmp_path, Collection(SHA, (FIRST,)), settings)


@pytest.mark.parametrize('field,value', [
    ('sha', 'b' * 40), ('session_cleanup', False), ('cleaned', []),
    ('collection_errors', 1), ('exit_status', 1),
])
def test_sha_cleanup_collection_and_exit_proof_remain_strict(tmp_path, field, value):
    """One retry flag cannot override independent lifecycle and identity checks."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report), encoding='utf-8')
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError):
        verify_history(tmp_path, Collection(SHA, (FIRST,)), settings)


def test_settings_cannot_be_changed_by_attempts_or_artifacts(tmp_path):
    """Independent verification uses the authorized launch snapshot."""
    settings = RunSettings(max_attempts=2)
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, RunSettings(max_attempts=6))
    with pytest.raises(ValueError, match='settings'):
        verify_history(tmp_path, Collection(SHA, (FIRST,)), settings)


def test_a_green_subset_cannot_replace_the_full_first_collection(tmp_path):
    """The independent full collection controls acceptance, not report counters."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError, match='collection'):
        verify_history(tmp_path, Collection(SHA, (FIRST, SECOND)), settings)
