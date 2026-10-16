"""Configured settlement preference reaches actual SDK identities without extra reads."""
from dataclasses import replace
from decimal import Decimal
import inspect
from types import SimpleNamespace
from unittest.mock import call, create_autospec
from test.test_index_currency import document

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import InstrumentsService, MarketDataService

from autorepeater.index_config import IndexConfig, validate_index_config
from autorepeater.index_strategy import IndexStrategy
from autorepeater.strategies import create_strategy, prepare_strategy
from autorepeater.strategy_data import InstrumentInfo, InstrumentMatch, InstrumentType
from autorepeater.strategy_data import PriceQuote, StrategyData
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from e2e.test_sandbox_multicurrency import _unique
from scripts.sandbox_lifecycle import SandboxFailure


def config_document():
    """One exact ticker with independent RUB reference data."""
    payload = document()
    payload['instruments'] = [dict(payload['instruments'][0], ticker='RU000A105104')]
    return payload


@pytest.mark.parametrize('value,expected', [
    ('RUB', 'rub'), ('rub', 'rub'), ('CNY', 'cny'), ('cNy', 'cny')])
def test_preferred_currency_config_normalizes_only_case(value, expected):
    """Settlement preference leaves historical RUB units intact."""
    payload = config_document()
    payload['preferred_currency'] = value
    config = validate_index_config(payload)
    assert config.preferred_currency == expected
    assert config.reference_units.capitalization_currency == 'rub'
    assert not config.reference_units.price_currencies


def test_legacy_config_and_original_constructor_default_to_rub():
    """Both JSON and positional construction retain the implicit RUB contract."""
    config = validate_index_config(config_document())
    assert config.preferred_currency == 'rub'
    assert IndexConfig(config.name, config.max_lot_weight_error, config.instruments,
                       config.reserve, config.allocation_drift_limits).preferred_currency == 'rub'


@pytest.mark.parametrize('value', [None, '', ' RUB', 'CNY ', 'XXX', 'unknown', 1, True, [], {}])
def test_invalid_preference_fails_during_preparation(value):
    """An explicit bad preference cannot silently become the default."""
    payload = config_document()
    payload['preferred_currency'] = value
    with pytest.raises(ValueError, match='preferred_currency'):
        validate_index_config(payload)


def instrument(uid, currency, available=True):
    """Actual full metadata, never guessed from the ticker or board."""
    return InstrumentInfo(uid, 'RU000A105104', 'bond', InstrumentType.BOND,
                          'TQOY' if currency == 'cny' else 'TQCB', 1, currency, available)


@pytest.fixture(name='data')
def fixture_data():
    """Only the neutral read port is available, with no SDK or effect methods."""
    port = create_autospec(StrategyData, instance=True, spec_set=True)
    port.get_last_prices.side_effect = lambda uids: [
        PriceQuote(uid, Decimal(100), None) for uid in uids]
    return port


def wire(data, candidates):
    """Search metadata has no currency; selection must load every full candidate once."""
    data.find_instruments.return_value = [InstrumentMatch(
        item.uid, item.ticker, item.name, item.instrument_type, item.class_code)
        for item in candidates]
    data.get_instrument.side_effect = candidates


@pytest.mark.parametrize('preferred, currencies, expected', [
    (None, ('cny', 'rub'), '1'), ('CNY', ('rub', 'cny'), '1'),
    ('RUB', ('rub', 'cny'), '0'), ('CNY', ('cny', 'rub'), '0'),
    (None, ('cny',), '0'), ('CNY', ('rub',), '0'),
    ('CNY', ('usd', 'rub', 'cny'), '2'),
])
def test_index_prefers_currency_and_keeps_unique_foreign_fallback(
        data, preferred, currencies, expected):
    """Currency outranks result order while a lone foreign candidate stays usable."""
    payload = config_document()
    if preferred is not None:
        payload['preferred_currency'] = preferred
    candidates = [instrument(str(i), code) for i, code in enumerate(currencies)]
    wire(data, candidates)
    snapshot = IndexStrategy(validate_index_config(payload)).load_snapshot(data)
    quote = snapshot['RU000A105104']
    assert (quote.uid, quote.currency) == (expected, candidates[int(expected)].currency)
    assert data.method_calls == [call.find_instruments('RU000A105104')] + [
        call.get_instrument(item.uid) for item in candidates] + [call.get_last_prices([expected])]


@pytest.mark.parametrize('currencies,preferred', [
    (('rub', 'rub'), 'RUB'), (('cny', 'cny', 'rub'), 'CNY'),
    (('usd', 'eur'), 'CNY'), (('cny', 'usd'), 'RUB'),
])
def test_remaining_ambiguity_fails_before_prices(data, currencies, preferred):
    """Neither duplicate preferred boards nor an unmatched set has a first winner."""
    wire(data, [instrument(str(i), code) for i, code in enumerate(currencies)])
    payload = dict(config_document(), preferred_currency=preferred)
    with pytest.raises(ValueError, match='ambiguous index instrument'):
        IndexStrategy(validate_index_config(payload)).load_snapshot(data)
    data.get_last_prices.assert_not_called()
    assert data.get_instrument.call_count == len(currencies)


@pytest.mark.parametrize('preferred', ['CNY', 'RUB'])
def test_unavailable_preferred_candidate_cannot_displace_available(data, preferred):
    """API permission precedes the configured currency priority."""
    wire(data, [instrument('closed', preferred.lower(), False), instrument('open', 'usd')])
    payload = dict(config_document(), preferred_currency=preferred)
    assert IndexStrategy(validate_index_config(payload)).load_snapshot(
        data)['RU000A105104'].uid == 'open'
    data.get_last_prices.assert_called_once_with(['open'])


@pytest.mark.parametrize('candidates', [[], [instrument('closed', 'cny', False)]])
def test_no_available_candidate_remains_error(data, candidates):
    """No search hits and no API permission both stop before price reads."""
    wire(data, candidates)
    with pytest.raises(ValueError, match='no index instrument'):
        IndexStrategy(validate_index_config(config_document())).load_snapshot(data)
    data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('value', [None, '', ' rub', 1, True, 'credential-DO-NOT-LOG'])
def test_missing_or_invalid_live_currency_never_becomes_rub(data, value):
    """Unknown live units cannot masquerade as a RUB preference match."""
    wire(data, [instrument('bad', value), instrument('good', 'rub')])
    with pytest.raises(ValueError, match='currency') as caught:
        IndexStrategy(validate_index_config(config_document())).load_snapshot(data)
    assert 'credential-DO-NOT-LOG' not in str(caught.value)
    data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('preferred,expected', [(None, 'rub'), ('RUB', 'rub'), ('CNY', 'cny')])
def test_prepared_config_reaches_real_sdk_selector(tmp_path, monkeypatch, preferred, expected):
    """Prepared JSON drives actual SDK conversion and full metadata reads exactly once."""
    import json  # pylint: disable=import-outside-toplevel
    payload = config_document()
    if preferred is not None:
        payload['preferred_currency'] = preferred
    (tmp_path / 'mixed.json').write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))
    monkeypatch.delenv('IMOEX_CONFIG_PATH', raising=False)
    prepared = prepare_strategy('INDEX', payload['name'])
    strategy = create_strategy(prepared)
    client = SimpleNamespace(**{name: create_autospec(
        inspect.unwrap(service), instance=True, spec_set=True) for name, service in (
            ('instruments', InstrumentsService), ('market_data', MarketDataService))})
    candidates = [instrument('rub', 'RUB'), instrument('cny', 'CNY')]
    client.instruments.find_instrument.return_value = sdk.FindInstrumentResponse(instruments=[
        sdk.InstrumentShort(uid=item.uid, ticker=item.ticker, instrument_type='bond',
                            class_code=item.class_code) for item in candidates])
    client.instruments.get_instrument_by.side_effect = [sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid=item.uid, ticker=item.ticker, instrument_type='bond',
                                  class_code=item.class_code, lot=1, currency=item.currency,
                                  api_trade_available_flag=True)) for item in candidates]
    client.market_data.get_last_prices.return_value = sdk.GetLastPricesResponse(last_prices=[
        sdk.LastPrice(instrument_uid=expected, price=sdk.Quotation(units=100, nano=0))])
    adapter = TInvestStrategyData(client)
    adapter.begin_snapshot()
    snapshot = strategy.load_snapshot(adapter)
    assert snapshot['RU000A105104'].uid == expected
    assert snapshot['RU000A105104'].currency == expected.upper()
    assert client.instruments.method_calls == [call.find_instrument(query='RU000A105104')] + [
        call.get_instrument_by(id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=item.uid)
        for item in candidates]
    client.market_data.get_last_prices.assert_called_once_with(instrument_id=[expected])


@pytest.mark.parametrize('preferred,expected', [('RUB', 'r'), ('CNY', 'c')])
def test_setup_uses_same_currency_preference_without_extra_reads(data, preferred, expected):
    """Setup selects the actual requested currency from its already loaded response."""
    wire(data, [instrument('r', 'rub'), instrument('c', 'cny')])
    session = SimpleNamespace(data=data, scenario='offline')
    assert _unique(session, 'RU000A105104', preferred_currency=preferred).uid == expected
    assert data.method_calls == [call.find_instruments('RU000A105104'),
                                 call.get_instrument('r'), call.get_instrument('c')]


def test_setup_preference_diagnostic_explains_remaining_ambiguity(data, capsys):
    """Bounded facts distinguish discarded currency from still-ambiguous preferred boards."""
    import json  # pylint: disable=import-outside-toplevel
    wire(data, [instrument('r', 'rub'), instrument('c', 'cny'), instrument('c2', 'cny')])
    with pytest.raises(SandboxFailure):
        _unique(SimpleNamespace(data=data, scenario='offline'), 'RU000A105104',
                preferred_currency='CNY')
    fact = json.loads(capsys.readouterr().out.removeprefix('Sandbox diagnostic: '))
    assert fact['preferred_currency'] == 'cny'
    assert fact['raw_count'] == 3 and fact['eligible_count'] == 2
    assert [item['reason'] for item in fact['candidates']] == [
        'nonpreferred_currency', 'eligible', 'eligible']


def test_successful_preferred_setup_has_no_false_metadata_failure(data, capsys):
    """A discarded RUB alternative is not a metadata failure of the selected CNY bond."""
    wire(data, [instrument('r', 'rub'), instrument('c', 'cny')])
    chosen = _unique(SimpleNamespace(data=data, scenario='offline'), 'RU000A105104',
                     expected=(InstrumentType.BOND, 'cny'), preferred_currency='CNY')
    assert chosen.uid == 'c'
    assert not capsys.readouterr().out
    assert data.method_calls == [call.find_instruments('RU000A105104'),
                                 call.get_instrument('r'), call.get_instrument('c')]


def test_unavailable_invalid_currency_is_not_a_preference_candidate(data):
    """An unavailable board supplies neither a match nor usable currency metadata."""
    wire(data, [replace(instrument('closed', 'cny', False), currency=None),
                instrument('good', 'rub')])
    assert IndexStrategy(validate_index_config(config_document())).load_snapshot(
        data)['RU000A105104'].uid == 'good'


@pytest.mark.parametrize('currencies,expected', [
    (('rub',), '0'), (('usd', 'rub'), None), (('cny', 'cny'), None)])
def test_setup_preserves_unique_fallback_and_ambiguity(data, currencies, expected):
    """Setup shares INDEX singleton fallback and rejects both forms of ambiguity."""
    wire(data, [instrument(str(i), code) for i, code in enumerate(currencies)])
    session = SimpleNamespace(data=data, scenario='offline')
    if expected is None:
        with pytest.raises(SandboxFailure):
            _unique(session, 'RU000A105104', preferred_currency='CNY')
    else:
        assert _unique(session, 'RU000A105104', preferred_currency='CNY').uid == expected
    assert data.get_instrument.call_count == len(currencies)


@pytest.mark.parametrize('value,marker', [
    (None, 'missing'), ('', 'missing'), ('credential-DO-NOT-LOG', 'invalid')])
def test_setup_invalid_currency_diagnostic_preserves_actual_metadata(data, capsys, value, marker):
    """Invalid metadata stops setup with bounded safe facts and no rereads."""
    import json  # pylint: disable=import-outside-toplevel
    wire(data, [instrument('bad', value)])
    with pytest.raises(ValueError, match='currency'):
        _unique(SimpleNamespace(data=data, scenario='offline'), 'RU000A105104')
    text = capsys.readouterr().out
    assert 'credential-DO-NOT-LOG' not in text
    fact = json.loads(text.removeprefix('Sandbox diagnostic: '))
    assert fact['reason'] == 'invalid_currency'
    assert fact['candidates'][0]['currency'] == marker
    data.get_instrument.assert_called_once_with('bad')
