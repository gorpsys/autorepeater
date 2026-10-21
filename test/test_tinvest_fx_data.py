"""Real SDK 1.51.0 read schemas behind unwrapped autospecced services; no live I/O."""
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError, schemas as sdk
from t_tech.invest.services import InstrumentsService, MarketDataService, OrdersService

from autorepeater.execution_data import ExecutionDataError, ExecutionSnapshot
from autorepeater.fx import build_fx_snapshot, select_direct_pair
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.tinvest_fx_data import TInvestFxData

D = Decimal
NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


def quotation(units, nano=0):
    """Keep malformed response components observable despite SDK normalization."""
    value = sdk.Quotation(units=0, nano=0)
    value.units, value.nano = units, nano
    return value


def instrument(**changes):
    """Ticker deliberately disagrees with the real currency identity."""
    return replace(sdk.Currency(
        uid='cny-tom', ticker='NOT_A_CURRENCY', class_code='CETS',
        iso_currency_name='CNY', currency='RUB', lot=1000,
        nominal=sdk.MoneyValue(currency='CNY', units=10, nano=0),
        api_trade_available_flag=True, buy_available_flag=True, sell_available_flag=True),
        **changes)


@pytest.fixture(name='reads')
def fixture_reads():
    """All RPCs are spec-checked and data is populated with actual SDK dataclasses."""
    client = Mock(spec_set=['instruments', 'market_data', 'orders'])
    for name, service in [('instruments', InstrumentsService), ('market_data', MarketDataService),
                          ('orders', OrdersService)]:
        setattr(client, name, create_autospec(inspect.unwrap(service),
                                              instance=True, spec_set=True))
    client.instruments.currencies.return_value = sdk.CurrenciesResponse(instruments=[instrument()])
    client.market_data.get_order_book.return_value = sdk.GetOrderBookResponse(
        instrument_uid='cny-tom', depth=1,
        bids=[sdk.Order(price=quotation(119, 1), quantity=3)],
        asks=[sdk.Order(price=quotation(120, 999999999), quantity=2)],
        orderbook_ts=NOW - timedelta(seconds=1),
        last_price=quotation(999), last_price_ts=NOW, close_price=quotation(888))
    client.market_data.get_trading_status.return_value = sdk.GetTradingStatusResponse(
        instrument_uid='cny-tom', api_trade_available_flag=True,
        bestprice_order_available_flag=True, market_order_available_flag=False,
        limit_order_available_flag=False,
        trading_status=sdk.SecurityTradingStatus.SECURITY_TRADING_STATUS_UNSPECIFIED)
    client.orders.get_max_lots.return_value = sdk.GetMaxLotsResponse(
        currency='RUB', buy_limits=sdk.BuyLimitsView(
            buy_money_amount=quotation(24200, 1), buy_max_lots=2, buy_max_market_lots=0),
        sell_limits=sdk.SellLimitsView(sell_max_lots=3),
        buy_margin_limits=sdk.BuyLimitsView(buy_max_lots=9000),
        sell_margin_limits=sdk.SellLimitsView(sell_max_lots=8000))
    return client, TInvestFxData(client, clock=lambda: NOW)


def test_sdk_identity_units_and_exact_rpc_boundary(reads):
    """Metadata nominal identifies base units; orderbook_ts is the only market clock."""
    client, adapter = reads
    snapshot = build_fx_snapshot(adapter, ['rub', 'CNY', 'cny'], now=NOW)
    book = snapshot.quotes['cny']
    assert (book.pair.base_currency, book.pair.quote_currency) == ('cny', 'rub')
    assert (book.pair.nominal, book.pair.lot_size) == (D(10), 1000)
    assert (book.bid, book.ask) == (D('119.000000001'), D('120.999999999'))
    assert (book.market_time, book.received_time) == (NOW - timedelta(seconds=1), NOW)
    assert snapshot.rate('cny') == D(12)
    assert client.mock_calls == [
        call.instruments.currencies(instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL),
        call.market_data.get_order_book(instrument_id='cny-tom', depth=1)]


def test_rub_does_not_even_load_catalog(reads):
    """RUB identity remains local with the real adapter as well as the port mock."""
    client, adapter = reads
    assert build_fx_snapshot(adapter, ['RUB'], now=NOW).rate('rub') == D(1)
    assert client.mock_calls == []


def test_sdk_never_guesses_currency_from_ticker(reads):
    """A CNY-looking ticker can be a USD pair; it must not satisfy CNY demand."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [instrument(
        ticker='CNYRUB_TOM', iso_currency_name='USD',
        nominal=sdk.MoneyValue(currency='usd', units=1, nano=0))]
    with pytest.raises(ValueError, match='route'):
        build_fx_snapshot(adapter, ['cny'], now=NOW)
    client.market_data.get_order_book.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'iso_currency_name': ''}, {'currency': None}, {'uid': ''}, {'lot': 0}, {'lot': True},
    {'nominal': None}, {'nominal': sdk.MoneyValue(currency='usd', units=10, nano=0)},
    {'nominal': sdk.MoneyValue(currency='cny', units=0, nano=0)},
    {'nominal': sdk.MoneyValue(currency='cny', units=1, nano=-1)},
    {'api_trade_available_flag': None}, {'buy_available_flag': 1},
    {'sell_available_flag': None},
])
def test_sdk_rejects_unknown_identity_or_units(reads, changes):
    """Malformed metadata is rejected before reading a book or account limits."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [instrument(**changes)]
    with pytest.raises(ValueError):
        adapter.list_pairs()
    client.market_data.get_order_book.assert_not_called()
    client.orders.get_max_lots.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('instrument_uid', 'other'), ('bids', []), ('asks', []), ('orderbook_ts', None),
    ('orderbook_ts', NOW.replace(tzinfo=None)),
    ('orderbook_ts', NOW - timedelta(seconds=180, microseconds=1)),
    ('orderbook_ts', NOW - timedelta(seconds=240)),
    ('orderbook_ts', NOW + timedelta(milliseconds=100, microseconds=1)),
    ('orderbook_ts', NOW + timedelta(seconds=1)),
    ('bids', [sdk.Order(price=quotation(122), quantity=1)]),
    ('asks', [sdk.Order(price=quotation(0), quantity=1)]),
    ('bids', [sdk.Order(price=None, quantity=1)]),
    ('bids', [sdk.Order(price=quotation(119), quantity=0)]),
    ('asks', [sdk.Order(price=quotation(121), quantity=True)]),
    ('bids', [sdk.Order(price=quotation(119, 1000000000), quantity=1)]),
    ('asks', [sdk.Order(price=quotation(121, -1), quantity=1)]),
    ('bids', [sdk.Order(price=quotation(True), quantity=1)]),
])
def test_bad_book_never_falls_back_to_fresh_last_trade_or_one(reads, field, value):
    """Last/close trades do not repair a missing, crossed, future or stale book."""
    client, adapter = reads
    setattr(client.market_data.get_order_book.return_value, field, value)
    with pytest.raises(ValueError):
        build_fx_snapshot(adapter, ['cny'], now=NOW)
    client.market_data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('age', [60, 60.121750, 90, 90.000001, 120, 128.014157, 160, 180])
def test_fx_book_accepts_historical_staleness_and_120_second_boundary(reads, age):
    """The FX adapter and immutable snapshot use the same inclusive market-age policy."""
    client, adapter = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=age)
    assert build_fx_snapshot(adapter, ['cny'], now=NOW).rate('cny') == D(12)
    client.market_data.get_order_book.assert_called_once_with(instrument_id='cny-tom', depth=1)
    client.market_data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('ahead', [timedelta(0), timedelta(microseconds=1),
                                   timedelta(milliseconds=50), timedelta(milliseconds=65),
                                   timedelta(microseconds=65426), timedelta(milliseconds=100)])
def test_fx_accepts_bounded_market_clock_skew_without_rewriting_times(reads, ahead):
    """The adapter and snapshot retain the raw timestamp within the inclusive allowance."""
    client, adapter = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW + ahead
    snapshot = build_fx_snapshot(adapter, ['cny'], now=NOW)
    book = snapshot.quotes['cny']
    assert (book.market_time, book.received_time) == (NOW + ahead, NOW)
    assert snapshot.rate('cny') == D(12)
    book.validate_fresh(NOW)
    snapshot.validate_fresh(NOW + timedelta(microseconds=1))
    client.market_data.get_order_book.assert_called_once_with(instrument_id='cny-tom', depth=1)
    client.market_data.get_last_prices.assert_not_called()


def test_fx_samples_validation_clock_after_book_response(reads):
    """The validation time belongs to the returned quote, not the request start."""
    client, adapter = reads
    selected = adapter.list_pairs()[0]

    def clock():
        client.market_data.get_order_book.assert_called_once_with(instrument_id='cny-tom', depth=1)
        return NOW

    assert TInvestFxData(client, clock=clock).get_quote(selected).received_time == NOW


def test_rules_are_fresh_own_limits_and_explicit_permissions(reads):
    """No price/GetOrderPrice is necessary for BESTPRICE; neither margin cap is used."""
    client, adapter = reads
    selected = adapter.list_pairs()[0]
    rules = adapter.get_trade_rules('account', selected)
    assert (rules.lot, rules.currency) == (1000, 'rub')
    assert (rules.buy_money_amount, rules.buy_max_lots, rules.sell_max_lots) == (
        D('24200.000000001'), 2, 3)
    assert rules.api_trade_available and rules.bestprice_order_available
    request = client.orders.get_max_lots.call_args.kwargs['request']
    assert (request.account_id, request.instrument_id) == ('account', 'cny-tom')
    assert request.price is sdk.GetMaxLotsRequest().price
    client.market_data.get_trading_status.assert_called_once_with(instrument_id='cny-tom')
    client.orders.get_order_price.assert_not_called()
    client.orders.post_order.assert_not_called()
    client.orders.get_max_lots.return_value.buy_limits.buy_max_lots = 0
    assert adapter.get_trade_rules('account', selected).buy_max_lots == 0
    assert client.orders.get_max_lots.call_count == 2


@pytest.mark.parametrize('field', ['api_trade_available_flag', 'bestprice_order_available_flag'])
def test_closed_market_is_separate_from_valuation(reads, field):
    """Status enum is not a NORMAL_TRADING-only whitelist."""
    client, adapter = reads
    setattr(client.market_data.get_trading_status.return_value, field, False)
    snapshot = build_fx_snapshot(adapter, ['cny'], now=NOW)
    assert snapshot.rate('cny') == D(12)
    selected = snapshot.quotes['cny'].pair
    rules = adapter.get_trade_rules('account', selected)
    state = ExecutionSnapshot(PortfolioSnapshot(()), D(0), {}, {},
                              {'rub': D(50000)}, {}, (), True)
    assert not snapshot.quotes['cny'].can_trade(
        'BUY', 1, rules, state, now=NOW)


def test_catalog_metadata_api_permission_cannot_be_overridden(reads):
    """Current status cannot enable a pair disabled by full instrument metadata."""
    client, adapter = reads
    client.instruments.currencies.return_value.instruments = [instrument(
        api_trade_available_flag=False)]
    selected = adapter.list_pairs()[0]
    assert not adapter.get_trade_rules('account', selected).api_trade_available
    assert select_direct_pair([selected], 'cny') == selected


@pytest.mark.parametrize('field,value', [
    ('instrument_uid', 'other'), ('api_trade_available_flag', None),
    ('bestprice_order_available_flag', 1),
])
def test_rules_reject_bad_status(reads, field, value):
    """No permission is manufactured from missing or mismatched responses."""
    client, adapter = reads
    setattr(client.market_data.get_trading_status.return_value, field, value)
    with pytest.raises(ValueError):
        adapter.get_trade_rules('account', adapter.list_pairs()[0])
    client.orders.get_max_lots.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('currency', 'cny'), ('currency', None), ('buy_limits', None), ('sell_limits', None),
])
def test_rules_reject_inconsistent_native_caps(reads, field, value):
    """Own monetary limits must have the quote currency, not the purchased currency."""
    client, adapter = reads
    setattr(client.orders.get_max_lots.return_value, field, value)
    with pytest.raises(ValueError):
        adapter.get_trade_rules('account', adapter.list_pairs()[0])


@pytest.mark.parametrize('field,value', [
    ('buy_money_amount', quotation(-1)), ('buy_money_amount', quotation(1, -1)),
    ('buy_max_lots', True), ('buy_max_lots', -1), ('sell_max_lots', None),
])
def test_rules_reject_invalid_own_values(reads, field, value):
    """Malformed own limits cannot be replaced with much larger margin limits."""
    client, adapter = reads
    caps = client.orders.get_max_lots.return_value
    setattr(caps.sell_limits if field.startswith('sell') else caps.buy_limits, field, value)
    with pytest.raises(ValueError):
        adapter.get_trade_rules('account', adapter.list_pairs()[0])


@pytest.mark.parametrize('method', ['catalog', 'book', 'status', 'caps'])
def test_transport_errors_have_safe_reason_and_preserved_cause(reads, method):
    """No raw SDK error or credentials appear in the public exception message."""
    client, adapter = reads
    selected = adapter.list_pairs()[0]
    error = RequestError(StatusCode.UNAVAILABLE, 'private transport detail', None)
    methods = {'catalog': client.instruments.currencies, 'book': client.market_data.get_order_book,
               'status': client.market_data.get_trading_status,
               'caps': client.orders.get_max_lots}
    rpc = methods[method]
    rpc.side_effect = error
    with pytest.raises(ExecutionDataError, match='FX data read failed') as caught:
        if method == 'catalog':
            adapter.list_pairs()
        elif method == 'book':
            adapter.get_quote(selected)
        else:
            adapter.get_trade_rules('account', selected)
    assert caught.value.__cause__ is error
    assert 'private' not in str(caught.value)
