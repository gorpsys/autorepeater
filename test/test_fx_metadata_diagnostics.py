"""FX catalog failures expose the actual field without changing broker reads."""
from dataclasses import replace
from decimal import Decimal
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, call
from test.test_sandbox_currency_harness import OfflineBroker
from test.test_tinvest_fx_data import fixture_reads, instrument, quotation

import pytest
from t_tech.invest import schemas as sdk

from autorepeater import tinvest_fx_data
from autorepeater.market_diagnostics import set_diagnostic_context
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from e2e import conftest as live_hooks
from e2e import test_sandbox_multicurrency as scenarios

SECRET = 'Bearer simulated-credential-DO-NOT-LOG'


@pytest.fixture(name='reads')
def fixture_diagnostic_reads():
    """Reuse the actual SDK/unwrapped autospec boundary, without a real client."""
    return fixture_reads.__wrapped__()


def rejection(caplog):
    """Only one failing pair is expanded, never the whole currency response."""
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    failures = [fact for fact in facts if fact['event'] == 'fx_metadata_rejected']
    assert len(failures) == 1
    assert SECRET not in caplog.text
    return failures[0]


@pytest.mark.parametrize('value', [None, True, '1', Decimal('NaN'), float('inf')])
def test_raw_nominal_types_are_identified(reads, caplog, value):
    """Malformed components reach the existing strict units/nano guard."""
    client, adapter = reads
    nominal = sdk.MoneyValue(currency='cny', units=1, nano=0)
    nominal.units = value
    item = instrument(uid='other-fx', nominal=nominal)
    item.credentials = SECRET
    client.instruments.currencies.return_value.instruments = [item]
    with pytest.raises(ValueError, match='invalid money units/nano') as caught:
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['metadata']['uid'] == 'other-fx'
    assert fact['operation'] == 'fx.list_pairs' and fact['field'] == 'nominal'
    assert fact['constraint'] == 'integer_units_nano_abs_nano_lt_1e9_same_sign'
    assert fact['source']['units']['type'] == type(value).__name__
    assert fact['normalized'] == 'unavailable'
    assert json.dumps(fact) in caught.value.__notes__[0]
    assert client.mock_calls == [call.instruments.currencies(
        instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL)]


@pytest.mark.parametrize('parts', [(0, 0), (-1, 0), (0, -1)])
def test_nonpositive_nominal_preserves_exact_source_and_decimal(reads, caplog, parts):
    """The FxPair finite_decimal rejection identifies nominal, not a guessed CNY bug."""
    client, adapter = reads
    nominal = sdk.MoneyValue(currency='cny', units=parts[0], nano=parts[1])
    client.instruments.currencies.return_value.instruments = [instrument(
        uid='usd-other', iso_currency_name='USD', nominal=replace(nominal, currency='usd'))]
    with pytest.raises(ValueError, match='FX nominal: expected a positive finite Decimal'):
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['field'] == 'nominal' and fact['constraint'] == 'positive_finite_decimal'
    assert fact['metadata']['uid'] == 'usd-other'
    assert fact['source']['units'] == {'type': 'int', 'value': str(parts[0])}
    assert fact['source']['nano'] == {'type': 'int', 'value': str(parts[1])}
    assert Decimal(fact['normalized']['value']) == Decimal(parts[0]) + Decimal(parts[1])/10**9


@pytest.mark.parametrize('parts', [(1, -1), (1, 1000000000), (-1, 1)])
def test_invalid_quotation_reports_original_components(reads, caplog, parts):
    """Do not let SDK constructor normalization hide malformed raw quotations."""
    client, adapter = reads
    nominal = quotation(*parts)
    nominal.currency = 'CNY'
    client.instruments.currencies.return_value.instruments = [instrument(nominal=nominal)]
    with pytest.raises(ValueError, match='invalid money units/nano'):
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['source']['units']['value'] == str(parts[0])
    assert fact['source']['nano']['value'] == str(parts[1])
    assert fact['source']['type'] == 'Quotation'


@pytest.mark.parametrize('field,value,constraint', [
    ('nominal', None, 'required_nominal'),
    ('iso_currency_name', None, 'three_ascii_letters'),
    ('currency', '', 'three_ascii_letters'),
    ('lot', 0, 'positive_integer_not_bool'),
    ('lot', True, 'positive_integer_not_bool'),
    ('uid', '', 'nonempty_uid_without_whitespace'),
    ('nominal', sdk.MoneyValue(currency='usd', units=1, nano=0),
     'nominal_currency_matches_iso_base'),
])
def test_metadata_guard_context_keeps_original_valueerror(reads, caplog, field, value, constraint):
    """Identity and numeric guards retain their exception type and expose source fields."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [instrument(**{field: value})]
    with pytest.raises(ValueError) as caught:
        adapter.list_pairs()
    assert isinstance(caught.value, ValueError)
    fact = rejection(caplog)
    expected_field = ('nominal.currency' if constraint == 'nominal_currency_matches_iso_base'
                      else field)
    assert fact['field'] == expected_field and fact['constraint'] == constraint


@pytest.mark.parametrize('currency', [None, '', True])
def test_invalid_nominal_currency_has_its_own_guard_context(reads, caplog, currency):
    """Invalid currency syntax is distinct from a valid currency disagreeing with ISO."""
    client, adapter = reads
    nominal = sdk.MoneyValue(currency='cny', units=1, nano=0)
    nominal.currency = currency
    client.instruments.currencies.return_value.instruments = [instrument(nominal=nominal)]
    with pytest.raises(ValueError, match='invalid monetary currency'):
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['field'] == 'nominal.currency' and fact['constraint'] == 'three_ascii_letters'
    assert fact['source']['type'] == type(currency).__name__


@pytest.mark.parametrize('value', [None, '1', True, Decimal('NaN'), Decimal('Infinity')])
def test_normalized_nominal_finite_decimal_guard_has_field_context(
        reads, caplog, monkeypatch, value):
    """Exercise FxPair's guard independently of the earlier strict quotation converter."""
    _, adapter = reads
    monkeypatch.setattr(tinvest_fx_data, '_amount', lambda *_: value)
    with pytest.raises(ValueError, match='FX nominal: expected a positive finite Decimal'):
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['field'] == 'nominal' and fact['constraint'] == 'positive_finite_decimal'
    assert fact['normalized']['type'] == type(value).__name__


def test_unexpected_objects_never_use_repr_or_dynamic_type_name(reads, caplog):
    """An unexpected component may contain arbitrary SDK credentials or repr text."""
    client, adapter = reads
    payload = SimpleNamespace(credentials=SECRET)
    client.instruments.currencies.return_value.instruments = [instrument(lot=payload)]
    with pytest.raises(ValueError):
        adapter.list_pairs()
    assert rejection(caplog)['source'] == {'type': 'unsupported', 'value': 'invalid'}


def test_original_guard_exception_object_is_preserved(reads, caplog, monkeypatch):
    """The safe diagnostic note supplements the original error rather than replacing it."""
    _, adapter = reads
    error = ValueError('FX nominal: expected a positive finite Decimal')

    def reject(*_):
        raise error

    monkeypatch.setattr(tinvest_fx_data, 'FxPair', reject)
    with pytest.raises(ValueError) as caught:
        adapter.list_pairs()
    assert caught.value is error
    assert rejection(caplog)['field'] == 'nominal'


@pytest.mark.parametrize('field', [
    'api_trade_available_flag', 'buy_available_flag', 'sell_available_flag',
])
def test_permissions_do_not_hide_invalid_source_types(reads, caplog, field):
    """Each source permission remains explicit, including the failing marker's raw type."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [instrument(**{field: None})]
    with pytest.raises(ValueError, match='FX metadata permissions must be bool'):
        adapter.list_pairs()
    fact = rejection(caplog)
    assert fact['constraint'] == 'boolean_permissions'
    assert fact['source'][field] == {'type': 'NoneType', 'value': 'None'}


def test_unrecognized_valueerror_never_logs_external_text(reads, caplog, monkeypatch):
    """Guard matching is an exact table; unknown error text cannot reach the emitter."""
    _, adapter = reads
    error = ValueError(SECRET)

    def reject(*_):
        raise error

    monkeypatch.setattr(tinvest_fx_data, 'FxPair', reject)
    with pytest.raises(ValueError) as caught:
        adapter.list_pairs()
    assert caught.value is error
    assert rejection(caplog)['constraint'] == 'FxPair_validation'


@pytest.fixture(name='setup_session')
def fixture_setup_session():
    """The actual live catalog can fail before account/configuration creation."""
    broker = OfflineBroker()
    response = broker.client.instruments.currencies.return_value
    bad = instrument(uid='other-fx', nominal=sdk.MoneyValue(currency='cny', units=0, nano=0))
    bad.credentials = SECRET
    response.instruments.insert(0, bad)
    session = SimpleNamespace(
        client=broker.client, data=TInvestStrategyData(broker.client), scenario='INDEX-CNY',
        records=[], configs={}, quotes={}, market=None,
        lifecycle=SimpleNamespace(created={}, cleanup=Mock(), run_id='offline'))
    session.data.begin_snapshot()
    return broker, session


def check_safe_setup_report(item, error):
    """Pytest removes captured sections, while safe diagnostics use the explicit emitter."""
    report = SimpleNamespace(when='setup', sections=[SECRET])
    hook = live_hooks.pytest_runtest_makereport(
        item, SimpleNamespace(excinfo=SimpleNamespace(value=error)))
    assert next(hook) is None
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.sections == []
    assert SECRET not in json.dumps(report.__dict__)


@pytest.fixture(name='cold_logger')
def fixture_cold_logger():
    """A fresh pytest child has not constructed Runner or configured INFO logging."""
    logger = logging.getLogger('tinkoffBot')
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield logger
    finally:
        logger.setLevel(previous)


def test_cny_catalog_failure_before_accounts_survives_stdout_dump_and_safe_report(
        setup_session, tmp_path, capsys, monkeypatch, cold_logger):
    """A bad unrelated pair must be attributed before the CNY filter and account setup."""
    broker, session = setup_session
    item = SimpleNamespace(nodeid='e2e/test_sandbox_multicurrency.py::test_index_cny',
                           name='test_index_cny')
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    set_diagnostic_context(stage='sandbox_e2e', scenario=item.nodeid, phase='setup')
    fixture = live_hooks.live.__wrapped__(SimpleNamespace(node=item, session=None), session)
    try:
        assert cold_logger.level == logging.WARNING
        assert next(fixture)[0] is session
        with pytest.raises(ValueError) as caught:
            scenarios.catalog(session)
        with pytest.raises(StopIteration):
            next(fixture)
        assert cold_logger.level == logging.WARNING
    finally:
        fixture.close()
        set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')
    output = capsys.readouterr().out
    assert SECRET not in output
    facts = [json.loads(line.removeprefix('Market diagnostic: ')) for line in output.splitlines()
             if line.startswith('Market diagnostic: ')]
    failure = next(fact for fact in facts if fact['event'] == 'fx_metadata_rejected')
    assert failure['metadata']['uid'] == 'other-fx' and failure['requested_uid'] == 'F'
    assert failure['scenario'] == item.nodeid and failure['phase'] == 'setup'
    selected = next(fact for fact in facts if fact['event'] == 'currency_setup_metadata')
    assert selected['currency']['uid'] == 'F' and selected['bond']['uid'] == 'C'
    assert selected['bond_full']['nominal']['units']['value'] == '1000'
    assert not broker.accounts
    broker.client.market_data.get_order_book.assert_not_called()
    check_safe_setup_report(item, caught.value)
    path = tmp_path / 'test_index_cny.json'
    assert json.loads(path.read_text())['accounts'] == {}
    assert json.loads(path.read_text())['configs'] == {}
    assert any(event.get('fact') == failure for event in json.loads(path.read_text())['events'])
    assert SECRET not in path.read_text()


def test_selected_cny_pair_logs_loaded_units_without_extra_rpc(reads, caplog):
    """Only a specifically requested setup pair gets an INFO success record."""
    from autorepeater.fx_diagnostics import requested_pair  # pylint: disable=import-outside-toplevel
    client, adapter = reads
    set_diagnostic_context(stage='sandbox_e2e', scenario='CNY-test', phase='setup')
    caplog.set_level(logging.INFO, logger='tinkoffBot')
    try:
        with requested_pair('cny-tom'):
            pair = adapter.list_pairs()[0]
    finally:
        set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    selected = next(fact for fact in facts if fact['event'] == 'fx_setup_pair')
    assert selected['metadata']['uid'] == pair.uid
    assert selected['metadata']['ticker'] == 'NOT_A_CURRENCY'
    assert selected['metadata']['class_code'] == 'CETS'
    assert selected['metadata']['nominal']['units']['value'] == '10'
    assert selected['normalized_nominal'] == {'type': 'Decimal', 'value': '10.000000000'}
    assert len(client.mock_calls) == 1


def test_live_finalizer_keeps_failure_and_timing_summary_in_existing_dump(
        setup_session, tmp_path, monkeypatch, capsys):
    """Exercise the real fixture finalizer with no accounts and no broker cleanup calls."""
    _, session = setup_session
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    request = SimpleNamespace(node=SimpleNamespace(name='offline-case', nodeid='offline-case'),
                              session=SimpleNamespace())
    set_diagnostic_context(stage='sandbox_e2e', scenario='offline-case', phase='call')
    fixture = live_hooks.live.__wrapped__(request, session)
    try:
        assert next(fixture)[0] is session
        with pytest.raises(ValueError):
            scenarios.catalog(session)
        with pytest.raises(StopIteration):
            next(fixture)
    finally:
        fixture.close()
        set_diagnostic_context(stage='unknown', scenario='unknown', phase='unknown')
    events = json.loads((tmp_path / 'offline-case.json').read_text())['events']
    facts = [event['fact'] for event in events if event['event'] == 'market_diagnostic']
    assert any(fact['event'] == 'fx_metadata_rejected' for fact in facts)
    totals = next(fact['operations'] for fact in facts if fact['event'] == 'market_stage_timings')
    assert totals['catalog.instrument_selection']['count'] == 3
    assert totals['metadata.find.request']['count'] == 3
    assert totals['metadata.catalog.request']['count'] == 1
    assert totals['metadata.instrument.request']['count'] == 2
    assert totals['fx.catalog.request']['count'] == 1
    assert all(fact['duration_seconds'] >= 0 for fact in totals.values())
    assert 'market_stage_timings' in capsys.readouterr().out
    session.lifecycle.cleanup.assert_called_once_with()


def test_selected_increment_has_raw_and_normalized_decimal(reads, caplog):
    """The increment uses the loaded settlement currency and keeps the nano source."""
    from autorepeater.fx_diagnostics import requested_pair  # pylint: disable=import-outside-toplevel
    client, adapter = reads
    client.instruments.currencies.return_value.instruments[0].min_price_increment = quotation(0, 1)
    caplog.set_level(logging.INFO, logger='tinkoffBot')
    with requested_pair('cny-tom'):
        adapter.list_pairs()
    fact = next(json.loads(record.message.removeprefix('Market diagnostic: '))
                for record in caplog.records if record.message.startswith('Market diagnostic: '))
    increment = fact['metadata']['min_price_increment']
    assert increment['nano'] == {'type': 'int', 'value': '1'}
    assert Decimal(increment['normalized']['value']) == Decimal('0.000000001')


def test_info_sdk_records_cannot_enter_diagnostic_stdout_or_dump(capsys):
    """INFO activation never gives arbitrary SDK messages or headers a forwarding route."""
    events = []
    handler = live_hooks.MarketDiagnosticOutput(records=events)
    handler.emit(logging.LogRecord('t_tech.invest', logging.INFO, 'sdk.py', 1,
                                   'headers: ' + SECRET, (), None))
    assert not events
    assert capsys.readouterr().out == ''
