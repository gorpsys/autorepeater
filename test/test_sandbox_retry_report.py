"""Transport provenance and actual finalizers are recorded without SDK text."""
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import create_autospec, call

import pytest
from grpc import RpcError, StatusCode
from t_tech.invest import RequestError

from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure, SandboxLifecycle, safe_call
from scripts import sandbox_retry_report as reporting
from scripts.sandbox_retry_evidence import read_report


@pytest.mark.parametrize('status,expected', [
    (StatusCode.DEADLINE_EXCEEDED, 'DEADLINE_EXCEEDED'),
    (StatusCode.UNAVAILABLE, 'UNAVAILABLE'), (StatusCode.INTERNAL, None),
    (StatusCode.UNAUTHENTICATED, None), (StatusCode.FAILED_PRECONDITION, None),
])
def test_known_transport_root_status_only(status, expected):
    """Transient classification never comes from a textual exception message."""
    transport = RequestError(status, 'private', {'token': 'private'})
    wrapper = DataAccessError('private')
    wrapper.__cause__ = transport
    chain, actual = reporting.transport_facts(wrapper)
    assert actual == expected
    assert chain == ('DataAccessError', 'RequestError')


def test_assertion_with_transport_cause_cannot_be_retried():
    """An SDK failure buried under a logic assertion is still a permanent failure."""
    error = AssertionError('private')
    error.__cause__ = RequestError(StatusCode.UNAVAILABLE, 'private', {})
    assert reporting.transport_facts(error) == (('other',), None)


@pytest.mark.parametrize('wrapped', [False, True])
def test_request_error_stops_at_typed_sdk_boundary(wrapped):
    """SDK transport details below RequestError do not change retry permission."""
    transport = RequestError(StatusCode.DEADLINE_EXCEEDED, 'private', {})
    transport.__cause__ = RpcError('private grpc details')
    error = DataAccessError('private') if wrapped else transport
    if wrapped:
        error.__cause__ = transport
    chain = ('DataAccessError', 'RequestError') if wrapped else ('RequestError',)
    assert reporting.transport_facts(error) == (chain, 'DEADLINE_EXCEEDED')


def test_unknown_intermediate_wrapper_cannot_gain_transport_permission():
    """A valid SDK cause does not override an intervening logic failure."""
    error = DataAccessError('private')
    error.__cause__ = AssertionError('private')
    error.__cause__.__cause__ = RequestError(StatusCode.UNAVAILABLE, 'private', {})
    assert reporting.transport_facts(error)[1] is None


def test_safe_call_facts_preserve_verified_transport_without_unsafe_context():
    """The setup wrapper stores allowlisted facts rather than the original SDK error."""
    def query():
        raise RequestError(StatusCode.DEADLINE_EXCEEDED, 'private', {}) from RpcError('private')
    with pytest.raises(SandboxFailure) as caught:
        safe_call(query)
    assert reporting.transport_facts(caught.value) == (
        ('SandboxFailure', 'RequestError'), 'DEADLINE_EXCEEDED')


@pytest.mark.parametrize('facts', [
    [{'type': 'AssertionError'}, {'type': 'RequestError', 'grpc_status': 'UNAVAILABLE'}],
    [{'type': 'DataAccessError'}],
])
def test_setup_wrapper_does_not_hide_unknown_or_absent_transport(facts):
    """Retained setup facts must carry a complete permitted prefix, not just any cause."""
    assert reporting.transport_facts(SandboxFailure('fixed', api_facts=facts))[1] is None


def test_report_records_failed_session_setup_without_scenario_markers(tmp_path):
    """A cleaned empty lifecycle can safely retry all cached setup failures."""
    node = 'e2e/test_sandbox.py::test_full_balanced_imoex_oblg_gold'
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.selected = (node,)
    error = RequestError(StatusCode.UNAVAILABLE, 'private', {})
    recorder.phase(node, 'setup', 'failed', error)
    recorder.phase(node, 'teardown', 'passed', None)
    recorder.session_cleanup = True
    recorder.finish(1)
    facts = read_report(tmp_path / 'report.json')
    assert not facts.entered and not facts.cleaned and facts.session_cleanup
    assert (tmp_path / 'cleanup.log').read_text() == 'Sandbox session cleanup complete\n'
    assert 'private' not in (tmp_path / 'report.json').read_text()


def test_hook_keeps_original_phases_after_teardown(tmp_path, monkeypatch):
    """The original phase recorder also works when capture hides console markers."""
    from e2e import conftest  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv('E2E_ATTEMPT_REPORT', str(tmp_path / 'report.json'))
    node = 'e2e/test_sandbox.py::test_full_balanced_imoex_oblg_gold'
    session = SimpleNamespace(config=SimpleNamespace(), items=[SimpleNamespace(nodeid=node)])
    conftest.pytest_sessionstart(session)
    conftest.pytest_collection_finish(session)
    item = SimpleNamespace(nodeid=node, session=session)
    for when in ('setup', 'call', 'teardown'):
        hook = conftest.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
        next(hook)
        with pytest.raises(StopIteration):
            result = SimpleNamespace(when=when, outcome='passed')
            hook.send(SimpleNamespace(get_result=lambda result=result: result))
    recorder = session.config.sandbox_retry_recorder
    recorder.entered.append(node)
    recorder.cleaned.append(node)
    recorder.session_cleanup = True
    conftest.pytest_sessionfinish(session, 0)
    facts = read_report(tmp_path / 'report.json')
    assert [phase.when for phase in facts.phases] == ['setup', 'call', 'teardown']


@pytest.mark.parametrize('failure', ['none', 'preflight', 'scan', 'cleanup', 'unknown-open'])
def test_session_proves_cleanup_before_retry(tmp_path, monkeypatch, failure):
    """A yielded session must find unknown-created IDs; readonly preflight needs no rescan."""
    from e2e import conftest  # pylint: disable=import-outside-toplevel
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    request = SimpleNamespace(session=SimpleNamespace(
        config=SimpleNamespace(sandbox_retry_recorder=recorder)))
    lifecycle = create_autospec(SandboxLifecycle, instance=True, spec_set=True)
    # run_id is an instance attribute, not part of the class autospec.
    lifecycle = SimpleNamespace(
        cleanup=lifecycle.cleanup, remove_orphans=lifecycle.remove_orphans, run_id='a' * 32)
    transport = SandboxFailure('fixed', api_facts=[
        {'type': 'RequestError', 'grpc_status': 'DEADLINE_EXCEEDED'}])
    if failure in ('preflight', 'scan'):
        lifecycle.remove_orphans.side_effect = (
            [transport] if failure == 'preflight' else [None, transport])
    if failure == 'cleanup':
        lifecycle.cleanup.side_effect = SandboxFailure('cleanup failed')
    monkeypatch.setenv('E2E_NAMESPACE', 'local')
    monkeypatch.setenv('E2E_RUN_ID', 'a' * 32)
    monkeypatch.setattr(conftest, 'sandbox_client', lambda: nullcontext(
        SimpleNamespace(sandbox=object(), users=object())))
    monkeypatch.setattr(conftest, 'SandboxLifecycle', lambda *_: lifecycle)
    monkeypatch.setattr(conftest, 'SandboxSession', lambda *_: 'session')
    fixture = conftest.fixture_sandbox.__wrapped__(request)
    if failure == 'preflight':
        with pytest.raises(SandboxFailure):
            next(fixture)
    else:
        assert next(fixture) == 'session'
        expected = StopIteration if failure == 'none' else SandboxFailure
        with pytest.raises(expected):
            if failure == 'unknown-open':
                fixture.throw(transport)
            else:
                next(fixture)
    lifecycle.cleanup.assert_called_once_with()
    scans = [call()] if failure in ('preflight', 'cleanup') else [
        call(), call(run_id='a' * 32)]
    assert lifecycle.remove_orphans.call_args_list == scans
    assert recorder.session_cleanup == (failure in ('none', 'preflight', 'unknown-open'))
