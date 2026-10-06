"""Metadata catalogs avoid per-board API quota exhaustion without stale snapshots."""
import inspect
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from grpc import StatusCode
from t_tech import invest
from t_tech.invest.services import InstrumentsService, MarketDataService

from autorepeater import strategies
from autorepeater.execution_data import ExecutionData, ExecutionSnapshot, TradeRules
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_data import DataAccessError, PortfolioSnapshot
from autorepeater.tinvest_strategy_data import TInvestStrategyData


@pytest.fixture(name='catalog_client')
def fixture_catalog_client():
    """Use SDK service signatures and explicit full catalog DTOs, without network."""
    return SimpleNamespace(
        instruments=create_autospec(
            inspect.unwrap(InstrumentsService), instance=True, spec_set=True),
        market_data=create_autospec(
            inspect.unwrap(MarketDataService), instance=True, spec_set=True))


def configure_catalog(client, strategy):
    """Seven boards per constituent exceed the old 200/min GetInstrumentBy quota."""
    shares, etfs, short = [], [], {}
    for child in strategy.children:
        for item in child.config.instruments:
            kind = 'etf' if item.ticker in ('GOLD', 'OBLG') else 'share'
            rows = []
            for number in range(7):
                row = (invest.Etf if kind == 'etf' else invest.Share)(
                    uid=f'{item.ticker}-{number}', ticker=item.ticker, name=item.ticker,
                    class_code=f'BOARD{number}', lot=1, currency='rub',
                    api_trade_available_flag=number == 1)
                (etfs if kind == 'etf' else shares).append(row)
                rows.append(invest.InstrumentShort(
                    uid=row.uid, ticker=row.ticker, name=row.name, class_code=row.class_code,
                    instrument_type=kind, api_trade_available_flag=number != 1))
            short[item.ticker] = rows
    client.instruments.shares.return_value = invest.SharesResponse(instruments=shares)
    client.instruments.etfs.return_value = invest.EtfsResponse(instruments=etfs)
    client.instruments.find_instrument.side_effect = lambda query: invest.FindInstrumentResponse(
        instruments=short[query])
    all_rows = {row.uid: row for row in shares + etfs}

    def individual(**kwargs):
        if client.instruments.get_instrument_by.call_count > 200:
            raise invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, '200 per minute', None)
        row = all_rows[kwargs['id']]
        return invest.InstrumentResponse(instrument=invest.Instrument(
            uid=row.uid, ticker=row.ticker, name=row.name, class_code=row.class_code,
            instrument_type='etf' if row.ticker in ('GOLD', 'OBLG') else 'share',
            lot=row.lot, currency=row.currency,
            api_trade_available_flag=row.api_trade_available_flag))

    client.instruments.get_instrument_by.side_effect = individual
    client.market_data.get_last_prices.side_effect = lambda instrument_id: (
        invest.GetLastPricesResponse(
            last_prices=[invest.LastPrice(instrument_uid=uid, price=invest.Quotation(100, 0))
                         for uid in instrument_id]))
    return shares, etfs


def test_cloud_composite_avoids_per_instrument_quota(catalog_client):
    """A real BALANCED calculation uses two catalogs for over 300 board candidates."""
    strategy = strategies.create_strategy(strategies.prepare_strategy('COMPOSITE', 'BALANCED'))
    configure_catalog(catalog_client, strategy)
    data = TInvestStrategyData(catalog_client)
    reads = create_autospec(ExecutionData, instance=True, spec_set=True)
    reads.get_destination.return_value = ExecutionSnapshot(
        PortfolioSnapshot(()), Decimal(154000), {}, {}, {'rub': Decimal(154000)}, {}, (), True)
    reads.get_trade_rules.side_effect = lambda account_id, uids: {
        uid: TradeRules(1, 'rub', True, True, Decimal(154000), 100000, 0) for uid in uids}
    engine = AutoRepeater(strategy, data, reads, None)
    engine.set_debug(True)
    with patch('autorepeater.tinvest_requests.time.sleep',
               side_effect=AssertionError('metadata quota exhausted')):
        engine.sync_accounts('dst')
    catalog_client.instruments.get_instrument_by.assert_not_called()
    catalog_client.instruments.shares.assert_called_once_with(
        instrument_status=invest.InstrumentStatus.INSTRUMENT_STATUS_ALL)
    catalog_client.instruments.etfs.assert_called_once_with(
        instrument_status=invest.InstrumentStatus.INSTRUMENT_STATUS_ALL)
    assert reads.get_trade_rules.call_args.args[1]


def test_duplicate_catalog_uid_is_a_data_error(catalog_client):
    """A corrupt full catalog fails explicitly rather than replacing one venue."""
    data = TInvestStrategyData(catalog_client)
    data.begin_snapshot()
    row = invest.Share(uid='uid', ticker='TEST', name='Test', class_code='ANY', lot=1,
                       currency='rub', api_trade_available_flag=True)
    catalog_client.instruments.shares.return_value = invest.SharesResponse(instruments=[row, row])
    catalog_client.instruments.find_instrument.return_value = invest.FindInstrumentResponse(
        instruments=[invest.InstrumentShort(uid='uid', instrument_type='share')])
    data.find_instruments('TEST')
    with pytest.raises(ValueError, match='duplicate instrument catalog UID: uid'):
        data.get_instrument('uid')
    catalog_client.instruments.get_instrument_by.assert_not_called()


def test_direct_metadata_is_cached_only_within_pass(catalog_client):
    """Unknown portfolio UIDs use the direct endpoint once, not on each BUY refresh."""
    data = TInvestStrategyData(catalog_client)
    row = invest.Instrument(uid='uid', ticker='UNKNOWN', name='Unknown', class_code='ANY',
                            instrument_type='bond', lot=1, currency='rub',
                            api_trade_available_flag=True)
    catalog_client.instruments.get_instrument_by.return_value = invest.InstrumentResponse(
        instrument=row)
    data.begin_snapshot()
    first = data.get_instrument('uid')
    assert data.get_instrument('uid') is first
    catalog_client.instruments.get_instrument_by.assert_called_once_with(
        id_type=invest.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')
    row.api_trade_available_flag = False
    data.begin_snapshot()
    assert data.get_instrument('uid').api_trade_available is False
    assert catalog_client.instruments.get_instrument_by.call_count == 2
    catalog_client.instruments.shares.assert_not_called()
    catalog_client.instruments.etfs.assert_not_called()


def test_catalog_refreshes_between_passes(catalog_client):
    """An API-enabled board changes on the next snapshot, not after a TTL."""
    strategy = strategies.create_strategy(strategies.prepare_strategy('COMPOSITE', 'BALANCED'))
    shares, etfs = configure_catalog(catalog_client, strategy)
    data = TInvestStrategyData(catalog_client)
    data.begin_snapshot()
    first = strategy.load_snapshot(data)
    for row in shares + etfs:
        row.api_trade_available_flag = row.uid.endswith('-2')
    data.begin_snapshot()
    second = strategy.load_snapshot(data)
    for before, after in zip(first.children, second.children):
        assert all(quote.uid.endswith('-1') for quote in before.values())
        assert all(quote.uid.endswith('-2') for quote in after.values())
    assert catalog_client.instruments.shares.call_count == 2
    assert catalog_client.instruments.etfs.call_count == 2


def test_catalog_transport_failure_is_not_silently_bypassed(catalog_client):
    """A failed bulk lookup must not fall back to guessed or incomplete metadata."""
    data = TInvestStrategyData(catalog_client)
    data.begin_snapshot()
    catalog_client.instruments.find_instrument.return_value = invest.FindInstrumentResponse(
        instruments=[invest.InstrumentShort(uid='uid', instrument_type='share')])
    data.find_instruments('ticker')
    error = invest.RequestError(StatusCode.UNAVAILABLE, 'transport', None)
    catalog_client.instruments.shares.side_effect = error
    with pytest.raises(DataAccessError) as captured:
        data.get_instrument('uid')
    assert captured.value.__cause__ is error
    catalog_client.instruments.get_instrument_by.assert_not_called()


@pytest.mark.parametrize('endpoint', ['shares', 'etfs', 'get_instrument_by',
                                     'find_instrument', 'get_last_prices'])
def test_strategy_sdk_reads_retry_quota_in_shared_wrapper(catalog_client, endpoint):
    """Each source read, including bulk metadata, reaches the quota wrapper."""
    data = TInvestStrategyData(catalog_client)
    fields = {'uid': 'uid', 'ticker': 'TEST', 'name': 'Test', 'class_code': 'ANY',
              'lot': 1, 'currency': 'rub', 'api_trade_available_flag': True}
    if endpoint in ('shares', 'etfs'):
        kind = 'share' if endpoint == 'shares' else 'etf'
        data.begin_snapshot()
        catalog_client.instruments.find_instrument.return_value = invest.FindInstrumentResponse(
            instruments=[invest.InstrumentShort(uid='uid', instrument_type=kind)])
        data.find_instruments('TEST')
        response = (invest.SharesResponse(instruments=[invest.Share(**fields)])
                    if kind == 'share' else invest.EtfsResponse(instruments=[invest.Etf(**fields)]))
        action, argument = data.get_instrument, 'uid'
    elif endpoint == 'get_instrument_by':
        response = invest.InstrumentResponse(instrument=invest.Instrument(
            **fields, instrument_type='share'))
        action, argument = data.get_instrument, 'uid'
    elif endpoint == 'find_instrument':
        response = invest.FindInstrumentResponse(instruments=[])
        action, argument = data.find_instruments, 'TEST'
    else:
        response = invest.GetLastPricesResponse(last_prices=[])
        action, argument = data.get_last_prices, []
    service = (catalog_client.market_data if endpoint == 'get_last_prices'
               else catalog_client.instruments)
    method = getattr(service, endpoint)
    method.side_effect = [invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None),
                          response]
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep:
        action(argument)
    sleep.assert_called_once_with(10)
    assert method.call_args_list == [method.call_args_list[0]] * 2
