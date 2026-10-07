"""Index constituents are selected by API availability, not a board whitelist."""
from dataclasses import replace
from decimal import Decimal
import logging
from test.test_autorepeater import client_tinvest  # pylint: disable=unused-import
from unittest.mock import call, create_autospec

import pytest
from t_tech import invest

from autorepeater import strategies
from autorepeater.index_strategy import IndexStrategy
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_data import (
    DataAccessError, InstrumentInfo, InstrumentMatch, InstrumentType, PriceQuote, StrategyData,
)
from autorepeater.tinvest_strategy_data import TInvestStrategyData


def selection_case(rows):
    """Use a real one-ticker config with explicit SDK-independent candidate metadata."""
    strategy = strategies.create_strategy(strategies.prepare_strategy('INDEX', 'GOLD'))
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = [
        InstrumentMatch(item.uid, item.ticker, item.name, item.instrument_type, item.class_code)
        for item in rows]
    by_uid = {item.uid: item for item in rows}
    data.get_instrument.side_effect = by_uid.__getitem__
    data.get_last_prices.side_effect = lambda uids: [PriceQuote(uid, Decimal('10'), None)
                                                    for uid in uids]
    return strategy, data


def instrument(uid, board, available):
    """Availability is a required bool from full metadata, not the search response."""
    return InstrumentInfo(uid, 'GOLD', 'Gold', InstrumentType.ETF, board, 1, 'RUB', available)


@pytest.mark.parametrize('board', ['TQBR', 'TQTF', 'NEW_BOARD', 'PTEQ'])
def test_any_board_is_eligible_when_api_tradable(board, caplog):
    """Board codes are descriptive metadata and cannot veto an available fund."""
    old = instrument('old', 'TQTF', False)
    chosen = instrument('chosen', board, True)
    strategy, data = selection_case([old, chosen])
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        snapshot = strategy.load_snapshot(data)
    assert snapshot['GOLD'].uid == 'chosen'
    assert data.get_instrument.call_args_list == [call('old'), call('chosen')]
    data.get_last_prices.assert_called_once_with(['chosen'])
    assert any('Skipping index candidate: GOLD uid=old' in message
               and 'api_trade_available=False' in message for message in caplog.messages)
    assert any(f'Selected index instrument: GOLD uid=chosen class_code={board}' in message
               for message in caplog.messages)
    assert all(record.levelno == logging.INFO for record in caplog.records)


def test_unavailable_retired_candidate_does_not_require_valid_lot():
    """A closed venue's obsolete lot must not prevent choosing the available venue."""
    strategy, data = selection_case([
        replace(instrument('old', 'RETIRED', False), lot=0), instrument('new', 'CURRENT', True)])
    assert strategy.load_snapshot(data)['GOLD'].uid == 'new'


@pytest.mark.parametrize('available', [[], [False], [False, False]])
def test_no_unique_available_candidate_fails_before_quotes(available):
    """A retired candidate must not silently replace an unavailable ticker."""
    rows = [instrument(f'uid-{index}', f'BOARD-{index}', flag)
            for index, flag in enumerate(available)]
    strategy, data = selection_case(rows)
    with pytest.raises(ValueError, match='index instrument: GOLD.*available via API') as error:
        strategy.load_snapshot(data)
    for item in rows:
        assert item.uid in str(error.value)
        assert item.class_code in str(error.value)
    data.get_last_prices.assert_not_called()


def test_multiple_available_candidates_choose_first_in_search_order_with_warning(caplog):
    """The first API-enabled candidate wins, with all live UIDs/boards in the warning."""
    rows = [instrument('closed', 'TQTF', False), instrument('z-first', 'NEW', True),
            instrument('a-second', 'TQBR', True)]
    strategy, data = selection_case(rows)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        snapshot = strategy.load_snapshot(data)
    assert snapshot['GOLD'].uid == 'z-first'
    data.get_last_prices.assert_called_once_with(['z-first'])
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].message == (
        'Multiple API-tradable index instruments: GOLD; '
        'selected first uid=z-first class_code=NEW; candidates: z-first/NEW, a-second/TQBR')


@pytest.mark.parametrize('available', [None, 'true', 'false', 1, 0, object()])
def test_own_port_requires_explicit_boolean_availability(available):
    """Unknown availability is a data error, never truthiness-based permission."""
    strategy, data = selection_case([instrument('uid', 'ANY', available)])
    with pytest.raises(ValueError, match='api_trade_available.*GOLD.*uid'):
        strategy.load_snapshot(data)
    data.get_last_prices.assert_not_called()


def test_new_snapshot_reconsiders_uid_and_board():
    """The venue is resolved on every sync, with no UID or board cache."""
    rows = [instrument('old', 'OLD', True), instrument('new', 'NEW', False)]
    strategy, data = selection_case(rows)
    first = strategy.load_snapshot(data)
    second_rows = {item.uid: replace(item, api_trade_available=not item.api_trade_available)
                   for item in rows}
    data.get_instrument.side_effect = second_rows.__getitem__
    second = strategy.load_snapshot(data)
    assert first['GOLD'].uid == 'old'
    assert second['GOLD'].uid == 'new'
    assert data.get_last_prices.call_args_list == [call(['old']), call(['new'])]


def test_available_alternative_does_not_mask_transport_failure():
    """An incomplete candidate lookup cannot prove that a choice is unambiguous."""
    strategy, data = selection_case([
        instrument('ok', 'ANY', True), instrument('failed', 'OTHER', False)])
    data.get_instrument.side_effect = [
        instrument('ok', 'ANY', True), DataAccessError('read failed')]
    with pytest.raises(DataAccessError, match='read failed'):
        strategy.load_snapshot(data)
    data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('src', ['GOLD', 'OBLG'])
def test_closed_tqtf_fund_is_replaced_by_api_available_tqbr(client, src):
    """Reproduce fund migration end to end; all API calls and orders are mocked."""
    strategy = strategies.create_strategy(strategies.prepare_strategy('INDEX', src))
    assert isinstance(strategy, IndexStrategy)
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = invest.FindInstrumentResponse(instruments=[
        invest.InstrumentShort(uid=uid, ticker=src, instrument_type='etf', class_code=board,
                               api_trade_available_flag=short_flag)
        for uid, board, short_flag in [('old', 'TQTF', 'true'), ('new', 'TQBR', 'false')]])
    metadata = {
        uid: invest.InstrumentResponse(instrument=invest.Instrument(
            uid=uid, ticker=src, name=src, instrument_type='etf', class_code=board,
            lot=1, currency='RUB', api_trade_available_flag=available, trading_status=status))
        for uid, board, available, status in [
            ('old', 'TQTF', False,
             invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING),
            ('new', 'TQBR', True,
             invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING)]}
    client.instruments.get_instrument_by.side_effect = lambda **params: metadata[params['id']]
    client.market_data.get_last_prices.return_value = invest.GetLastPricesResponse(last_prices=[
        invest.LastPrice(instrument_uid='new', price=invest.Quotation(10, 0))])
    client.operations.get_portfolio.side_effect = None
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(instrument_type='currency', quantity=invest.Quotation(100, 0),
                                 current_price=invest.MoneyValue('RUB', 1, 0))])
    from autorepeater.execution_data import ExecutionSnapshot, TradeRules  # pylint: disable=import-outside-toplevel
    from autorepeater.order_execution import TInvestOrderExecutor  # pylint: disable=import-outside-toplevel
    from autorepeater.strategy_data import PortfolioSnapshot  # pylint: disable=import-outside-toplevel
    from unittest.mock import Mock, ANY  # pylint: disable=import-outside-toplevel
    execution = Mock(spec_set=['get_destination', 'get_trade_rules'])
    execution.get_destination.return_value = ExecutionSnapshot(
        PortfolioSnapshot(()), Decimal(100), {}, {}, {'rub': Decimal(100)}, {}, (), True)
    execution.get_trade_rules.return_value = {
        'new': TradeRules(1, 'rub', True, True, Decimal(100), 100, 0)}
    client.orders.post_order.return_value = invest.PostOrderResponse(
        instrument_uid='new', direction=invest.OrderDirection.ORDER_DIRECTION_BUY,
        order_id='offline', lots_requested=9, lots_executed=9,
        execution_report_status=invest.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL)
    engine = AutoRepeater(
        strategy, TInvestStrategyData(client), execution, TInvestOrderExecutor(client))
    engine.sync_accounts('dst')
    client.market_data.get_last_prices.assert_called_once_with(instrument_id=['new'])
    assert client.instruments.get_instrument_by.call_args_list == [
        call(id_type=invest.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        for uid in ('old', 'new')]
    client.orders.post_order.assert_called_once_with(
        instrument_id='new', quantity=9, direction=invest.OrderDirection.ORDER_DIRECTION_BUY,
        account_id='dst', order_type=invest.OrderType.ORDER_TYPE_BESTPRICE, order_id=ANY)


@pytest.mark.parametrize('available', [True, False])
def test_adapter_preserves_full_instrument_availability(client, available):
    """Only the SDK adapter knows the broker field name."""
    client.instruments.get_instrument_by.side_effect = None
    client.instruments.get_instrument_by.return_value = invest.InstrumentResponse(
        instrument=invest.Instrument(uid='uid', ticker='GOLD', name='Gold',
                                     instrument_type='etf', class_code='NEW', lot=1,
                                     currency='RUB', api_trade_available_flag=available))
    result = TInvestStrategyData(client).get_instrument('uid')
    assert result.api_trade_available is available


@pytest.mark.parametrize('available', [None, 'true', 'false', 0, 1, object()])
def test_adapter_rejects_unknown_api_permission(client, available):
    """A string false is truthy in Python; never turn it into permission to trade."""
    client.instruments.get_instrument_by.side_effect = None
    client.instruments.get_instrument_by.return_value = invest.InstrumentResponse(
        instrument=invest.Instrument(uid='uid', api_trade_available_flag=available))
    with pytest.raises(ValueError, match='uid: api_trade_available_flag must be bool'):
        TInvestStrategyData(client).get_instrument('uid')
