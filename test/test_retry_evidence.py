"""Original results must prove a full run without interpreting failure causes."""
from dataclasses import asdict
import json
import xml.etree.ElementTree as ET

import pytest

from scripts.retry_evidence import inspect_history, verify_history, write_junit
from scripts import retry_evidence
from scripts import sandbox_github
from scripts.retry_results import PhaseResult
from scripts.retry_settings import RunSettings


SHA = 'a' * 40
FIRST = 'e2e/test_any.py::test_first'
SECOND = 'e2e/test_any.py::test_second'


@pytest.mark.parametrize('sha', [None, SHA])
def test_first_actual_nonempty_attempt_defines_run_without_inventory(tmp_path, sha):
    """Local runs need no SHA; trusted runs bind originals directly to their SHA."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report['sha'] = sha
    path.write_text(json.dumps(report))
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    assert verify_history(tmp_path, settings, expected_sha=sha) == 1


@pytest.mark.parametrize('first_sha,second_sha', [(SHA, 'b' * 40), (None, SHA), (SHA, None)])
def test_all_attempts_keep_same_sha_without_external_inventory(tmp_path, first_sha, second_sha):
    """Even local verification cannot combine results from different checkouts."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('failed', True)}),
               write_attempt(tmp_path, 2, {FIRST: ('passed', False)})]
    for number, sha in enumerate((first_sha, second_sha), 1):
        path = tmp_path / 'attempts' / f'{number:02}' / 'result.json'
        report = json.loads(path.read_text())
        report['sha'] = sha
        path.write_text(json.dumps(report))
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError, match='SHA'):
        inspect_history(tmp_path, settings)


def test_artifact_settings_must_contain_the_full_authorized_snapshot(tmp_path):
    """An artifact cannot silently fill missing launch budgets from verifier defaults."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    (tmp_path / 'run-settings.json').write_text('{}')
    with pytest.raises(ValueError, match='settings'):
        inspect_history(tmp_path, settings)


def mark_original_skipped(path):
    """Change a failed original into a well-formed JUnit skip for negative tests."""
    suite = ET.parse(path).getroot()
    suite.set('failures', '0')
    suite.set('skipped', '1')
    suite.find('testcase/failure').tag = 'skipped'
    ET.ElementTree(suite).write(path)


def write_attempt(root, number, outcomes):
    """Write independent structured originals with one phase flag per failure."""
    directory = root / 'attempts' / f'{number:02}'
    directory.mkdir(parents=True, exist_ok=True)
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
    (root / 'retry.json').write_text(
        json.dumps({'version': 1, 'attempts': entries}), encoding='utf-8')
    (root / 'run-settings.json').write_text(settings.to_json(), encoding='utf-8')
    write_junit(states, root / 'junit.xml')


@pytest.mark.parametrize('fault', [None, 'sha', 'settings', 'cleanup', 'permanent'])
def test_trusted_v4_cli_uses_fixed_sha_and_hour_budget(tmp_path, monkeypatch, fault):
    """The transition validates v4 originals, never a PR-provided verifier or inventory."""
    settings = RunSettings(timeout_seconds=3600)
    states = {FIRST: 'failed' if fault == 'permanent' else 'passed'}
    entries = [write_attempt(tmp_path, 1, {FIRST: (states[FIRST], False)})]
    write_history(tmp_path, entries, states, settings)
    if fault == 'settings':
        (tmp_path / 'run-settings.json').write_text(RunSettings().to_json())
    if fault == 'cleanup':
        (tmp_path / 'attempts/01/cleanup.log').write_text('')
    output = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    arguments = ['evidence', '--evidence-format', 'retry-v4',
                 '--junit', str(tmp_path / 'junit.xml'),
                 '--sha', 'b' * 40 if fault == 'sha' else SHA]
    if fault is None:
        assert sandbox_github.main(arguments) == 0
        assert output.read_text() == 'complete=true\npassed=1\n'
    else:
        assert sandbox_github.main(arguments) == 1
        assert not output.exists()


def test_retry_permission_is_the_test_owned_boolean(tmp_path):
    """An allowed retry needs no SDK class, transport code or market timestamp."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {
        FIRST: ('passed', False), SECOND: ('failed', True)}),
        write_attempt(tmp_path, 2, {SECOND: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed', SECOND: 'passed'}, settings)
    assert verify_history(tmp_path, settings, expected_sha=SHA) == 2


def test_permanent_failure_is_preserved_while_other_test_retries(tmp_path):
    """A later green subset cannot hide the permanent result from the first pass."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {
        FIRST: ('failed', False), SECOND: ('failed', True)}),
        write_attempt(tmp_path, 2, {SECOND: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'failed', SECOND: 'passed'}, settings)
    assert inspect_history(tmp_path, settings, expected_sha=SHA) == {
        FIRST: 'failed', SECOND: 'passed'}
    with pytest.raises(ValueError, match='failed'):
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_attempt_limit_is_launch_configuration_not_a_master_constant(tmp_path):
    """More than four attempts can be permitted explicitly for this run."""
    settings = RunSettings(max_attempts=6)
    entries = [write_attempt(tmp_path, number, {
        FIRST: ('passed', False) if number == 6 else ('failed', True)})
        for number in range(1, 7)]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    assert verify_history(tmp_path, settings, expected_sha=SHA) == 1


def test_all_collected_tests_are_required_regardless_of_count(tmp_path):
    """The verifier is not coupled to seventeen or twenty-six named scenarios."""
    settings = RunSettings()
    selected = tuple(f'e2e/test_any.py::test_case_{index}' for index in range(31))
    entries = [write_attempt(tmp_path, 1, {node: ('passed', False) for node in selected})]
    write_history(tmp_path, entries, dict.fromkeys(selected, 'passed'), settings)
    assert verify_history(tmp_path, settings, expected_sha=SHA) == 31


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
        verify_history(tmp_path, settings, expected_sha=SHA)


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
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_settings_cannot_be_changed_by_attempts_or_artifacts(tmp_path):
    """Independent verification uses the authorized launch snapshot."""
    settings = RunSettings(max_attempts=2)
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, RunSettings(max_attempts=6))
    with pytest.raises(ValueError, match='settings'):
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_skipped_original_cannot_be_relabelled_failed_then_retried(tmp_path):
    """A skip cannot be disguised by a failed boolean report and a green retry."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('failed', True)}),
               write_attempt(tmp_path, 2, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/junit.xml'
    mark_original_skipped(path)
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError, match='skipped'):
        verify_history(tmp_path, settings, expected_sha=SHA)


@pytest.mark.parametrize('fault', ['duplicate-key', 'unknown-field',
                                   'cleanup', 'missing-case', 'counter', 'final', 'symlink'])
def test_corrupt_originals_are_not_repaired_by_a_green_summary(tmp_path, fault):
    """The final summary never repairs malformed original or lifecycle evidence."""
    settings = RunSettings(max_attempts=1)
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    attempt = tmp_path / 'attempts/01'
    if fault == 'duplicate-key':
        path = attempt / 'result.json'
        path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    if fault == 'unknown-field':
        path = attempt / 'result.json'
        value = json.loads(path.read_text())
        value['unexpected'] = 1
        path.write_text(json.dumps(value))
    if fault == 'cleanup':
        (attempt / 'cleanup.log').write_text('')
    if fault == 'missing-case':
        write_junit({}, attempt / 'junit.xml')
    if fault == 'counter':
        path = attempt / 'junit.xml'
        path.write_text(path.read_text().replace('tests="1"', 'tests="2"'))
    if fault == 'final':
        write_junit({FIRST: 'failed'}, tmp_path / 'junit.xml')
    if fault == 'symlink':
        original = attempt / 'result.json'
        target = tmp_path / 'saved.json'
        original.rename(target)
        original.symlink_to(target)
    with pytest.raises(ValueError):
        verify_history(tmp_path, settings, expected_sha=SHA)


@pytest.mark.parametrize('fault', ['extra', 'gap', 'limit'])
def test_corrupt_attempt_sequence_cannot_pass(tmp_path, fault):
    """A run cannot omit, add or exceed its authorized attempt history."""
    settings = RunSettings(max_attempts=1)
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    if fault == 'extra':
        (tmp_path / 'attempts/02').mkdir()
    if fault == 'gap':
        entries[0]['number'] = 2
        (tmp_path / 'retry.json').write_text(json.dumps({'version': 1, 'attempts': entries}))
    if fault == 'limit':
        entries.append(write_attempt(tmp_path, 2, {FIRST: ('passed', False)}))
        write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    with pytest.raises(ValueError):
        verify_history(tmp_path, settings, expected_sha=SHA)


@pytest.mark.parametrize('selection', [(FIRST,), (FIRST, SECOND), ()])
def test_retry_selection_cannot_include_passed_or_omit_eligible_tests(tmp_path, selection):
    """Selection is exactly the permitted failed subset, never an arbitrary replay."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {
        FIRST: ('passed', False), SECOND: ('failed', True)}),
        write_attempt(tmp_path, 2, {node: ('passed', False) for node in selection})]
    write_history(tmp_path, entries, {FIRST: 'passed', SECOND: 'passed'}, settings)
    with pytest.raises(ValueError):
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_real_pytest_shaped_setup_error_and_parameterized_method(tmp_path):
    """Class and parameter identities agree with pytest's original JUnit schema."""
    node = 'e2e/test_other.py::TestExample::test_case[a::b]'
    path = tmp_path / 'junit.xml'
    path.write_text('<testsuites><testsuite tests="1" failures="0" errors="1" skipped="0">'
                    '<testcase classname="e2e.test_other.TestExample" name="test_case[a::b]">'
                    '<error message="setup failed"/></testcase></testsuite></testsuites>')
    assert retry_evidence.xml_states(path, (node,)) == {node: 'failed'}


@pytest.mark.parametrize('field,value', [
    ('version', 2), ('phases', False), ('session_cleanup', 'true'),
    ('selected', [FIRST, FIRST]), ('exit_status', False),
])
def test_original_result_schema_rejects_coercion(tmp_path, field, value):
    """Counters, lifecycle proof and phase lists have exact original types."""
    write_attempt(tmp_path, 1, {FIRST: ('passed', False)})
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        retry_evidence.read_result(path)


@pytest.mark.parametrize('fault', [
    'empty-first', 'outside-fixture', 'no-fixture', 'canceled', 'missing-directories',
    'original-disagrees', 'symlink-directory',
])
def test_lifecycle_guards_remain_required_without_collection_inventory(tmp_path, fault):
    """A retry permission never excuses missing execution or cleanup evidence."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    if fault == 'empty-first':
        report.update(selected=[], phases=[], entered=[], cleaned=[])
    elif fault == 'outside-fixture':
        report.update(entered=[FIRST, SECOND], cleaned=[FIRST, SECOND])
    elif fault == 'no-fixture':
        report.update(entered=[], cleaned=[])
    elif fault == 'canceled':
        report['exit_status'] = entries[0]['returncode'] = 143
    path.write_text(json.dumps(report))
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    if fault == 'missing-directories':
        (tmp_path / 'attempts').rename(tmp_path / 'saved-attempts')
    elif fault == 'symlink-directory':
        (tmp_path / 'attempts/01').rename(tmp_path / 'saved-attempt')
        (tmp_path / 'attempts/01').symlink_to(tmp_path / 'saved-attempt', target_is_directory=True)
    elif fault == 'original-disagrees':
        write_junit({FIRST: 'failed'}, tmp_path / 'attempts/01/junit.xml')
    with pytest.raises(ValueError):
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_setup_failure_without_scenario_fixture_can_retry_after_session_cleanup(tmp_path):
    """Failed session setup has no entered scenario, but still requires its finalizers."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('failed', True)}),
               write_attempt(tmp_path, 2, {FIRST: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report.update(entered=[], cleaned=[], phases=[
        asdict(PhaseResult(FIRST, 'setup', 'failed', True)),
        asdict(PhaseResult(FIRST, 'teardown', 'passed', False))])
    path.write_text(json.dumps(report))
    (path.parent / 'cleanup.log').write_text('Sandbox session cleanup complete\n')
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    assert verify_history(tmp_path, settings, expected_sha=SHA) == 1


def test_colliding_junit_identities_are_not_accepted(tmp_path):
    """Distinct selectors must remain distinguishable in the original JUnit."""
    first = 'e2e/nested/test_cases.py::TestCases::test_case[a::b]'
    second = 'e2e.nested/test_cases.py::TestCases::test_case[a::b]'
    path = tmp_path / 'junit.xml'
    write_junit({first: 'passed'}, path)
    with pytest.raises(ValueError, match='ambiguous'):
        retry_evidence.xml_states(path, (first, second))


@pytest.mark.parametrize('function,value', [
    (retry_evidence.checked_sha, '0' * 40), (retry_evidence.checked_sha, 'abc'),
    (retry_evidence.nodeids, 'not-a-list'), (retry_evidence.nodeids, [FIRST, FIRST]),
    (retry_evidence.nodeids, ['--bad']), (retry_evidence.integer, True),
    (retry_evidence.integer, '1'),
    (retry_evidence.phase_result, {'nodeid': FIRST}),
    (retry_evidence.phase_result, {'nodeid': FIRST, 'when': 0, 'outcome': 'failed',
                                   'retryable': True}),
])
def test_generic_evidence_boundaries_reject_ambiguous_values(function, value):
    """Counters, selectors, commit IDs and phase fields are never coerced."""
    with pytest.raises(ValueError):
        function(value)


@pytest.mark.parametrize('raw', [b'{', b'\xff', b'[]', b'null', b'{}'])
def test_invalid_result_payloads_fail_closed(tmp_path, raw):
    """Malformed or incomplete JSON cannot act as an empty passing result."""
    path = tmp_path / 'result.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        retry_evidence.read_result(path)


@pytest.mark.parametrize('fault', ['missing', 'oversized', 'duplicate-case', 'bad-xml',
                                   'doctype', 'unknown-case', 'bad-root', 'missing-suite',
                                   'bad-cleanup-unicode', 'bad-settings-unicode'])
def test_unreadable_or_invalid_evidence_is_not_assumed_green(tmp_path, monkeypatch, fault):
    """File, XML and encoding errors invalidate the entire acceptance proof."""
    settings = RunSettings()
    entries = [write_attempt(tmp_path, 1, {FIRST: ('passed', False)})]
    write_history(tmp_path, entries, {FIRST: 'passed'}, settings)
    attempt = tmp_path / 'attempts/01'
    path = attempt / 'junit.xml'
    if fault == 'missing':
        path.unlink()
    elif fault == 'oversized':
        monkeypatch.setattr(retry_evidence, 'MAX_BYTES', 10)
    elif fault == 'duplicate-case':
        text = path.read_text()
        case = text[text.index('<testcase'):text.index('</testsuite>')]
        path.write_text(text.replace('</testsuite>', case + '</testsuite>')
                        .replace('tests="1"', 'tests="2"'))
    elif fault == 'bad-xml':
        path.write_text('<testsuite')
    elif fault == 'doctype':
        path.write_text('<!DOCTYPE testsuite><testsuite/>')
    elif fault == 'unknown-case':
        write_junit({SECOND: 'passed'}, path)
    elif fault == 'bad-root':
        path.write_text('<root><testsuite tests="0" failures="0" errors="0" skipped="0"/></root>')
    elif fault == 'missing-suite':
        path.write_text('<testsuites/>')
    elif fault == 'bad-cleanup-unicode':
        (attempt / 'cleanup.log').write_bytes(b'\xff')
    else:
        (tmp_path / 'run-settings.json').write_bytes(b'\xff')
    with pytest.raises(ValueError):
        verify_history(tmp_path, settings, expected_sha=SHA)
