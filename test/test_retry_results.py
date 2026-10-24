"""The orchestrator consumes explicit retry decisions, not failure semantics."""
from dataclasses import replace

import pytest

from scripts.retry_results import PhaseResult, result_states, retry_nodes


FIRST = 'e2e/test_arbitrary.py::test_first'
SECOND = 'e2e/test_arbitrary.py::test_second'


def phases(node=FIRST, *, outcome='failed', retryable=False):
    """Every ordinary test has completed setup, call and teardown phases."""
    return (
        PhaseResult(node, 'setup', 'passed', False),
        PhaseResult(node, 'call', outcome, retryable),
        PhaseResult(node, 'teardown', 'passed', False),
    )


@pytest.mark.parametrize('retryable,expected', [(True, (FIRST,)), (False, ())])
def test_failed_test_has_one_explicit_retry_decision(retryable, expected):
    """No exception type, clock threshold or brokerage fact is required by CI."""
    results = phases(retryable=retryable)
    assert result_states(results, (FIRST,)) == {FIRST: 'failed'}
    assert retry_nodes(results, (FIRST,)) == expected


def test_retryable_failures_are_selected_independently_of_permanent_failures():
    """Permanent failure remains visible but cannot suppress a permitted retry."""
    results = phases(retryable=False) + phases(SECOND, retryable=True)
    assert retry_nodes(results, (FIRST, SECOND)) == (SECOND,)
    assert result_states(results, (FIRST, SECOND)) == {FIRST: 'failed', SECOND: 'failed'}


def test_passed_test_is_never_repeated():
    """Only still-failing tests can be selected for another attempt."""
    results = phases(outcome='passed') + phases(SECOND, retryable=True)
    assert retry_nodes(results, (FIRST, SECOND)) == (SECOND,)


def test_retryable_setup_failure_requires_successful_teardown():
    """An unfinished setup has a valid two-phase lifecycle, not a fabricated call."""
    results = (
        PhaseResult(FIRST, 'setup', 'failed', True),
        PhaseResult(FIRST, 'teardown', 'passed', False),
    )
    assert retry_nodes(results, (FIRST,)) == (FIRST,)


@pytest.mark.parametrize('value', [None, 0, 1, '', 'true', 'false', [], {}])
def test_retry_decision_is_mandatory_boolean(value):
    """Missing, unknown or truthy values cannot grant retry permission."""
    with pytest.raises(ValueError):
        PhaseResult(FIRST, 'call', 'failed', value)


@pytest.mark.parametrize('when,outcome', [
    ('setup', 'passed'), ('call', 'passed'), ('call', 'skipped'),
    ('teardown', 'failed'), ('teardown', 'passed'),
])
def test_only_failed_setup_or_call_can_request_retry(when, outcome):
    """Cleanup failures and skipped tests cannot be repaired by replaying a trade."""
    with pytest.raises(ValueError):
        PhaseResult(FIRST, when, outcome, True)


@pytest.mark.parametrize('changes', [
    {'nodeid': ''}, {'nodeid': '--override'}, {'nodeid': 'bad\x00id'},
    {'when': 'unknown'}, {'outcome': 'unknown'},
])
def test_invalid_phase_shape_is_rejected(changes):
    """The shared contract validates structure rather than application details."""
    with pytest.raises(ValueError):
        replace(PhaseResult(FIRST, 'call', 'failed', False), **changes)


@pytest.mark.parametrize('results,selected', [
    ((), (FIRST,)),
    (phases(), ()),
    (phases(), (FIRST, FIRST)),
    (phases(), (SECOND,)),
    (phases() + phases(), (FIRST,)),
    (phases()[:2], (FIRST,)),
    ((phases()[0], phases()[2]), (FIRST,)),
    ((PhaseResult(FIRST, 'setup', 'failed', True), *phases()[1:]), (FIRST,)),
    (phases(outcome='skipped'), (FIRST,)),
    ((phases()[0], phases()[1], PhaseResult(FIRST, 'teardown', 'failed', False)), (FIRST,)),
])
def test_incomplete_or_inconsistent_results_stop_all_retries(results, selected):
    """Unknown tests, missing phases and cleanup failures never produce acceptance."""
    with pytest.raises(ValueError):
        result_states(results, selected)


def test_arbitrary_collected_suite_size_and_order_are_preserved():
    """There is no frozen list of seventeen scenarios in the orchestrator."""
    selected = tuple(f'e2e/test_any.py::test_case_{index}' for index in range(31))
    results = tuple(phase for node in selected for phase in phases(node, retryable=True))
    assert retry_nodes(results, selected) == selected


@pytest.mark.parametrize('node', [
    '../test_case.py::test_case', '/tmp/test_case.py::test_case',
    'e2e/../test_case.py::test_case', 'e2e/test_case.py',
    'e2e/test_case.py::', 'e2e/test_case.py::test_case\tunsafe',
])
def test_selectors_cannot_escape_checkout_or_become_process_options(node):
    """Actual pytest selectors remain relative, explicit and free of controls."""
    with pytest.raises(ValueError):
        PhaseResult(node, 'call', 'failed', True)


def test_nested_module_class_and_parameter_selectors_are_valid():
    """Class members and opaque pytest parameter labels preserve their identity."""
    node = 'e2e/nested/test_cases.py::TestOuter::TestInner::test_case[a::b / c]'
    results = phases(node, retryable=True)
    assert retry_nodes(results, (node,)) == (node,)
