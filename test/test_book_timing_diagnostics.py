"""Controlled diagnostic clocks distinguish API staleness from local processing."""
from datetime import timedelta
import json
from unittest.mock import Mock
from test.test_market_diagnostics import fixture_books, NOW
from test.test_tinvest_fx_data import fixture_reads

import pytest

from autorepeater import market_diagnostics as diagnostics
from autorepeater.tinvest_requests import call_api


@pytest.fixture(name='books')
def fixture_timed_books():
    """The real SDK adapter still receives its original single validation clock."""
    return fixture_books.__wrapped__()


def book_failure(caplog):
    """Timing is attached to the original failure event, rather than a success stream."""
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    return next(fact for fact in facts if fact['event'] in (
        'security_book_rejected', 'fx_book_rejected'))


@pytest.mark.parametrize('case', [
    (61, 0, 61), (61, -2, 59), (119, -1, 118),
])
def test_security_failure_times_request_receipt_and_existing_check(
        books, caplog, monkeypatch, case):
    """UTC observations are diagnostic only; the business clock is called exactly once."""
    client, adapter = books
    market_age, receipt_offset, expected_receipt_age = case
    adapter._clock = Mock(return_value=NOW)  # pylint: disable=protected-access
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(
        seconds=market_age)
    receipt = NOW + timedelta(seconds=receipt_offset)
    utc = Mock(side_effect=[receipt - timedelta(seconds=2), receipt, NOW])
    monotonic = Mock(side_effect=[10., 12., 14.])
    monkeypatch.setattr(diagnostics, '_utc_now', utc)
    monkeypatch.setattr(diagnostics, 'monotonic', monotonic)
    with pytest.raises(ValueError, match='security book UID mismatch or stale market time'):
        adapter.get_security_quote('uid')
    fact = book_failure(caplog)
    timing = fact['request_timing']
    assert timing['request_start_monotonic'] == 10
    assert timing['response_monotonic'] == 12 and timing['checked_monotonic'] == 14
    assert timing['request_duration_seconds'] == 2
    assert timing['response_to_check_seconds'] == 2
    assert timing['response_utc'] == receipt.isoformat()
    assert timing['checked_utc'] == NOW.isoformat()
    assert fact['age_at_response_seconds'] == expected_receipt_age
    assert fact['age_at_check_seconds'] == fact['age_seconds'] == market_age
    assert fact['metadata']['currency'] == 'rub'
    adapter._clock.assert_called_once_with()  # pylint: disable=protected-access
    assert len(client.instruments.method_calls) == len(client.market_data.method_calls) == 1


def test_failure_uid_is_linked_to_loaded_ticker_class_currency(books, caplog):
    """No extra lookup is needed to label the failing full-portfolio security."""
    client, adapter = books
    item = client.instruments.get_instrument_by.return_value.instrument
    item.ticker, item.class_code, item.currency = 'FULL-SECURITY', 'T', 'cny'
    item.unexpected_credential = 'Bearer simulated-credential-DO-NOT-LOG'
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=61)
    with pytest.raises(ValueError):
        adapter.get_security_quote('uid')
    fact = book_failure(caplog)
    assert fact['expected_uid'] == fact['metadata']['uid'] == 'uid'
    assert fact['metadata']['ticker'] == 'FULL-SECURITY'
    assert fact['metadata']['class_code'] == 'T' and fact['metadata']['currency'] == 'cny'
    assert 'simulated-credential' not in caplog.text
    assert len(client.instruments.method_calls) == len(client.market_data.method_calls) == 1


def test_observational_future_receipt_does_not_affect_sixty_second_guard(
        books, caplog, monkeypatch):
    """A changed diagnostic wall clock cannot cause rejection of a fresh book."""
    client, adapter = books
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=60)
    monkeypatch.setattr(diagnostics, '_utc_now', lambda: NOW + timedelta(days=1))
    assert adapter.get_security_quote('uid').estimate_per_piece().amount > 0
    assert 'security_book_rejected' not in caplog.text


def test_fx_book_staleness_has_same_request_breakdown(caplog, monkeypatch):
    """FX and security observations share diagnostics, not their validation decisions."""
    client, adapter = fixture_reads.__wrapped__()
    pair = adapter.list_pairs()[0]
    now = adapter._clock()  # pylint: disable=protected-access
    client.market_data.get_order_book.return_value.orderbook_ts = now - timedelta(seconds=61)
    monkeypatch.setattr(diagnostics, '_utc_now', lambda: now)
    monkeypatch.setattr(diagnostics, 'monotonic', Mock(side_effect=[20., 22., 22.5]))
    with pytest.raises(ValueError, match='stale FX order book'):
        adapter.get_quote(pair)
    fact = book_failure(caplog)
    assert fact['expected_uid'] == pair.uid
    assert fact['age_at_response_seconds'] == fact['age_at_check_seconds'] == 61
    assert fact['request_timing']['request_duration_seconds'] == 2
    assert fact['request_timing']['response_to_check_seconds'] == .5


def test_stage_totals_are_bounded_by_operations_and_reset_per_scenario(monkeypatch):
    """Many reads aggregate into one operation counter; phases retain the same totals."""
    monkeypatch.setattr(diagnostics, 'monotonic', Mock(side_effect=[0., 1., 2., 5.]))
    diagnostics.set_diagnostic_context(stage='sandbox_e2e', scenario='one', phase='setup')
    try:
        with diagnostics.measure_stage('metadata.find'):
            pass
        diagnostics.set_diagnostic_context(stage='sandbox_e2e', scenario='one', phase='call')
        with diagnostics.measure_stage('metadata.find'):
            pass
        assert diagnostics.timing_totals() == {
            'metadata.find': {'count': 2, 'duration_seconds': 4.0}}
        diagnostics.set_diagnostic_context(stage='sandbox_e2e', scenario='two', phase='setup')
        assert diagnostics.timing_totals() == {}
    finally:
        diagnostics.set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')


def test_quota_attempts_and_waits_are_separate_stage_totals(books, monkeypatch, caplog):
    """Two quota retries preserve the method/arguments and two existing ten-second waits."""
    from grpc import StatusCode  # pylint: disable=import-outside-toplevel
    from t_tech.invest import RequestError  # pylint: disable=import-outside-toplevel
    client, _ = books
    method = client.instruments.get_instrument_by
    response = method.return_value
    method.side_effect = [RequestError(StatusCode.RESOURCE_EXHAUSTED, 'private', None),
                          RequestError(StatusCode.RESOURCE_EXHAUSTED, 'private', None), response]
    sleep = Mock()
    monkeypatch.setattr('autorepeater.tinvest_requests.time.sleep', sleep)
    monkeypatch.setattr(diagnostics, 'monotonic', Mock(side_effect=[
        0., 0., 1., 1., 11., 11., 13., 13., 23., 23., 26., 26.]))
    diagnostics.set_diagnostic_context(stage='sandbox_e2e', scenario='quota', phase='call')
    try:
        with diagnostics.measure_stage('metadata.instrument.request'):
            assert call_api(method, id='uid') is response
        assert diagnostics.timing_totals() == {
            'metadata.instrument.request': {'count': 1, 'duration_seconds': 26.},
            'metadata.instrument.request.attempt': {'count': 3, 'duration_seconds': 6.},
            'metadata.instrument.request.quota_wait': {'count': 2, 'duration_seconds': 20.}}
    finally:
        diagnostics.set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')
    assert method.call_count == 3
    assert all(attempt.kwargs == {'id': 'uid'} for attempt in method.call_args_list)
    assert sleep.call_count == 2 and all(wait.args == (10,) for wait in sleep.call_args_list)
    assert 'private' not in caplog.text


def test_no_e2e_stage_clocks_or_state_for_normal_metadata_reads(books, monkeypatch):
    """The stage counters are an E2E observation; normal request behavior is unchanged."""
    client, _ = books
    diagnostics.set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')
    clock = Mock(side_effect=AssertionError('unexpected diagnostic clock'))
    monkeypatch.setattr(diagnostics, 'monotonic', clock)
    with diagnostics.measure_stage('metadata.instrument.request'):
        assert call_api(client.instruments.get_instrument_by, id='uid') is (
            client.instruments.get_instrument_by.return_value)
    assert diagnostics.timing_totals() == {}
    clock.assert_not_called()


def test_book_metadata_missing_labels_stay_missing(books, caplog):
    """The expected UID is known even if its loaded SDK metadata lacks human labels."""
    client, adapter = books
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=61)
    with pytest.raises(ValueError):
        adapter.get_security_quote('uid')
    metadata = book_failure(caplog)['metadata']
    assert metadata['uid'] == 'uid'
    assert metadata['ticker'] == metadata['class_code'] == 'missing'
