"""Offline stale provenance, mixed outcomes and trusted protocol migration."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import inspect
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import create_autospec
from test.test_sandbox_retry import NODE, OTHER, attempt, store

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError
from t_tech.invest.services import MarketDataService

from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure, list_exception_facts, safe_call
from scripts import sandbox_github, sandbox_retry, sandbox_retry_evidence as evidence
from scripts import sandbox_retry_report as reporting

SHA = 'a' * 40
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def unrelated_validation():
    """An unrelated ValueError never becomes a book timestamp rejection."""
    raise ValueError('private')


class SecurityGuard:  # pylint: disable=too-few-public-methods
    """Future boundary stand-in: supply frame inputs without porting application code."""

    def __init__(self, market) -> None:
        self.market = market
        self.reject_metadata = False

    def get_security_quote(self, uid):
        """Deliberately fail so the classifier, rather than a business guard, is tested."""
        # The named locals model the future validation boundary, not pricing behavior.
        if self.reject_metadata:
            raise ValueError('private')
        book = self.market.get_order_book(instrument_id=uid, depth=1)
        now = NOW
        if not book.bids:
            unrelated_validation()
        raise ValueError('private', now)


class FxGuard:  # pylint: disable=too-few-public-methods
    """Frame-only future FX boundary; no currency valuation or trading implementation."""

    def __init__(self) -> None:
        self.pair = SimpleNamespace(uid='uid')
        self.market_time = NOW - timedelta(seconds=181)
        self.received_time = NOW

    def validate_fresh(self, now):
        """Provide the real method inputs and a terminal validation raise."""
        if not isinstance(now, datetime):
            unrelated_validation()
        raise ValueError('private')


@pytest.fixture(autouse=True)
def future_validation_modules(monkeypatch):
    """Optional future validators are already loaded, never imported by the classifier."""
    for name, owner, guard in (
            ('autorepeater.tinvest_security_data', 'TInvestSecurityData', SecurityGuard),
            ('autorepeater.fx_data', 'FxQuote', FxGuard)):
        module = ModuleType(name)
        setattr(module, owner, guard)
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture(name='reads')
def fixture_reads():
    """Autospecced SDK read port, with no real client or unavailable application imports."""
    market = create_autospec(inspect.unwrap(MarketDataService), instance=True, spec_set=True)
    market.get_order_book.return_value = SimpleNamespace(
        instrument_uid='uid', orderbook_ts=NOW, bids=[1])
    return SimpleNamespace(market_data=market), SecurityGuard(market)


def stale_fact(age=181):
    """The verifier recomputes stale age rather than trusting a retry verdict."""
    return {'reason': 'market_time_stale', 'expected_uid': 'uid', 'received_uid': 'uid',
            'market_time': (NOW - timedelta(seconds=age)).isoformat(),
            'observed_utc': NOW.isoformat(), 'freshness_limit_seconds': 180}


def v3_attempt(nodes=(NODE,), failed=(NODE,)):
    """Original typed facts for an actual stale validation failure."""
    report = replace(attempt(nodes, failed), sha=SHA)
    return replace(report, phases=tuple(
        replace(phase, transport_chain=('ValueError',), grpc_status=None,
                stale_book=stale_fact()) if phase.outcome == 'failed' else phase
        for phase in report.phases))


def store_v3(root, number, report):
    """Retain originals and finalizer markers using the existing evidence fixtures."""
    store(root, number, report)
    (root / 'attempts' / f'{number:02}' / 'report.json').write_text(json.dumps(asdict(report)))


def trusted_master_verifier(monkeypatch):
    """Load immutable verifier code, independent of the tested producer and current module."""
    source = subprocess.run(['git', 'show', '0492181:scripts/sandbox_retry_evidence.py'],
                            capture_output=True, text=True, check=True).stdout
    legacy = ModuleType('trusted_retry_bootstrap')
    monkeypatch.setitem(sys.modules, 'trusted_retry_bootstrap', legacy)
    exec(compile(source, '<trusted 0492181>', 'exec'), legacy.__dict__)  # pylint: disable=exec-used
    return legacy


@pytest.mark.parametrize('age,uid,eligible', [
    (181, 'uid', True), (180.000001, 'uid', True), (180, 'uid', False),
    (128.014157, 'uid', False), (120.000001, 'uid', False), (120, 'uid', False),
    (90.000001, 'uid', False), (90, 'uid', False), (-1, 'uid', False),
    (181, 'other', False), (None, 'uid', False), ('naive', 'uid', False),
])
@pytest.mark.parametrize('wrapped', [False, True])
def test_security_stale_requires_actual_validation_and_identity(
        reads, age, uid, eligible, wrapped):
    """A known adapter exception and exact raw timestamp facts are both required."""
    client, data = reads
    book = client.market_data.get_order_book.return_value
    book.instrument_uid = uid
    book.orderbook_ts = (None if age is None else NOW.replace(tzinfo=None) if age == 'naive'
                         else NOW - timedelta(seconds=age))
    try:
        if wrapped:
            safe_call(data.get_security_quote, uid='uid')
        else:
            data.get_security_quote('uid')
    except (ValueError, SandboxFailure) as error:
        fact = reporting.stale_book_facts(error)
    else:
        fact = None
    assert fact == (stale_fact(age) if eligible else None)
    client.market_data.get_order_book.assert_called_once_with(instrument_id='uid', depth=1)


@pytest.mark.parametrize('boundary', ['security', 'fx'])
@pytest.mark.parametrize('wrapper', ['known', 'unknown', 'auth'])
def test_safe_call_stale_phase_preserves_retry_and_denials(
        reads, tmp_path, monkeypatch, boundary, wrapper):
    """Only the known guard through safe_call grants retry, never an opaque or auth error."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v3')
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=181)

    def query():
        try:
            if boundary == 'security':
                data.get_security_quote('uid')
            else:
                FxGuard().validate_fresh(NOW)
        except ValueError as error:
            if wrapper == 'unknown':
                raise RuntimeError('private') from error
            if wrapper == 'auth':
                raise RequestError(StatusCode.UNAUTHENTICATED, 'private', {}) from error
            raise

    with pytest.raises(SandboxFailure) as caught:
        safe_call(query)
    assert caught.value.__suppress_context__
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.phase(NODE, 'call', 'failed', caught.value)
    assert len(recorder.phases) == 1
    phase = recorder.phases[0]
    assert phase.stale_book == (stale_fact() if wrapper == 'known' else None)
    assert phase.auth_failure == (wrapper == 'auth')
    first = v3_attempt()
    first = replace(first, phases=(first.phases[0], phase, first.phases[2]))
    assert evidence.retry_nodes(first, protocol='retry-v3') == (
        (NODE,) if wrapper == 'known' else ())
    assert 'private' not in json.dumps(asdict(phase))


@pytest.mark.parametrize('wrappers', [1, 7, 8, 9])
def test_safe_call_stale_context_keeps_the_chain_bound(reads, wrappers):
    """The setup wrapper does not extend the existing eight-exception inspection budget."""
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=181)
    with pytest.raises(ValueError) as caught:
        data.get_security_quote('uid')
    error = caught.value

    def query(error):
        raise error

    for _ in range(wrappers):
        with pytest.raises(SandboxFailure) as caught:
            safe_call(query, error=error)
        error = caught.value
    assert reporting.stale_book_facts(error) == (stale_fact() if wrappers < 8 else None)


def test_safe_call_does_not_hide_incomplete_auth_inspection(reads, tmp_path, monkeypatch):
    """A real stale guard cannot grant retry when its wrapped cause chain is cyclic."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v3')
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=181)
    with pytest.raises(ValueError) as caught:
        data.get_security_quote('uid')
    original = caught.value
    original.__cause__ = original

    def query():
        raise original

    with pytest.raises(SandboxFailure) as wrapped:
        safe_call(query)
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.phase(NODE, 'call', 'failed', wrapped.value)
    assert recorder.phases[0].auth_failure
    assert recorder.phases[0].stale_book is None


@pytest.mark.parametrize('wrapper', [None, DataAccessError, AssertionError, ValueError])
def test_stale_provenance_does_not_authorize_arbitrary_wrappers(reads, wrapper):
    """Only existing transport wrappers may carry a genuine validation exception."""
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=181)
    with pytest.raises(ValueError) as caught:
        data.get_security_quote('uid')
    error = caught.value
    if wrapper is not None:
        error = wrapper('private')
        error.__cause__ = caught.value
    assert (reporting.stale_book_facts(error) is not None) == (wrapper in (None, DataAccessError))
    assert reporting.stale_book_facts(ValueError('stale FX order book')) is None


def test_fx_stale_uses_actual_validation_clock():
    """The original FX validation traceback contains the stale observation itself."""
    quote = FxGuard()
    with pytest.raises(ValueError) as caught:
        quote.validate_fresh(NOW)
    assert reporting.stale_book_facts(caught.value) == stale_fact()


@pytest.mark.parametrize('now', [NOW - timedelta(seconds=1), None, 'private'])
def test_fx_invalid_receipt_clock_cannot_be_stale(now):
    """A stale market observation never authorizes an invalid or future receipt clock."""
    quote = FxGuard()
    with pytest.raises(ValueError) as caught:
        quote.validate_fresh(now)
    assert reporting.stale_book_facts(caught.value) is None


def test_incomplete_or_cyclic_error_chains_cannot_grant_stale_retry():
    """The classifier stops at hidden, missing or cyclic provenance without retaining it."""
    error = DataAccessError('private')
    assert reporting.stale_book_facts(error) is None
    error.__cause__ = error
    assert reporting.stale_book_facts(error) is None
    error.__cause__ = None
    error.__suppress_context__ = True
    error.__context__ = ValueError('private')
    assert reporting.stale_book_facts(error) is None


@pytest.mark.parametrize('kind', ['metadata', 'price', 'timestamp'])
def test_other_adapter_value_errors_remain_permanent(reads, kind):
    """A known method can fail before book locals exist or in unrelated price guards."""
    client, data = reads
    if kind == 'metadata':
        data.reject_metadata = True
    elif kind == 'price':
        client.market_data.get_order_book.return_value.bids = []
    else:
        client.market_data.get_order_book.return_value.orderbook_ts = 'private'
    with pytest.raises(ValueError) as caught:
        data.get_security_quote('uid')
    assert reporting.stale_book_facts(caught.value) is None


@pytest.mark.parametrize('status', [StatusCode.UNAUTHENTICATED, StatusCode.PERMISSION_DENIED])
@pytest.mark.parametrize('stored', [False, True])
def test_auth_failure_stops_all_eligible_cases(tmp_path, status, stored):
    """Direct SDK and safe_call-stored auth failures both forbid mixed retries."""
    error = (SandboxFailure('fixed', api_facts=[
        {'type': 'RequestError', 'grpc_status': status.name}]) if stored else
        RequestError(status, 'private', {'token': 'private'}))
    assert reporting.auth_failure(error)
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.phase(OTHER, 'call', 'failed', error)
    first = v3_attempt((NODE, OTHER), (NODE, OTHER))
    first = replace(first, phases=tuple(
        recorder.phases[0] if phase.nodeid == OTHER and phase.outcome == 'failed' else phase
        for phase in first.phases))
    store_v3(tmp_path, 1, first)
    assert not evidence.retry_nodes(first, protocol='retry-v3')
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    with pytest.raises(evidence.RetryFailure, match='illegal retry'):
        evidence.inspect_history(tmp_path, expected=evidence.SuiteCollection(SHA, (NODE, OTHER)),
                                 protocol='retry-v3')


@pytest.mark.parametrize('status', [StatusCode.UNAUTHENTICATED, StatusCode.PERMISSION_DENIED])
@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('link', ['__cause__', '__context__'])
@pytest.mark.parametrize('wrappers', [7, 8, 9])
def test_auth_chain_boundary_stops_mixed_retries(tmp_path, status, stored, link, wrappers):
    """A truncated raw or sanitized auth chain cannot authorize another live process."""
    error = RequestError(status, 'private', {'token': 'private'})
    for _ in range(wrappers):
        wrapper = DataAccessError('private')
        setattr(wrapper, link, error)
        error = wrapper
    if stored:
        error = SandboxFailure('fixed', api_facts=list_exception_facts(error))
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.phase(OTHER, 'call', 'failed', error)
    first = v3_attempt((NODE, OTHER), (NODE, OTHER))
    first = replace(first, phases=tuple(
        recorder.phases[0] if phase.nodeid == OTHER and phase.outcome == 'failed' else phase
        for phase in first.phases))
    store_v3(tmp_path, 1, first)
    report = evidence.verify_attempt(tmp_path / 'attempts' / '01', 1, protocol='retry-v3')
    assert next(phase for phase in report.phases if phase.nodeid == OTHER
                and phase.outcome == 'failed').auth_failure
    assert not evidence.retry_nodes(report, protocol='retry-v3')
    assert 'private' not in (tmp_path / 'attempts' / '01' / 'report.json').read_text()
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    with pytest.raises(evidence.RetryFailure, match='illegal retry'):
        evidence.inspect_history(tmp_path, expected=evidence.SuiteCollection(SHA, (NODE, OTHER)),
                                 protocol='retry-v3')


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
    assert reporting.auth_failure(error) == (count >= (8 if stored else 9))


def test_cyclic_auth_inspection_stops_all_retries():
    """A repeated exception cannot prove that auth inspection reached the end."""
    error = DataAccessError('private')
    error.__cause__ = error
    assert reporting.auth_failure(error)


def test_stored_auth_inspection_is_bounded():
    """An uninspected stored suffix is a stop condition, never an unbounded scan."""
    unread = create_autospec(dict, instance=True, spec_set=True)
    unread.get.side_effect = AssertionError('stored inspection limit exceeded')
    error = SandboxFailure('fixed', api_facts=[{'type': 'DataAccessError'}] * 8 + [unread])
    assert reporting.auth_failure(error)
    unread.get.assert_not_called()


@pytest.mark.parametrize('protocol', ['retry-v1', 'retry-v2', 'retry-v3'])
def test_recorder_roundtrip_and_bootstrap_schema(reads, tmp_path, monkeypatch, protocol):
    """The new producer preserves exact old phase fields unless v3 is requested."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', protocol)
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=181)
    with pytest.raises(ValueError) as caught:
        data.get_security_quote('uid')
    recorder = reporting.AttemptRecorder(tmp_path / 'report.json')
    recorder.phase(NODE, 'call', 'failed', caught.value)
    recorder.finish(1)
    report = evidence.read_report(recorder.path, protocol=protocol)
    assert report.phases[0].stale_book == (stale_fact() if protocol == 'retry-v3' else None)
    assert report.phases[0].transport_chain == (('ValueError',) if protocol == 'retry-v3'
                                                else ('other',))
    fields = json.loads(recorder.path.read_text())['phases'][0]
    assert ('auth_failure' in fields) == ('stale_book' in fields) == (protocol == 'retry-v3')
    assert 'private' not in recorder.path.read_text()


@pytest.mark.parametrize('fact_updates,phase_updates', [
    pytest.param({'market_time': (NOW + timedelta(seconds=1)).isoformat()}, {}, id='future'),
    pytest.param({'market_time': NOW.isoformat()}, {}, id='fresh'),
    pytest.param({'market_time': None}, {}, id='missing'),
    pytest.param({'market_time': NOW.replace(tzinfo=None).isoformat()}, {}, id='naive'),
    pytest.param({'received_uid': 'other'}, {}, id='uid'),
    pytest.param({'reason': 'market_time_future'}, {}, id='reason'),
    pytest.param({'freshness_limit_seconds': 60}, {}, id='limit'),
    pytest.param({'freshness_limit_seconds': 150}, {}, id='unapproved-limit'),
    pytest.param({'freshness_limit_seconds': True}, {}, id='bool-limit'),
    pytest.param({'private': 'private'}, {}, id='extra'),
    pytest.param({}, {'transport_chain': ['other', 'ValueError']}, id='assertion'),
    pytest.param({}, {'when': 'teardown'}, id='teardown'),
    pytest.param({}, {'auth_failure': True}, id='auth'),
    pytest.param({}, {'auth_failure': 1}, id='bool-auth'),
    pytest.param({}, {'outcome': 'passed'}, id='passed'),
    pytest.param({}, {'grpc_status': 'UNAVAILABLE'}, id='transport'),
    pytest.param({}, {'transport_chain': []}, id='empty-chain'),
    pytest.param({'expected_uid': 'private credential', 'received_uid': 'private credential'},
                 {}, id='bad-uid'),
])
def test_v3_rejects_unsupported_or_invalid_stale_facts(fact_updates, phase_updates):
    """Producer verdicts cannot replace evidence of the exact stale guard."""
    fields = asdict(v3_attempt().phases[1])
    fields['transport_chain'] = list(fields['transport_chain'])
    fields['stale_book'].update(fact_updates)
    fields.update(phase_updates)
    with pytest.raises(evidence.RetryFailure):
        evidence.phase_fact(fields, protocol='retry-v3')


@pytest.mark.parametrize('fault', ['cleanup', 'collection', 'teardown', 'canceled', 'sha', 'skip'])
def test_v3_global_safety_gates_cannot_allow_retry(tmp_path, fault):
    """Valid stale evidence cannot override any original lifecycle safety failure."""
    report = v3_attempt()
    if fault == 'cleanup':
        report = replace(report, session_cleanup=False)
    elif fault == 'collection':
        report = replace(report, collection_errors=1)
    elif fault == 'canceled':
        report = replace(report, exit_status=143)
    elif fault == 'sha':
        report = replace(report, sha='b' * 40)
    else:
        report = replace(report, phases=(*report.phases[:-1], replace(
            report.phases[-1], outcome='skipped' if fault == 'skip' else 'failed')))
    store_v3(tmp_path, 1, report)
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    with pytest.raises(evidence.RetryFailure):
        evidence.inspect_history(tmp_path, expected=evidence.SuiteCollection(SHA, (NODE,)),
                                 protocol='retry-v3')


@pytest.mark.parametrize('count', [4, 5])
def test_v3_all_25_scenarios_and_attempt_limit(tmp_path, count):
    """The full 25-case collection and exactly three additional attempts remain mandatory."""
    nodes = tuple(f'e2e/test_sandbox.py::test_case_{index}' for index in range(25))
    for number in range(1, count + 1):
        selected = nodes if number == 1 else nodes[:1]
        report = v3_attempt(selected, failed=nodes[:1] if number < count else ())
        store_v3(tmp_path, number, report)
    expected = evidence.SuiteCollection(SHA, nodes)
    if count == 5:
        with pytest.raises(evidence.RetryFailure, match='attempt count'):
            evidence.inspect_history(tmp_path, expected=expected, protocol='retry-v3')
    else:
        states = evidence.inspect_history(tmp_path, expected=expected, protocol='retry-v3')
        evidence.write_final_junit(states, tmp_path / 'junit.xml')
        assert evidence.verify_history(tmp_path, expected=expected, protocol='retry-v3') == 25


def test_v3_requires_explicit_protocol_and_collection(tmp_path):
    """New evidence never falls back to the frozen legacy protocol or a subset."""
    with pytest.raises(evidence.RetryFailure, match='collection'):
        evidence.inspect_history(tmp_path, protocol='retry-v3')
    with pytest.raises(evidence.RetryFailure, match='protocol'):
        evidence.phase_fact({}, protocol='unknown')
    with pytest.raises(evidence.RetryFailure, match='protocol'):
        evidence.inspect_history(tmp_path, protocol='')


def test_unvalidated_stale_dataclass_cannot_gain_retry_permission():
    """Even direct eligibility calls recheck timestamps rather than trusting the reason."""
    report = v3_attempt()
    phase = replace(report.phases[1], stale_book={**stale_fact(), 'market_time': NOW.isoformat()})
    report = replace(report, phases=(report.phases[0], phase, report.phases[2]))
    assert not evidence.retry_nodes(report, protocol='retry-v3')


@pytest.mark.parametrize('limit', [90, 120, 180])
def test_v3_verifier_preserves_archived_limit_and_rechecks_exact_boundary(tmp_path, limit):
    """Archived 90s/120s evidence and current 180s evidence keep their strict clocks."""
    fact = {**stale_fact(limit), 'freshness_limit_seconds': limit}
    with pytest.raises(evidence.RetryFailure, match='timestamps'):
        evidence.validate_stale_book(fact)
    fact['market_time'] = (NOW - timedelta(seconds=limit, microseconds=1)).isoformat()
    first = v3_attempt()
    first = replace(first, phases=tuple(
        replace(phase, stale_book=fact) if phase.outcome == 'failed' else phase
        for phase in first.phases))
    store_v3(tmp_path, 1, first)
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    expected = evidence.SuiteCollection(SHA, (NODE,))
    states = evidence.inspect_history(tmp_path, expected=expected, protocol='retry-v3')
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, expected=expected, protocol='retry-v3') == 1


@pytest.mark.parametrize('category', ['stale', 'UNAVAILABLE', 'DEADLINE_EXCEEDED'])
def test_v3_mixed_failure_retries_eligible_and_preserves_permanent(tmp_path, category):
    """Successful retry changes only that scenario and never erases permanent failure."""
    first = v3_attempt((NODE, OTHER), (NODE, OTHER))
    first = replace(first, phases=tuple(
        replace(phase, stale_book=None, transport_chain=('other',))
        if phase.nodeid == OTHER and phase.outcome == 'failed' else phase
        for phase in first.phases))
    if category != 'stale':
        first = replace(first, phases=tuple(
            replace(phase, stale_book=None, transport_chain=('RequestError',), grpc_status=category)
            if phase.nodeid == NODE and phase.outcome == 'failed' else phase
            for phase in first.phases))
    assert evidence.retry_nodes(first, protocol='retry-v3') == (NODE,)
    assert not evidence.retry_nodes(first)
    store_v3(tmp_path, 1, first)
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    expected = evidence.SuiteCollection(SHA, (NODE, OTHER))
    states = evidence.inspect_history(tmp_path, expected=expected, protocol='retry-v3')
    assert states == {NODE: 'passed', OTHER: 'failed'}
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    with pytest.raises(evidence.RetryFailure, match='remain failed'):
        evidence.verify_history(tmp_path, expected=expected, protocol='retry-v3')


def test_v3_controller_starts_fresh_eligible_subset_with_common_deadline(tmp_path, monkeypatch):
    """New processes retain the common budget and omit permanently failed scenarios."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v3')
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    (tmp_path / 'collection.json').write_text(json.dumps(
        {'version': 1, 'sha': SHA, 'nodeids': [NODE, OTHER]}))
    first = v3_attempt((NODE, OTHER), (NODE, OTHER))
    first = replace(first, phases=tuple(
        replace(phase, stale_book=None, transport_chain=('other',))
        if phase.nodeid == OTHER and phase.outcome == 'failed' else phase
        for phase in first.phases))
    reports = [first, v3_attempt(failed=())]

    def child(command, *, env, timeout, check):
        number = 1 if 'e2e/' in command else 2
        assert env['E2E_ATTEMPT_REPORT'].endswith(f'{number:02}/report.json')
        assert timeout == (100 if number == 1 else 90) and check is False
        if number == 2:
            assert NODE in command and OTHER not in command
        (tmp_path / 'attempts' / f'{number:02}').rmdir()
        store_v3(tmp_path, number, reports[number - 1])
        return subprocess.CompletedProcess(command, reports[number - 1].exit_status)

    runner = create_autospec(subprocess.run, spec_set=True, side_effect=child)
    clock = create_autospec(sandbox_retry.time.monotonic, spec_set=True,
                            side_effect=[0, 0, 0, 10, 10])
    sleep = create_autospec(sandbox_retry.time.sleep, spec_set=True)
    assert sandbox_retry.run_attempts(['e2e/'], tmp_path, 100,
                                      runtime=sandbox_retry.RetryRuntime(runner, clock, sleep)) == 1
    assert runner.call_count == 2
    sleep.assert_called_once_with(10)


def test_trusted_master_requires_sequenced_v3_bootstrap(tmp_path, monkeypatch):
    """Unchanged trusted master cannot accept new classifications or mixed retries."""
    legacy = trusted_master_verifier(monkeypatch)
    store_v3(tmp_path, 1, v3_attempt())
    store_v3(tmp_path, 2, v3_attempt(failed=()))
    expected = evidence.SuiteCollection(SHA, (NODE,))
    (tmp_path / 'collection.json').write_text(json.dumps(
        {'version': 1, 'sha': SHA, 'nodeids': [NODE]}))
    states = evidence.inspect_history(tmp_path, expected=expected, protocol='retry-v3')
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, expected=expected, protocol='retry-v3') == 1
    assert sandbox_github.evidence_result(
        tmp_path / 'junit.xml', None, 'retry-v3',
        collection=tmp_path / 'collection.json', sha=SHA).passed == 1
    with pytest.raises(getattr(legacy, 'RetryFailure')):
        getattr(legacy, 'verify_history')(
            tmp_path, expected=getattr(legacy, 'SuiteCollection')(SHA, (NODE,)))
    with pytest.raises(sandbox_github.GitHubFailure):
        sandbox_github.evidence_result(tmp_path / 'junit.xml', None, 'retry-v2',
                                       collection=tmp_path / 'collection.json', sha=SHA)


def test_current_v2_producer_is_accepted_by_unchanged_trusted_master(tmp_path, monkeypatch):
    """The infrastructure bootstrap remains verifiable before v3 activation."""
    legacy = trusted_master_verifier(monkeypatch)
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v2')
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    directory = tmp_path / 'attempts' / '01'
    recorder = reporting.AttemptRecorder(directory / 'report.json')
    recorder.selected = (NODE,)
    recorder.entered = recorder.cleaned = [NODE]
    recorder.session_cleanup = True
    for when in ('setup', 'call', 'teardown'):
        recorder.phase(NODE, when, 'passed', None)
    recorder.finish(0)
    (tmp_path / 'retry.json').write_text(json.dumps(
        {'version': 1, 'attempts': [{'number': 1, 'returncode': 0}]}))
    for path in (directory / 'junit.xml', tmp_path / 'junit.xml'):
        evidence.write_final_junit({NODE: 'passed'}, path)
    assert getattr(legacy, 'verify_history')(
        tmp_path, expected=getattr(legacy, 'SuiteCollection')(SHA, (NODE,))) == 1
