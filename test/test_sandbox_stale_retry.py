"""Whole-attempt auth guards revoke test-owned transport and stale retry decisions."""
from unittest.mock import create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.strategy_data import DataAccessError
from e2e import retry_policy as policy
from scripts.retry_evidence import read_result
from scripts.retry_results import retry_nodes
from scripts.sandbox_lifecycle import SandboxFailure, list_exception_facts
from scripts.sandbox_retry_report import AttemptRecorder


FIRST = 'e2e/test_cases.py::test_first'
SECOND = 'e2e/test_cases.py::test_second'


@pytest.mark.parametrize('status', [StatusCode.UNAUTHENTICATED, StatusCode.PERMISSION_DENIED])
@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('link', ['__cause__', '__context__'])
@pytest.mark.parametrize('wrappers', [0, 7, 8, 9])
def test_auth_chain_blocks_all_retryable_cases(tmp_path, status, stored, link, wrappers):
    """Raw/stored auth and incomplete inspection revoke every permission in the attempt."""
    error = RequestError(status, 'private', {'token': 'private'})
    for _ in range(wrappers):
        wrapper = DataAccessError('private')
        setattr(wrapper, link, error)
        error = wrapper
    if stored:
        error = SandboxFailure('fixed', api_facts=list_exception_facts(error))
    assert policy.auth_failure(error)
    assert not policy.may_retry(error, 'call', 'failed')
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.selected = (FIRST, SECOND)
    for node in recorder.selected:
        recorder.phase(node, 'setup', 'passed', None)
        recorder.phase(node, 'call', 'failed', error if node == SECOND else None,
                       retryable=True)
        recorder.phase(node, 'teardown', 'passed', None)
    recorder.finish(1)
    report = read_result(recorder.path)
    assert not retry_nodes(report.phases, report.selected)
    assert 'private' not in recorder.path.read_text()


@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('count', [7, 8, 9])
def test_auth_inspection_requires_complete_chain(stored, count):
    """Raw termination is observable; a full stored buffer may have lost its tail."""
    error = ValueError('private')
    for _ in range(count - 1):
        wrapper = DataAccessError('private')
        wrapper.__cause__ = error
        error = wrapper
    if stored:
        error = SandboxFailure('fixed', api_facts=list_exception_facts(error))
    assert policy.auth_failure(error) == (count >= (8 if stored else 9))


def test_cyclic_auth_inspection_stops_all_retries(tmp_path):
    """A cycle cannot prove that auth inspection reached the end."""
    error = DataAccessError('private')
    error.__cause__ = error
    assert policy.auth_failure(error)
    assert not policy.may_retry(error, 'setup', 'failed')
    recorder = AttemptRecorder(tmp_path / 'result.json')
    recorder.phase(FIRST, 'call', 'failed', error, retryable=True)
    recorder.finish(1)
    assert not read_result(recorder.path).phases[0].retryable


def test_stored_auth_inspection_is_bounded():
    """An uninspected stored suffix blocks retry without scanning beyond its limit."""
    unread = create_autospec(dict, instance=True, spec_set=True)
    unread.get.side_effect = AssertionError('stored inspection limit exceeded')
    error = SandboxFailure('fixed', api_facts=[{'type': 'DataAccessError'}] * 8 + [unread])
    assert policy.auth_failure(error)
    unread.get.assert_not_called()


@pytest.mark.parametrize('wrapper', [DataAccessError, AssertionError, ValueError])
def test_untraced_value_error_never_authorizes_stale_retry(wrapper):
    """An arbitrary cause without the actual guard traceback stays permanent."""
    error = wrapper('private')
    error.__cause__ = ValueError('stale FX order book')
    assert not policy.may_retry(error, 'call', 'failed')


def test_hidden_or_cyclic_validation_chain_cannot_authorize_retry():
    """Suppressed exception context and repeated wrappers provide no guard provenance."""
    error = DataAccessError('private')
    error.__cause__ = error
    assert not policy.stale_guard(error)
    error.__cause__ = None
    error.__suppress_context__ = True
    error.__context__ = ValueError('private')
    assert not policy.stale_guard(error)
