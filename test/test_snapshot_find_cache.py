"""Offline RPC counts and source boundaries for snapshot-local search reuse."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from unittest.mock import Mock, call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError, schemas as sdk
from t_tech.invest.services import (
    InstrumentsService, MarketDataService, OperationsService, OrdersService,
)

from autorepeater.composite_strategy import (
    CompositeStrategy, PreparedCompositeComponent, PreparedCompositeSource,
)
from autorepeater.fx import FxSnapshot
from autorepeater.execution import OrderExecutor
from autorepeater.execution_data import ExecutionData
from autorepeater.index_strategy import IndexStrategy
from autorepeater.repeater import AutoRepeater
from autorepeater.strategies import create_strategy, prepare_strategy
from autorepeater.strategy_data import DataAccessError
from autorepeater.strategy_plan import StrategyContext
from autorepeater.tinvest_execution_data import TInvestExecutionData
from autorepeater.tinvest_security_data import TInvestSecurityData
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from autorepeater.valuation import Valuation


NOW = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)


@pytest.fixture(name='client')
def fixture_client():
    """No Client construction; all unary boundaries use unwrapped strict specs."""
    client = Mock(spec_set=['instruments', 'market_data', 'operations', 'orders'])
    for name, service in [('instruments', InstrumentsService), ('market_data', MarketDataService),
                          ('operations', OperationsService), ('orders', OrdersService)]:
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    return client


def search(*uids, query='GOLD', kind='etf'):
    """Keep returned order, including duplicate UIDs and fields."""
    return sdk.FindInstrumentResponse(instruments=[sdk.InstrumentShort(
        uid=uid, ticker=query, name=uid, instrument_type=kind, class_code='ANY')
        for uid in uids])


class UncachedFindData(TInvestStrategyData):
    """Reference keeps all existing pass caches except the proposed Find reuse."""

    def find_instruments(self, query):
        self._find_cache = None
        return super().find_instruments(query)


def index44_case(client, occurrences, adapter_type):
    """Synthetic all-share catalog/books for the real 44-constituent IMOEX config."""
    prepared = prepare_strategy('INDEX', 'IMOEX')
    leaf = create_strategy(prepared)
    assert len(leaf.config.instruments) == 44
    items = {item.ticker: sdk.Share(
        uid=f'uid-{item.ticker}', ticker=item.ticker, name=item.ticker, class_code='ANY',
        lot=1, currency='rub', api_trade_available_flag=True)
        for item in leaf.config.instruments}
    client.instruments.find_instrument.side_effect = lambda query: search(
        items[query].uid, query=query, kind='share')
    client.instruments.shares.return_value = sdk.SharesResponse(instruments=list(items.values()))
    client.instruments.get_instrument_by.side_effect = lambda **params: sdk.InstrumentResponse(
        instrument=sdk.Instrument(
            uid=params['id'], ticker=params['id'][4:], instrument_type='share',
            class_code='ANY', lot=1, currency='rub', api_trade_available_flag=True))
    client.market_data.get_order_book.side_effect = lambda **params: sdk.GetOrderBookResponse(
        instrument_uid=params['instrument_id'], orderbook_ts=NOW,
        bids=[sdk.Order(price=sdk.Quotation(99, 0), quantity=1)],
        asks=[sdk.Order(price=sdk.Quotation(101, 0), quantity=1)])
    data = adapter_type(client)
    data.valuation_sources = replace(data.valuation_sources, securities=TInvestSecurityData(
        client, clock=lambda: NOW))
    valuation = Valuation(FxSnapshot({}, NOW), data.valuation_sources.securities,
                          clock=lambda: NOW)
    valuation.begin_pass()
    valued = valuation.strategy_data(data)
    valued.begin_snapshot()
    strategy = leaf if occurrences == 1 else CompositeStrategy(PreparedCompositeSource(
        'REPEATED', tuple(PreparedCompositeComponent(
            'INDEX', 'IMOEX', Decimal('0.5'), prepared) for _ in range(occurrences)), Decimal(0)))
    snapshot = strategy.load_snapshot(valued)
    profile = strategy.allocation_profile(snapshot)
    plan = strategy.build_plan(snapshot, StrategyContext(
        (), Decimal('10000000'), Decimal(0), False, (), {}))
    return snapshot, profile, plan


@pytest.mark.parametrize('occurrences,uncached_rpcs', [(1, 133), (2, 177)])
def test_index44_and_repeated_composite_keep_plans_with_strict_rpc_counts(
        client, occurrences, uncached_rpcs):
    """Only repeated Find RPCs disappear; all financial DTOs are identical."""
    reference = index44_case(client, occurrences, UncachedFindData)
    assert client.instruments.find_instrument.call_count == 44 * occurrences
    assert len(client.mock_calls) == uncached_rpcs
    reference_calls = list(client.mock_calls)
    client.reset_mock()

    actual = index44_case(client, occurrences, TInvestStrategyData)

    assert actual == reference
    assert client.instruments.find_instrument.call_args_list == [
        call(query=item.ticker) for item in create_strategy(
            prepare_strategy('INDEX', 'IMOEX')).config.instruments]
    assert client.instruments.shares.call_args_list == [
        call(instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL)]
    assert client.instruments.get_instrument_by.call_count == 44
    assert client.market_data.get_order_book.call_count == 44
    assert len(client.mock_calls) == 133
    assert [item for item in client.mock_calls if item[0] != 'instruments.find_instrument'] == [
        item for item in reference_calls if item[0] != 'instruments.find_instrument']
    assert not client.operations.mock_calls
    assert not client.orders.mock_calls


def test_search_cache_preserves_duplicates_and_returns_independent_lists(client):
    """A consumer can reorder its list without changing another child's candidates."""
    client.instruments.find_instrument.return_value = search('z', 'a', 'z')
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    first = data.find_instruments('GOLD')
    expected = list(first)
    first.reverse()
    first.pop()
    second = data.find_instruments('GOLD')
    assert second == expected
    assert [item.uid for item in second] == ['z', 'a', 'z']
    assert second is not first
    assert client.mock_calls == [call.instruments.find_instrument(query='GOLD')]


def test_search_cache_keys_are_exact_and_snapshot_reset_observes_new_candidates(client):
    """No query normalization, instance sharing or reuse beyond the next pass."""
    client.instruments.find_instrument.side_effect = [
        search('old'), search('lower'), search('spaces'), search('new'), search('other')]
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    assert data.find_instruments('GOLD')[0].uid == 'old'
    assert data.find_instruments('gold')[0].uid == 'lower'
    assert data.find_instruments(' GOLD')[0].uid == 'spaces'
    assert data.find_instruments('GOLD')[0].uid == 'old'
    data.begin_snapshot()
    assert data.find_instruments('GOLD')[0].uid == 'new'
    other = TInvestStrategyData(client)
    other.begin_snapshot()
    assert other.find_instruments('GOLD')[0].uid == 'other'
    assert client.mock_calls == [call.instruments.find_instrument(query=query)
                                 for query in ['GOLD', 'gold', ' GOLD', 'GOLD', 'GOLD']]


def test_search_without_snapshot_stays_fresh(client):
    """Direct adapter reads have no implicit lifetime for a cache."""
    client.instruments.find_instrument.side_effect = [search('old'), search('new')]
    data = TInvestStrategyData(client)
    assert data.find_instruments('GOLD')[0].uid == 'old'
    assert data.find_instruments('GOLD')[0].uid == 'new'
    assert client.mock_calls == [call.instruments.find_instrument(query='GOLD')] * 2


def test_cached_search_restores_catalog_hint_after_interleaved_queries(client):
    """A hit applies its ordered type hints just as another fresh Find response would."""
    client.instruments.find_instrument.side_effect = [search('uid', kind='share'),
                                                      search('uid', kind='etf')]
    client.instruments.shares.return_value = sdk.SharesResponse(instruments=[sdk.Share(
        uid='uid', ticker='GOLD', name='share', class_code='ANY', lot=1, currency='rub',
        api_trade_available_flag=True)])
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    expected = data.find_instruments('GOLD')
    data.find_instruments('alias')
    assert data.find_instruments('GOLD') == expected
    assert data.get_instrument('uid').name == 'share'
    assert client.mock_calls == [call.instruments.find_instrument(query='GOLD'),
                                 call.instruments.find_instrument(query='alias'),
                                 call.instruments.shares(
                                     instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL)]


@pytest.mark.parametrize('failure', ['transport', 'malformed', 'empty'])
def test_failed_or_empty_search_does_not_cache_success(client, failure):
    """A later read must reach the SDK after a failed or negative lookup."""
    failed = {'transport': RequestError(StatusCode.UNAVAILABLE, 'offline', ()),
              'malformed': sdk.FindInstrumentResponse(instruments=[object()]),
              'empty': search()}[failure]
    client.instruments.find_instrument.side_effect = [failed, search('recovered')]
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    if failure == 'empty':
        assert data.find_instruments('GOLD') == []
    else:
        with pytest.raises(DataAccessError if failure == 'transport' else AttributeError):
            data.find_instruments('GOLD')
    assert data.find_instruments('GOLD')[0].uid == 'recovered'
    assert data.find_instruments('GOLD')[0].uid == 'recovered'
    assert client.mock_calls == [call.instruments.find_instrument(query='GOLD')] * 2


@pytest.mark.parametrize('preferred,expected', [
    ('rub', 'rub-first'), ('cny', 'cny-first'), ('usd', 'cny-first')])
def test_cached_candidates_keep_full_currency_priority_and_sdk_order(client, preferred, expected):
    """Find reuse shares no currency/permission flags; first preferred/fallback stays stable."""
    leaf = create_strategy(prepare_strategy('INDEX', 'GOLD'))
    leaf = IndexStrategy(replace(leaf.config, preferred_currency=preferred))
    rows = [('closed', 'rub', False), ('cny-first', 'cny', True),
            ('rub-first', 'rub', True), ('cny-second', 'cny', True), ('rub-second', 'rub', True)]
    client.instruments.find_instrument.return_value = search(*(uid for uid, _, _ in rows))
    client.instruments.etfs.return_value = sdk.EtfsResponse(instruments=[sdk.Etf(
        uid=uid, ticker='GOLD', class_code='ANY', name=uid, currency=currency, lot=1,
        api_trade_available_flag=available) for uid, currency, available in rows])
    client.market_data.get_last_prices.return_value = sdk.GetLastPricesResponse(last_prices=[
        sdk.LastPrice(instrument_uid=expected, price=sdk.Quotation(10, 0))])
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    first = leaf.load_snapshot(data)
    second = leaf.load_snapshot(data)
    assert first == second
    assert first['GOLD'].uid == expected
    assert client.mock_calls == [call.instruments.find_instrument(query='GOLD'),
                                 call.instruments.etfs(
                                     instrument_status=sdk.InstrumentStatus.INSTRUMENT_STATUS_ALL),
                                 call.market_data.get_last_prices(instrument_id=[expected]),
                                 call.market_data.get_last_prices(instrument_id=[expected])]


def test_cached_search_does_not_replace_or_repair_invalid_full_metadata(client):
    """Missing/invalid full responses still fail; a valid later response is reread."""
    client.instruments.find_instrument.return_value = search('uid')
    client.instruments.etfs.return_value = sdk.EtfsResponse(instruments=[])
    valid = sdk.Instrument(uid='uid', ticker='GOLD', name='gold', class_code='ANY',
                           instrument_type='etf', currency='rub', lot=1,
                           api_trade_available_flag=True)
    client.instruments.get_instrument_by.side_effect = [
        sdk.InstrumentResponse(instrument=None),
        sdk.InstrumentResponse(instrument=replace(valid, api_trade_available_flag=None)),
        sdk.InstrumentResponse(instrument=valid)]
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    for message in ['instrument is missing', 'api_trade_available_flag must be bool']:
        assert data.find_instruments('GOLD')[0].uid == 'uid'
        with pytest.raises(ValueError, match=message):
            data.get_instrument('uid')
    assert data.find_instruments('GOLD')[0].uid == 'uid'
    assert data.get_instrument('uid').currency == 'rub'
    assert client.instruments.find_instrument.call_count == 1
    assert client.instruments.get_instrument_by.call_args_list == [call(
        id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')] * 3


def test_search_cache_preserves_execution_sources_and_account_caps(client):
    """Existing snapshot metadata reuse does not suppress fresh status or account limits."""
    client.instruments.find_instrument.return_value = search('uid')
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    data.find_instruments('GOLD')
    data.find_instruments('GOLD')
    client.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', lot=1, currency='rub', instrument_type='etf',
                                  api_trade_available_flag=True))
    client.market_data.get_trading_status.side_effect = [sdk.GetTradingStatusResponse(
        instrument_uid='uid', api_trade_available_flag=flag,
        bestprice_order_available_flag=flag) for flag in [True, False, True]]
    client.orders.get_max_lots.side_effect = [sdk.GetMaxLotsResponse(
        currency='rub', buy_limits=sdk.BuyLimitsView(
            buy_money_amount=sdk.Quotation(cap, 0), buy_max_lots=cap),
        sell_limits=sdk.SellLimitsView(sell_max_lots=cap)) for cap in [100, 0, 7]]
    execution = TInvestExecutionData(client, data)
    before = execution.get_trade_rules('dst', ['uid'])['uid']
    after = execution.get_trade_rules('dst', ['uid'])['uid']
    unrelated = execution.get_trade_rules('other', ['uid'])['uid']
    assert before.api_trade_available and before.bestprice_order_available
    assert not after.api_trade_available and not after.bestprice_order_available
    assert (before.buy_max_lots, after.buy_max_lots, unrelated.buy_max_lots) == (100, 0, 7)
    assert unrelated.api_trade_available and unrelated.bestprice_order_available
    assert client.instruments.find_instrument.call_count == 1
    assert client.instruments.get_instrument_by.call_args_list == [call(
        id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')]
    assert client.market_data.get_trading_status.call_count == 3
    assert [item.kwargs['request'].account_id for item in
            client.orders.get_max_lots.call_args_list] == ['dst', 'dst', 'other']
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('currency', [None, '', 'invalid'])
def test_cached_find_never_supplies_missing_full_currency(client, currency):
    """Invalid full metadata still fails each load before prices or effects."""
    client.instruments.find_instrument.return_value = search('uid')
    client.instruments.etfs.return_value = sdk.EtfsResponse(instruments=[sdk.Etf(
        uid='uid', ticker='GOLD', name='gold', class_code='ANY', lot=1,
        currency=currency, api_trade_available_flag=True)])
    leaf = create_strategy(prepare_strategy('INDEX', 'GOLD'))
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    for _ in range(2):
        with pytest.raises(ValueError, match='currency'):
            leaf.load_snapshot(data)
    assert client.instruments.find_instrument.call_count == 1
    assert client.instruments.etfs.call_count == 1
    client.market_data.get_last_prices.assert_not_called()
    assert not client.orders.mock_calls


def test_each_sync_invalidates_search_and_full_metadata(client):
    """The engine's real snapshot boundary reselects the UID and reloads its catalog."""
    client.instruments.find_instrument.side_effect = [search('old'), search('new')]
    client.instruments.etfs.side_effect = [sdk.EtfsResponse(instruments=[sdk.Etf(
        uid=uid, ticker='GOLD', name=uid, class_code='ANY', lot=1, currency=currency,
        api_trade_available_flag=True)]) for uid, currency in [('old', 'rub'), ('new', 'cny')]]
    client.market_data.get_last_prices.side_effect = [sdk.GetLastPricesResponse(last_prices=[
        sdk.LastPrice(instrument_uid=uid, price=sdk.Quotation(price, 0))])
        for uid, price in [('old', 10), ('new', 20)]]
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    execution.get_destination.side_effect = RuntimeError('stop before effects')
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    data = TInvestStrategyData(client)
    engine = AutoRepeater(create_strategy(prepare_strategy('INDEX', 'GOLD')),
                          data, execution, executor)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='stop before effects'):
            engine.sync_accounts('dst')
    assert data.get_instrument('new').currency == 'cny'
    assert client.instruments.find_instrument.call_args_list == [call(query='GOLD')] * 2
    assert client.instruments.etfs.call_count == 2
    assert client.market_data.get_last_prices.call_args_list == [
        call(instrument_id=['old']), call(instrument_id=['new'])]
    assert not executor.method_calls


def test_security_refresh_keeps_independent_full_metadata_and_120s_guard(client):
    """Book refresh rereads Instrument and market time despite warm Find/catalog data."""
    client.instruments.find_instrument.return_value = search('uid')
    client.instruments.etfs.return_value = sdk.EtfsResponse(instruments=[sdk.Etf(
        uid='uid', ticker='GOLD', name='gold', class_code='ANY', lot=1, currency='rub',
        api_trade_available_flag=True)])
    client.instruments.get_instrument_by.side_effect = [sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', currency=currency, instrument_type='etf'))
        for currency in ['rub', 'cny', 'cny']]
    client.market_data.get_order_book.side_effect = [sdk.GetOrderBookResponse(
        instrument_uid='uid', orderbook_ts=NOW - timedelta(seconds=age),
        bids=[sdk.Order(price=sdk.Quotation(99, 0), quantity=1)],
        asks=[sdk.Order(price=sdk.Quotation(101, 0), quantity=1)]) for age in [0, 120, 121]]
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    data.find_instruments('GOLD')
    assert data.get_instrument('uid').currency == 'rub'
    valuation = Valuation(FxSnapshot({}, NOW), TInvestSecurityData(client, clock=lambda: NOW),
                          clock=lambda: NOW)
    assert valuation.security('uid').currency == 'rub'
    valuation.refresh_books()
    assert valuation.security('uid').currency == 'cny'
    valuation.refresh_books()
    with pytest.raises(ValueError, match='stale market time'):
        valuation.security('uid')
    assert client.instruments.get_instrument_by.call_args_list == [call(
        id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')] * 3
    assert client.market_data.get_order_book.call_args_list == [
        call(instrument_id='uid', depth=1)] * 3
    assert not client.orders.mock_calls


def test_search_reuse_does_not_mix_portfolios_or_currency_identities(client):
    """ACCOUNT reads and CurrencyBy remain fresh and distinct from Find DTOs."""
    client.instruments.find_instrument.return_value = search('cash', kind='currency')
    client.operations.get_portfolio.side_effect = [sdk.PortfolioResponse(positions=[
        sdk.PortfolioPosition(instrument_uid=uid, instrument_type='currency',
                              current_price=sdk.MoneyValue(currency, 1, 0),
                              quantity=sdk.Quotation(quantity, 0))])
        for uid, currency, quantity in [
            ('cash', 'rub', 100), ('other', 'cny', 7), ('cash', 'rub', 90)]]
    client.instruments.currency_by.side_effect = [sdk.CurrencyResponse(instrument=sdk.Currency(
        uid=uid, iso_currency_name=currency))
        for uid, currency in [('cash', 'rub'), ('other', 'cny')]]
    data = TInvestStrategyData(client)
    data.begin_snapshot()
    data.find_instruments('cash')
    data.find_instruments('cash')
    portfolios = [data.get_portfolio(account) for account in ['source', 'other', 'source']]
    assert [(item.positions[0].currency, item.positions[0].quantity) for item in portfolios] == [
        ('rub', Decimal(100)), ('cny', Decimal(7)), ('rub', Decimal(90))]
    assert [data.valuation_sources.securities.get_cash_currency(uid) for uid in
            ['cash', 'other']] == ['rub', 'cny']
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id=account) for account in ['source', 'other', 'source']]
    assert client.instruments.currency_by.call_args_list == [call(
        id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid) for uid in ['cash', 'other']]
    assert client.instruments.find_instrument.call_count == 1
