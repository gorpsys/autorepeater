"""Actual zero-nominal noninstrument must not poison unrelated direct FX routes."""
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import call
from test.test_fx_runtime import fixture_sdk_runtime, launch
from test.test_tinvest_fx_data import NOW, fixture_reads, instrument

import pytest
from t_tech.invest import schemas as sdk

from autorepeater.cash_facts import MoneyAmount
from autorepeater.fx import build_fx_snapshot, select_direct_pair
from autorepeater.fx_diagnostics import requested_pair

D = Decimal
USD_UID = 'c898d684-a9df-4e1a-a30b-ad9dbb5ea1f9'
CNY_UID = '4587ab1d-a9c9-4910-a0d6-86c7b9c42510'


def placeholder():
    """Safe actual fields from run38995748904, without ticker-based classification."""
    return instrument(
        uid=USD_UID, ticker='USDFIXME', class_code='CETS',
        iso_currency_name='USD', currency='rub', lot=0,
        nominal=sdk.MoneyValue(currency='usd', units=0, nano=0),
        min_price_increment=sdk.Quotation(units=0, nano=2500000),
        api_trade_available_flag=False, buy_available_flag=False, sell_available_flag=False)


@pytest.fixture(name='reads')
def fixture_catalog_reads():
    """Use strict, unwrapped SDK services without creating a broker client."""
    return fixture_reads.__wrapped__()


@pytest.fixture(name='runtime')
def fixture_catalog_runtime():
    """The same real Runner and SDK adapters as the native execution regression."""
    return fixture_sdk_runtime.__wrapped__()


@pytest.mark.parametrize('order', [(0, 1), (1, 0), (0, 2, 1), (1, 2, 0)])
def test_actual_placeholder_does_not_block_cny_snapshot(reads, caplog, order):
    """Zero USD nominal before CNY must not prevent an exact fresh midpoint."""
    client, adapter = reads
    valid = instrument(uid=CNY_UID, ticker='CNYRUB_TOM')
    other = replace(placeholder(), uid='another-unused', ticker='UNRELATED')
    items = [placeholder(), valid, other]
    items[0].credentials = 'private-do-not-log'
    client.instruments.currencies.return_value.instruments = [items[index] for index in order]
    client.market_data.get_order_book.return_value.instrument_uid = CNY_UID
    with requested_pair(CNY_UID):
        snapshot = build_fx_snapshot(adapter, ['rub', 'cny'], now=NOW)
    assert snapshot.rate('cny') == D(12)
    assert snapshot.quotes['cny'].pair.uid == CNY_UID
    assert snapshot.quotes['cny'].pair.lot_size == 1000
    assert client.mock_calls == [
        call.instruments.currencies(instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL),
        call.market_data.get_order_book(instrument_id=CNY_UID, depth=1)]
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    skipped = next(fact for fact in facts if fact['event'] == 'fx_catalog_skipped')
    assert skipped['operation'] == 'fx.list_pairs'
    assert skipped['field'] == 'nominal/lot/permissions'
    assert skipped['reason'] == 'zero_nominal_zero_lot_explicitly_unavailable'
    assert skipped['omitted_count'] == 0
    expected = [items[index].uid for index in order if index != 1]
    assert [item['uid'] for item in skipped['items']] == expected
    assert 'requested_uid' not in skipped
    for metadata in skipped['items']:
        assert metadata['lot'] == {'type': 'int', 'value': '0'}
        for flag in ('api_trade_available_flag', 'buy_available_flag', 'sell_available_flag'):
            assert metadata[flag] == {'type': 'bool', 'value': 'False'}
        assert metadata['nominal']['currency']['value'] == 'usd'
        assert metadata['nominal']['units']['value'] == '0'
        assert metadata['nominal']['nano']['value'] == '0'
        assert D(metadata['nominal']['normalized']['value']) == 0
    assert 'private-do-not-log' not in caplog.text


def test_skip_diagnostic_is_bounded_without_truncating_valid_catalog(reads, caplog):
    """Only twenty skipped metadata records expand; all usable DTOs survive."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [
        replace(placeholder(), uid=f'unused-{index}') for index in range(23)] + [instrument()]
    assert [pair.uid for pair in adapter.list_pairs()] == ['cny-tom']
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    assert len(facts) == 1
    assert facts[0]['event'] == 'fx_catalog_skipped'
    assert len(facts[0]['items']) == 20 and facts[0]['omitted_count'] == 3
    assert [item['uid'] for item in facts[0]['items']] == [f'unused-{i}' for i in range(20)]
    assert client.instruments.currencies.call_count == 1


def test_valid_inactive_pair_values_held_currency_but_cannot_trade(reads):
    """Catalog availability does not erase a valid route used for native cash value."""
    client, adapter = reads
    valid = instrument(api_trade_available_flag=False, buy_available_flag=False,
                       sell_available_flag=False)
    client.instruments.currencies.return_value.instruments = [placeholder(), valid]
    snapshot = build_fx_snapshot(adapter, ['cny'], now=NOW)
    assert snapshot.to_rub(MoneyAmount('cny', D(100))) == MoneyAmount('rub', D(1200))
    pair = snapshot.quotes['cny'].pair
    assert not pair.api_trade_available and not pair.buy_available and not pair.sell_available
    assert not adapter.get_trade_rules('account', pair).api_trade_available
    client.orders.post_order.assert_not_called()


def test_placeholder_is_not_a_usable_required_route(reads):
    """Neither implicit currency demand nor explicit UID can use the rejected record."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [placeholder(), instrument()]
    pairs = adapter.list_pairs()
    with pytest.raises(ValueError, match='no direct FX route'):
        select_direct_pair(pairs, 'usd', pair_uid=USD_UID)
    with pytest.raises(ValueError, match='no direct FX route'):
        build_fx_snapshot(adapter, ['usd'], now=NOW)
    client.market_data.get_order_book.assert_not_called()
    client.orders.get_max_lots.assert_not_called()
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'api_trade_available_flag': True}, {'buy_available_flag': True},
    {'sell_available_flag': True}, {'api_trade_available_flag': None},
    {'buy_available_flag': 0}, {'sell_available_flag': None},
    {'lot': True}, {'lot': None}, {'lot': -1}, {'lot': 1},
    {'nominal': sdk.MoneyValue(currency='usd', units=-1, nano=0)},
    {'nominal': sdk.MoneyValue(currency='usd', units=1, nano=0)},
    {'nominal': None}, {'nominal': sdk.MoneyValue(currency='cny', units=0, nano=0)},
    {'uid': ''}, {'uid': None}, {'currency': None}, {'iso_currency_name': None},
    {'lot': False}, {'api_trade_available_flag': 0},
])
def test_near_placeholder_errors_still_fail_closed(reads, changes):
    """No broad unavailable, invalid-lot, missing-field or ValueError suppression."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [
        replace(placeholder(), **changes), instrument()]
    with pytest.raises(ValueError):
        build_fx_snapshot(adapter, ['cny'], now=NOW)
    client.market_data.get_order_book.assert_not_called()
    client.orders.get_max_lots.assert_not_called()
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('field,value', [('units', False), ('nano', None), ('nano', -1)])
def test_bad_nominal_components_are_not_a_zero_placeholder(reads, field, value):
    """Classification follows exact units/nano validation, never truthiness."""
    client, adapter = reads
    item = placeholder()
    setattr(item.nominal, field, value)
    client.instruments.currencies.return_value.instruments = [item, instrument()]
    with pytest.raises(ValueError):
        adapter.list_pairs()
    client.market_data.get_order_book.assert_not_called()


def test_skipped_diagnostic_survives_later_active_metadata_failure(reads, caplog):
    """Skipped facts survive a subsequent active metadata failure."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [placeholder(), instrument(lot=0)]
    with pytest.raises(ValueError, match='invalid FX pair lot'):
        adapter.list_pairs()
    facts = [json.loads(record.message.removeprefix('Market diagnostic: '))
             for record in caplog.records if record.message.startswith('Market diagnostic: ')]
    assert [fact['event'] for fact in facts] == ['fx_metadata_rejected', 'fx_catalog_skipped']
    assert facts[0]['metadata']['uid'] == 'cny-tom'
    assert facts[1]['items'][0]['uid'] == USD_UID
    client.market_data.get_order_book.assert_not_called()


@pytest.mark.parametrize('placement', ['first', 'last'])
def test_normal_runner_preserves_funding_and_receipts_with_placeholder(runtime, placement):
    """Actual adapters still fund the paper, preserve fees, reserve and exact orders."""
    client, state = runtime
    catalog = client.instruments.currencies.return_value.instruments
    catalog.insert(0 if placement == 'first' else len(catalog), placeholder())
    plan = launch(client)
    assert plan.marks == {'C': D(720)}
    assert plan.snapshot.budget == D(1000) and plan.strategy.cash_floor == D(100)
    assert [(intent.uid, intent.lots) for intent in plan.funding.exchanges] == [('cny-rub', 65)]
    assert [item.kwargs['instrument_id'] for item in client.orders.post_order.call_args_list] == [
        'cny-rub', 'C']
    assert state.cash == {'rub': D(219), 'cny': D(4)} and state.held == 1
    assert len(state.requests) == len(set(state.requests)) == 2
    client.instruments.currencies.assert_called_once_with(
        instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL)
    assert USD_UID not in [item.kwargs['instrument_id']
                           for item in client.market_data.get_order_book.call_args_list]


def test_normal_runner_preserves_inactive_cash_valuation_and_preflight(runtime):
    """Held CNY still counts toward budget; unavailable FX blocks every trade."""
    client, state = runtime
    state.cash = {'rub': D(0), 'cny': D(100)}
    pair = client.instruments.currencies.return_value.instruments[0]
    pair.api_trade_available_flag = pair.buy_available_flag = pair.sell_available_flag = False
    client.instruments.currencies.return_value.instruments.insert(0, placeholder())
    plan = launch(client)
    assert plan.snapshot.budget == D(1200) and plan.strategy.cash_floor == D(120)
    assert plan.funding.demands and not plan.funding.exchanges
    assert state.cash == {'rub': D(0), 'cny': D(100)} and state.held == 0
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('kind', ['active-unrelated', 'required', 'missing', 'only-placeholder'])
def test_normal_runner_bad_metadata_cannot_reach_effects(runtime, kind):
    """Malformed active/required metadata and a missing usable route stop the pass."""
    client, state = runtime
    catalog = client.instruments.currencies.return_value.instruments
    if kind == 'active-unrelated':
        catalog.insert(0, replace(placeholder(), api_trade_available_flag=True))
    elif kind == 'required':
        catalog[0].nominal = sdk.MoneyValue(currency='cny', units=0, nano=0)
    elif kind == 'missing':
        catalog[0].buy_available_flag = None
    else:
        catalog[:] = [replace(placeholder(), iso_currency_name='cny',
                              nominal=sdk.MoneyValue(currency='cny', units=0, nano=0))]
    with pytest.raises(ValueError):
        launch(client)
    assert state.cash == {'rub': D(1000), 'cny': D(0)} and state.held == 0
    client.orders.post_order.assert_not_called()
    client.orders.get_max_lots.assert_not_called()
    client.market_data.get_trading_status.assert_not_called()
