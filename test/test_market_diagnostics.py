"""Failure diagnostics preserve guards, bounded facts and the SDK call surface."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, call, create_autospec

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import InstrumentsService, MarketDataService

from autorepeater.strategy_data import InstrumentInfo, InstrumentMatch, InstrumentType, StrategyData
from autorepeater.tinvest_security_data import TInvestSecurityData
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from e2e import conftest as live_hooks
from e2e.test_sandbox_multicurrency import _unique
from scripts.sandbox_lifecycle import SandboxFailure

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
SECRET = 'Bearer simulated-credential-DO-NOT-LOG'
INFO = InstrumentInfo('uid', 'RU000A105104', SECRET, InstrumentType.BOND, 'TQCB', 1, 'cny', True)


@pytest.fixture(name='selection')
def fixture_selection():
    """Only the read port exists; unexpected I/O is an autospec failure."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = []
    return SimpleNamespace(data=data, scenario='INDEX-CROSS')


def summary(capsys):
    """One compact safe stdout record, without changing evidence schemas."""
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('Sandbox diagnostic: ')
    assert SECRET not in lines[0]
    return json.loads(lines[0].removeprefix('Sandbox diagnostic: '))


@pytest.mark.parametrize('count,reason', [(0, 'no_candidates'), (2, 'ambiguous')])
def test_selection_distinguishes_empty_and_ambiguous(selection, capsys, count, reason):
    """Zero raw hits is distinguishable from several eligible instruments."""
    selection.data.find_instruments.return_value = [
        InstrumentMatch(f'uid-{i}', INFO.ticker, SECRET, INFO.instrument_type, INFO.class_code)
        for i in range(count)]
    selection.data.get_instrument.side_effect = [
        replace(INFO, uid=f'uid-{i}') for i in range(count)]
    with pytest.raises(SandboxFailure, match='requires unique available instrument'):
        _unique(selection, INFO.ticker)
    fact = summary(capsys)
    assert (fact['raw_count'], fact['eligible_count'], fact['reason']) == (count, count, reason)
    assert fact['query'] == INFO.ticker
    assert fact['stage'] == 'catalog' and fact['phase'] == 'instrument_selection'
    assert fact['scenario'] == selection.scenario
    assert [item['reason'] for item in fact['candidates']] == ['eligible'] * count
    assert selection.data.method_calls == [call.find_instruments(INFO.ticker)] + [
        call.get_instrument(f'uid-{i}') for i in range(count)]


def test_selection_reports_actual_rejections_and_unloaded_metadata(selection, capsys):
    """Ticker filters do not load metadata, and missing flags never become False."""
    selection.data.find_instruments.return_value = [
        InstrumentMatch('wrong', 'OTHER', SECRET, InstrumentType.SHARE, 'TQBR'),
        InstrumentMatch('closed', INFO.ticker, SECRET, InstrumentType.BOND, 'TQCB'),
        InstrumentMatch('missing', INFO.ticker, SECRET, InstrumentType.BOND, 'TQCB')]
    selection.data.get_instrument.side_effect = [
        replace(INFO, uid='closed', api_trade_available=False),
        replace(INFO, uid='missing', api_trade_available=None)]
    with pytest.raises(SandboxFailure):
        _unique(selection, INFO.ticker, ticker=INFO.ticker)
    fact = summary(capsys)
    assert fact['reason'] == 'no_eligible_candidates' and fact['eligible_count'] == 0
    assert [item['reason'] for item in fact['candidates']] == [
        'ticker_mismatch', 'api_unavailable', 'api_availability_missing']
    assert fact['candidates'][0]['kind'] == 'share'
    assert fact['candidates'][0]['class_code'] == 'TQBR'
    assert fact['candidates'][0]['currency'] == 'not_loaded'
    assert fact['candidates'][1]['api_available'] is False
    assert fact['candidates'][2]['api_available'] == 'missing'
    assert selection.data.get_instrument.call_args_list == [call('closed'), call('missing')]


def test_metadata_error_keeps_original_exception_and_loaded_facts(selection, capsys):
    """Do not log external exception text or retry a failed metadata read."""
    selection.data.find_instruments.return_value = [
        InstrumentMatch('uid', INFO.ticker, SECRET, InstrumentType.BOND, 'TQCB')]
    error = ValueError(SECRET)
    selection.data.get_instrument.side_effect = error
    with pytest.raises(ValueError) as caught:
        _unique(selection, INFO.ticker)
    assert caught.value is error
    fact = summary(capsys)
    assert fact['reason'] == 'metadata_read_failed'
    assert fact['eligible_count'] == 'incomplete'
    assert fact['candidates'][0]['reason'] == 'metadata_read_failed'
    selection.data.get_instrument.assert_called_once_with('uid')


def test_selection_summary_is_bounded_and_never_uses_object_repr(selection, capsys):
    """Unexpected payload fields, malformed scalars and names stay outside output."""
    bad = SimpleNamespace(uid={'credential': SECRET}, ticker=SECRET, name=SECRET,
                          instrument_type={'credential': SECRET}, class_code='x' * 1000,
                          api_trade_available='false', currency=None, credentials=SECRET)
    selection.data.find_instruments.return_value = [bad] * 100
    selection.data.get_instrument.return_value = bad
    with pytest.raises(SandboxFailure):
        _unique(selection, 'RU000A105104')
    fact = summary(capsys)
    assert fact['raw_count'] == 100 and fact['omitted_count'] == 80
    assert len(fact['candidates']) == 20
    assert fact['candidates'][0] == {
        'uid': 'invalid', 'ticker': 'invalid', 'class_code': 'invalid', 'kind': 'invalid',
        'currency': 'missing', 'api_available': 'invalid', 'reason': 'api_availability_invalid'}
    assert len(json.dumps(fact)) < 6000


def test_successful_selection_emits_no_snapshots(selection, capsys):
    """The original selected object and call count stay unchanged."""
    selection.data.find_instruments.return_value = [
        InstrumentMatch('uid', INFO.ticker, SECRET, InstrumentType.BOND, 'TQCB')]
    selection.data.get_instrument.return_value = INFO
    assert _unique(selection, INFO.ticker) is INFO
    assert capsys.readouterr().out == ''


@pytest.fixture(name='books')
def fixture_books():
    """Use actual SDK objects and autospecced unwrapped service methods."""
    client = Mock(spec_set=['instruments', 'market_data'])
    client.instruments = create_autospec(inspect.unwrap(InstrumentsService),
                                         instance=True, spec_set=True)
    client.market_data = create_autospec(inspect.unwrap(MarketDataService),
                                         instance=True, spec_set=True)
    client.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', currency='rub', instrument_type='share', name=SECRET))
    client.market_data.get_order_book.return_value = sdk.GetOrderBookResponse(
        instrument_uid='uid', orderbook_ts=NOW,
        bids=[sdk.Order(price=sdk.Quotation(units=1, nano=0), quantity=1)],
        asks=[sdk.Order(price=sdk.Quotation(units=2, nano=0), quantity=1)])
    return client, TInvestSecurityData(client, clock=lambda: NOW)


@pytest.mark.parametrize('case', [
    ('other', NOW, 'uid_mismatch', 0, 'security book UID mismatch or stale market time'),
    ('uid', NOW - timedelta(seconds=180, microseconds=1), 'market_time_stale', 180.000001,
     'security book UID mismatch or stale market time'),
    ('uid', NOW + timedelta(seconds=2), 'market_time_future', -2,
     'security book UID mismatch or stale market time'),
    ('uid', NOW + timedelta(seconds=1, microseconds=1), 'market_time_future', -1.000001,
     'security book UID mismatch or stale market time'),
    ('uid', None, 'market_time_missing', 'missing', 'FX timestamp must be timezone-aware'),
    ('uid', NOW.replace(tzinfo=None), 'market_time_naive', 'unavailable',
     'FX timestamp must be timezone-aware'),
    ('uid', {'credential': SECRET}, 'market_time_invalid', 'unavailable',
     'FX timestamp must be timezone-aware'),
])
def test_book_failures_are_distinct_without_changing_exceptions(books, caplog, case):
    """Warning is supplementary; previous validation messages remain intact."""
    client, data = books
    uid, stamp, reason, age, message = case
    book = client.market_data.get_order_book.return_value
    book.instrument_uid, book.orderbook_ts = uid, stamp
    with pytest.raises(ValueError, match=message):
        data.get_security_quote('uid')
    records = [record for record in caplog.records
               if record.message.startswith('Market diagnostic: ')]
    assert len(records) == 1
    fact = json.loads(records[0].message.removeprefix('Market diagnostic: '))
    assert reason in fact['reasons']
    assert fact['age_seconds'] == age and fact['freshness_limit_seconds'] == 180
    assert fact['expected_uid'] == 'uid' and fact['received_uid'] == uid
    assert fact['observed_utc'] == NOW.isoformat()
    assert SECRET not in caplog.text
    assert len(client.instruments.method_calls) == len(client.market_data.method_calls) == 1


@pytest.mark.parametrize('age', [-1, -0.578215, -0.1, -0.065426, 0, 60, 61, 90, 90.000001,
                                 120, 128.014157, 180])
def test_fresh_book_does_not_log_and_keeps_quote(books, caplog, age):
    """The old boundary and newly permitted ages remain accepted and quiet."""
    client, data = books
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=age)
    assert data.get_security_quote('uid').estimate_per_piece().amount == 1.5
    assert 'Market diagnostic:' not in caplog.text


@pytest.mark.parametrize('ahead', [timedelta(milliseconds=100), timedelta(seconds=1)])
def test_tolerated_market_skew_keeps_raw_diagnostic_age(books, caplog, ahead):
    """A UID failure preserves negative age without falsely classifying accepted skew."""
    client, data = books
    book = client.market_data.get_order_book.return_value
    book.instrument_uid = 'other'
    book.orderbook_ts = NOW + ahead
    with pytest.raises(ValueError, match='UID mismatch'):
        data.get_security_quote('uid')
    record, = [record for record in caplog.records
               if record.message.startswith('Market diagnostic: ')]
    fact = json.loads(record.message.removeprefix('Market diagnostic: '))
    assert fact['reasons'] == ['uid_mismatch']
    assert fact['age_seconds'] == fact['age_at_check_seconds'] == -ahead.total_seconds()
    assert fact['market_time'] == book.orderbook_ts.isoformat()
    assert fact['observed_utc'] == NOW.isoformat()


@pytest.mark.parametrize('flag,expected', [(None, 'missing'), ('false', 'invalid')])
def test_sdk_metadata_permission_failure_has_safe_facts(books, caplog, flag, expected):
    """The adapter can explain a missing marker before its original ValueError."""
    client, _ = books
    client.instruments.get_instrument_by.return_value.instrument.api_trade_available_flag = flag
    with pytest.raises(ValueError, match='api_trade_available_flag must be bool'):
        TInvestStrategyData(client).get_instrument('uid')
    assert f'"api_available": "{expected}"' in caplog.text
    assert SECRET not in caplog.text
    client.instruments.get_instrument_by.assert_called_once()
    client.market_data.get_order_book.assert_not_called()


def test_live_book_diagnostic_survives_safe_report_without_leaking(books, capsys):
    """Failure-only stdout survives -s even when captured sections are removed."""
    client, data = books
    book = client.market_data.get_order_book.return_value
    book.instrument_uid = {'credentials': SECRET}
    book.orderbook_ts = NOW - timedelta(seconds=181)
    book.credentials = SECRET
    item = SimpleNamespace(nodeid='e2e/test_sandbox.py::test_drift[INDEX-PAIR]')
    live_hooks.pytest_runtest_call(item)
    output = live_hooks.MarketDiagnosticOutput()
    logger = logging.getLogger('tinkoffBot')
    logger.addHandler(output)
    try:
        with pytest.raises(ValueError) as caught:
            data.get_security_quote('uid')
    finally:
        logger.removeHandler(output)
    text = capsys.readouterr().out
    assert SECRET not in text
    fact = json.loads(text.removeprefix('Market diagnostic: '))
    assert fact['scenario'] == item.nodeid and fact['phase'] == 'call'
    assert fact['stage'] == 'sandbox_e2e'
    assert fact['reasons'] == ['uid_mismatch', 'market_time_stale']
    assert fact['received_uid'] == 'invalid'
    report = SimpleNamespace(when='call', sections=[SECRET])
    hook = live_hooks.pytest_runtest_makereport(
        item, SimpleNamespace(excinfo=SimpleNamespace(value=caught.value)))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.sections == [] and SECRET not in json.dumps(report.__dict__)
    teardown = live_hooks.pytest_runtest_teardown(item)
    next(teardown)
    with pytest.raises(StopIteration):
        next(teardown)


def test_market_output_does_not_forward_unrelated_sdk_logs(capsys):
    """Names, credentials and arbitrary SDK text cannot use the diagnostic sink."""
    handler = live_hooks.MarketDiagnosticOutput()
    record = logging.LogRecord('tinkoffBot', logging.WARNING, 'sdk.py', 1, SECRET, (), None)
    handler.emit(record)
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('kind,currency,issues', [
    (InstrumentType.SHARE, 'cny', ['kind_mismatch']),
    (InstrumentType.BOND, 'rub', ['currency_mismatch']),
])
def test_selection_reports_known_metadata_expectations_without_changing_filter(
        selection, capsys, kind, currency, issues):
    """Later preflight owns type/currency guards; diagnostics must not invent a filter."""
    selection.data.find_instruments.return_value = [
        InstrumentMatch('uid', INFO.ticker, SECRET, kind, 'TQBR')]
    item = replace(INFO, instrument_type=kind, currency=currency, class_code='TQBR')
    selection.data.get_instrument.return_value = item
    assert _unique(selection, INFO.ticker, expected=(InstrumentType.BOND, 'cny')) is item
    fact = summary(capsys)
    assert fact['reason'] == 'metadata_expectation_mismatch'
    assert fact['requested_kind'] == 'bond' and fact['requested_currency'] == 'cny'
    assert fact['requested_class'] == 'unspecified'
    assert fact['candidates'][0]['class_code'] == 'TQBR'
    assert fact['candidates'][0]['metadata_issues'] == issues
