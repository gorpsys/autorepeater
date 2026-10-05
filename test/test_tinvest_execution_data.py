"""Execution reads use documented own limits, never estimated sale proceeds."""
import inspect
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import schemas as sdk
from t_tech.invest import RequestError
from t_tech.invest.services import MarketDataService, OperationsService, OrdersService

from autorepeater.execution_data import ExecutionDataError
from autorepeater.strategy_data import (
    DataAccessError, InstrumentInfo, InstrumentType, PortfolioEntry,
    PortfolioSnapshot, StrategyData,
)
from autorepeater.tinvest_execution_data import TInvestExecutionData
from autorepeater.tinvest_strategy_data import TInvestStrategyData

D = Decimal


def money(value=0, currency='rub'):
    """Construct explicit native currency values."""
    return sdk.MoneyValue(currency=currency, units=value, nano=0)


def entry(uid='uid', quantity='20', price='5', currency='rub'):
    """Neutral portfolio uses pieces and per-piece prices."""
    return PortfolioEntry(uid, InstrumentType.SHARE, currency, D(price), D(quantity),
                          '', False, D(0))


@pytest.fixture(name='reads')
def fixture_reads():
    """Use unwrapped SDK service specs without a Client or network."""
    client = Mock(spec_set=['operations', 'orders', 'market_data'])
    for name, service in [('operations', OperationsService), ('orders', OrdersService),
                          ('market_data', MarketDataService)]:
        setattr(client, name, create_autospec(inspect.unwrap(service), instance=True,
                                            spec_set=True))
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((entry(),))
    data.get_instrument.return_value = InstrumentInfo(
        'uid', 'T', 'Name', InstrumentType.SHARE, 'ANY', 10, 'rub', True)
    client.operations.get_positions.return_value = sdk.PositionsResponse(
        money=[money(120)], blocked=[money(0)], securities=[], futures=[], options=[],
        limits_loading_in_progress=False)
    client.orders.get_orders.return_value = sdk.GetOrdersResponse(orders=[])
    client.orders.get_max_lots.return_value = sdk.GetMaxLotsResponse(
        currency='rub', buy_limits=sdk.BuyLimitsView(
            buy_money_amount=sdk.Quotation(units=75, nano=500000000),
            buy_max_lots=1, buy_max_market_lots=1000),
        sell_limits=sdk.SellLimitsView(sell_max_lots=2),
        buy_margin_limits=sdk.BuyLimitsView(buy_max_lots=9000),
        sell_margin_limits=sdk.SellLimitsView(sell_max_lots=9000))
    client.market_data.get_trading_status.return_value = sdk.GetTradingStatusResponse(
        instrument_uid='uid', api_trade_available_flag=True,
        bestprice_order_available_flag=True,
        trading_status=sdk.SecurityTradingStatus.SECURITY_TRADING_STATUS_UNSPECIFIED,
        market_order_available_flag=False, limit_order_available_flag=False)
    return client, data, TInvestExecutionData(client, data)


def test_budget_keeps_nano_per_position_and_duplicate_pieces(reads):
    """Cash availability does not inflate B or change per-position rounding."""
    client, data, adapter = reads
    data.get_portfolio.return_value = PortfolioSnapshot((
        entry(quantity='0.5', price='0.000000001'),
        entry(quantity='0.5', price='0.000000001'),
        PortfolioEntry('cash', InstrumentType.CURRENCY, 'rub', D(1), D(120), '', False, D(0)),
    ))
    result = adapter.get_destination('account')
    assert result.budget == D(120)
    assert result.quantities == {'uid': D(1)}
    assert result.marks == {'uid': D('0.000000001')}
    assert result.available_quantities == result.quantities
    assert result.available_cash == {'rub': D(120)}
    assert result.limits_ready
    data.get_portfolio.assert_called_once_with('account')
    client.operations.get_positions.assert_called_once_with(account_id='account')
    client.orders.get_orders.assert_called_once_with(account_id='account')
    client.orders.get_max_lots.assert_not_called()


@pytest.mark.parametrize('kind', [InstrumentType.SHARE, InstrumentType.CURRENCY])
def test_nonrub_valuation_rejected_before_availability_reads(reads, kind):
    """The valuation currency must match B; unsupported holdings cannot disappear."""
    client, data, adapter = reads
    data.get_portfolio.return_value = PortfolioSnapshot((
        entry('rub-stock', quantity='1000', price='1'),
        replace(entry('foreign', quantity='1', price='100', currency='usd'),
                instrument_type=kind)))
    with pytest.raises(ValueError, match='foreign.*valuation.*rub'):
        adapter.get_destination('account')
    assert client.mock_calls == []


def test_usd_instrument_with_rub_valuation_keeps_full_budget(reads):
    """Portfolio current_price.currency is independent of native trading currency."""
    _, data, adapter = reads
    data.get_instrument.return_value = replace(data.get_instrument.return_value, currency='usd')
    result = adapter.get_destination('account')
    assert result.budget == D(100)
    assert result.quantities == {'uid': D(20)}
    data.get_instrument.assert_not_called()


@pytest.mark.parametrize('change', [{'current_price': D(6)}, {'currency': 'usd'},
                                   {'instrument_type': InstrumentType.ETF}])
def test_incompatible_duplicate_is_rejected(reads, change):
    """Duplicate aggregation cannot silently choose a mark or currency."""
    _, data, adapter = reads
    data.get_portfolio.return_value = PortfolioSnapshot((entry(), replace(entry(), **change)))
    with pytest.raises(ValueError, match='uid.*incompatible'):
        adapter.get_destination('account')


@pytest.mark.parametrize('guard', ['loading', 'money', 'security', 'exchange', 'future',
                                 'option', 'portfolio_flag', 'portfolio_number', 'new', 'partial'])
def test_any_blocking_disables_availability(reads, guard):
    """No balance units or money-minus-blocked arithmetic is needed."""
    client, data, adapter = reads
    positions = client.operations.get_positions.return_value
    if guard == 'loading':
        positions.limits_loading_in_progress = True
    elif guard == 'money':
        positions.blocked = [money(25)]
    elif guard in ('security', 'exchange'):
        positions.securities = [sdk.PositionsSecurities(
            instrument_uid='uid', blocked=3 if guard == 'security' else 0,
            balance=999999, exchange_blocked=guard == 'exchange')]
    elif guard == 'future':
        positions.futures = [sdk.PositionsFutures(instrument_uid='future', blocked=1)]
    elif guard == 'option':
        positions.options = [sdk.PositionsOptions(instrument_uid='option', blocked=1)]
    elif guard.startswith('portfolio'):
        data.get_portfolio.return_value = PortfolioSnapshot((replace(
            entry(), blocked=guard == 'portfolio_flag',
            blocked_lots=D(1) if guard == 'portfolio_number' else D(0)),))
    else:
        status = (sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW if guard == 'new'
                  else sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL)
        client.orders.get_orders.return_value.orders = [sdk.OrderState(
            order_id='order', instrument_uid='uid', execution_report_status=status,
            lots_requested=2, lots_executed=0 if guard == 'new' else 1)]
    result = adapter.get_destination('account')
    assert not result.limits_ready
    assert result.available_cash == {'rub': D(0)}
    assert result.available_quantities == {'uid': D(0)}
    assert result.budget == D(100)


def test_native_own_caps_not_market_or_margin_and_no_lot_multiplication(reads):
    """BESTPRICE permission and own caps are separate from market flags."""
    client, data, adapter = reads
    rules = adapter.get_trade_rules('account', ['uid', 'uid'])['uid']
    assert (rules.lot, rules.currency) == (10, 'rub')
    assert (rules.buy_money_amount, rules.buy_max_lots, rules.sell_max_lots) == (D('75.5'), 1, 2)
    assert rules.api_trade_available and rules.bestprice_order_available
    request = client.orders.get_max_lots.call_args.kwargs['request']
    assert isinstance(request, sdk.GetMaxLotsRequest)
    assert (request.account_id, request.instrument_id) == ('account', 'uid')
    client.orders.get_max_lots.assert_called_once()
    data.get_instrument.assert_called_once_with('uid')
    client.market_data.get_trading_status.assert_called_once_with(instrument_id='uid')
    assert adapter.get_trade_rules('other', []) == {}


@pytest.mark.parametrize('field', ['api_trade_available_flag', 'bestprice_order_available_flag'])
def test_false_permission_is_preserved(reads, field):
    """Current API and BESTPRICE flags gate independently of board enums."""
    client, _, adapter = reads
    setattr(client.market_data.get_trading_status.return_value, field, False)
    rules = adapter.get_trade_rules('account', ['uid'])['uid']
    assert not (rules.api_trade_available and rules.bestprice_order_available)


@pytest.mark.parametrize('value', [None, True, -1, D('NaN'), D('Infinity'), 'broken'])
@pytest.mark.parametrize('field', ['buy_max_lots', 'sell_max_lots'])
def test_invalid_caps(reads, field, value):
    """Missing or noninteger lot caps cannot be treated as unlimited."""
    client, _, adapter = reads
    response = client.orders.get_max_lots.return_value
    setattr(response.buy_limits if field.startswith('buy') else response.sell_limits,
            field, value)
    with pytest.raises(ValueError, match=field):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('value', [None, True, 'NaN', 'Infinity', 'broken'])
def test_invalid_native_money(reads, value):
    """The explicit availability quotation is validated before use."""
    client, _, adapter = reads
    client.orders.get_max_lots.return_value.buy_limits.buy_money_amount.units = value
    with pytest.raises(ValueError, match='buy_money_amount'):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('operation', [
    'positions', 'orders', 'status', 'caps', 'portfolio', 'metadata',
])
def test_transport_errors_preserve_cause_without_retry(reads, operation):
    """A neutral error does not conceal transport failure or resend anything."""
    client, data, adapter = reads
    error = RequestError(StatusCode.UNAVAILABLE, 'synthetic failure', ())
    targets = {'positions': client.operations.get_positions, 'orders': client.orders.get_orders,
               'status': client.market_data.get_trading_status, 'caps': client.orders.get_max_lots,
               'portfolio': data.get_portfolio, 'metadata': data.get_instrument}
    if operation in ('portfolio', 'metadata'):
        error = DataAccessError('translated failure')
    targets[operation].side_effect = error
    with pytest.raises(ExecutionDataError) as raised:
        if operation in ('positions', 'orders', 'portfolio'):
            adapter.get_destination('account')
        else:
            adapter.get_trade_rules('account', ['uid'])
    assert raised.value.__cause__ is error
    assert targets[operation].call_count == 1
    client.orders.post_order.assert_not_called()


def test_sdk_portfolio_translation_retains_blocking_and_pieces(reads):
    """Destination uses the existing translator; blocked balance is not a quantity."""
    client, _, _ = reads
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[
        sdk.PortfolioPosition(instrument_uid='uid', instrument_type='share',
                              quantity=sdk.Quotation(units=23, nano=500000000),
                              current_price=money(5), blocked=True,
                              blocked_lots=sdk.Quotation(units=1, nano=0))])
    result = TInvestExecutionData(client, TInvestStrategyData(client)).get_destination('account')
    assert result.quantities == {'uid': D('23.5')}
    assert result.budget == D('117.5')
    assert not result.limits_ready


def test_blocking_boolean_is_not_numeric(reads):
    """A bool masquerading as an integer blocked count is malformed data."""
    client, _, adapter = reads
    client.operations.get_positions.return_value.securities = [
        SimpleNamespace(instrument_uid='uid', blocked=True, exchange_blocked=False)]
    with pytest.raises(ValueError, match='blocked'):
        adapter.get_destination('account')


@pytest.mark.parametrize('field, value', [
    ('uid', ''), ('currency', ' '), ('quantity', D('NaN')), ('quantity', True),
    ('current_price', D('Infinity')), ('blocked', None), ('blocked', 1),
    ('blocked_lots', None), ('blocked_lots', D(-1)),
])
def test_invalid_neutral_portfolio_fields(reads, field, value):
    """Malformed destination data is rejected before any trading path."""
    _, data, adapter = reads
    data.get_portfolio.return_value = PortfolioSnapshot((replace(entry(), **{field: value}),))
    with pytest.raises(ValueError, match=field if field != 'uid' else 'UID'):
        adapter.get_destination('account')


@pytest.mark.parametrize('field, value', [
    ('uid', 'wrong'), ('lot', 0), ('lot', True), ('lot', -1), ('currency', None),
    ('api_trade_available', None), ('api_trade_available', 1),
])
def test_invalid_metadata(reads, field, value):
    """Metadata must match the requested UID and have a usable lot/currency."""
    _, data, adapter = reads
    data.get_instrument.return_value = replace(data.get_instrument.return_value, **{field: value})
    with pytest.raises(ValueError, match='UID' if field == 'uid' else field):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('field, value', [
    ('instrument_uid', 'wrong'), ('api_trade_available_flag', None),
    ('api_trade_available_flag', 1), ('bestprice_order_available_flag', None),
    ('bestprice_order_available_flag', 'yes'),
])
def test_invalid_status_fields(reads, field, value):
    """Fresh permission fields cannot be inferred from another order type."""
    client, _, adapter = reads
    setattr(client.market_data.get_trading_status.return_value, field, value)
    with pytest.raises(ValueError, match='UID' if field == 'instrument_uid' else field):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('field, value', [
    ('currency', 'usd'), ('currency', ''), ('buy_limits', None), ('sell_limits', None),
])
def test_invalid_cap_currency_or_missing_own_limits(reads, field, value):
    """A currency mismatch or absent own limits cannot fall back to margin limits."""
    client, _, adapter = reads
    setattr(client.orders.get_max_lots.return_value, field, value)
    with pytest.raises(ValueError, match='currency' if field == 'currency' else 'own limits'):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('value', [None, sdk.Quotation(units=-1, nano=0),
                                 SimpleNamespace(units=0, nano=True),
                                 SimpleNamespace(nano=0), True])
def test_missing_negative_or_bool_availability(reads, value):
    """Available cash is nonnegative and all quotation components are numeric."""
    client, _, adapter = reads
    client.orders.get_max_lots.return_value.buy_limits.buy_money_amount = value
    with pytest.raises(ValueError, match='buy_money_amount'):
        adapter.get_trade_rules('account', ['uid'])


@pytest.mark.parametrize('state', ['money_currency', 'money_missing', 'money_nan', 'blocked_bool',
                                 'loading_bool', 'exchange_bool'])
def test_invalid_positions_fields(reads, state):
    """Even when loading, malformed numeric and flag values remain errors."""
    client, _, adapter = reads
    positions = client.operations.get_positions.return_value
    if state == 'money_currency':
        positions.money[0].currency = ''
    elif state == 'money_missing':
        positions.money[0].units = None
    elif state == 'money_nan':
        positions.money[0].nano = 'NaN'
    elif state == 'blocked_bool':
        positions.blocked[0].units = True
    elif state == 'loading_bool':
        positions.limits_loading_in_progress = 0
    else:
        positions.securities = [sdk.PositionsSecurities(
            instrument_uid='uid', blocked=0, exchange_blocked=1)]
    with pytest.raises(ValueError):
        adapter.get_destination('account')


@pytest.mark.parametrize('field, value', [
    ('execution_report_status', sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_UNSPECIFIED),
    ('execution_report_status', True), ('lots_requested', None), ('lots_executed', 3),
    ('order_id', ''), ('instrument_uid', ''),
])
def test_invalid_active_orders(reads, field, value):
    """An unknown status or impossible fill count cannot permit trading."""
    client, _, adapter = reads
    order = sdk.OrderState(order_id='order', instrument_uid='uid',
                          execution_report_status=(
                              sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW),
                          lots_requested=2, lots_executed=0)
    setattr(order, field, value)
    client.orders.get_orders.return_value.orders = [order]
    with pytest.raises(ValueError):
        adapter.get_destination('account')


def test_multicurrency_fresh_cash_zero_caps_and_terminal_orders(reads):
    """Fresh observations, not cached or estimated proceeds, determine availability."""
    client, data, adapter = reads
    positions = client.operations.get_positions.return_value
    positions.money = [money(120), money(2, 'usd'), money(1, 'usd')]
    positions.blocked = [money(0, 'usd')]
    positions.securities = [sdk.PositionsSecurities(
        instrument_uid='uid', blocked=0, balance=999999, exchange_blocked=False)]
    positions.futures = [sdk.PositionsFutures(instrument_uid='future', blocked=0)]
    positions.options = [sdk.PositionsOptions(instrument_uid='option', blocked=0)]
    client.orders.get_orders.return_value.orders = [
        sdk.OrderState(order_id='closed', execution_report_status=status,
                       lots_requested=2, lots_executed=2)
        for status in (sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
                       sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED,
                       sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED)]
    first = adapter.get_destination('account')
    assert first.available_cash == {'rub': D(120), 'usd': D(3)}
    assert first.available_quantities == {'uid': D(20)}
    assert first.active_orders == () and first.limits_ready
    positions.money = [money(7)]
    second = adapter.get_destination('account')
    assert second.available_cash == {'rub': D(7)}
    assert second.budget == first.budget == D(100)
    assert data.get_portfolio.call_count == 2
    assert client.operations.get_positions.call_count == 2
    assert client.orders.get_orders.call_count == 2
    caps = client.orders.get_max_lots.return_value
    caps.buy_limits.buy_money_amount = sdk.Quotation(units=0, nano=0)
    caps.buy_limits.buy_max_lots = caps.sell_limits.sell_max_lots = 0
    data.get_instrument.return_value = replace(data.get_instrument.return_value,
                                               api_trade_available=False)
    rules = adapter.get_trade_rules('account', ['uid'])['uid']
    assert (rules.buy_money_amount, rules.buy_max_lots, rules.sell_max_lots) == (D(0), 0, 0)
    assert not rules.api_trade_available


@pytest.mark.parametrize('account', [None, '', 'a b', True])
@pytest.mark.parametrize('method', ['get_destination', 'get_trade_rules'])
def test_account_is_required(reads, account, method):
    """Account-dependent caps cannot be loaded without a valid account."""
    client, data, adapter = reads
    with pytest.raises(ValueError, match='account_id'):
        if method == 'get_trade_rules':
            adapter.get_trade_rules(account, [])
        else:
            adapter.get_destination(account)
    assert client.mock_calls == []
    assert data.mock_calls == []


def test_invalid_uid_and_programming_error_are_not_masked(reads):
    """Only recognized transport failures are translated."""
    _, data, adapter = reads
    with pytest.raises(ValueError, match='UID'):
        adapter.get_trade_rules('account', [''])
    data.get_portfolio.side_effect = RuntimeError('bug')
    with pytest.raises(RuntimeError, match='bug'):
        adapter.get_destination('account')


def test_sdk_missing_blocking_is_neutral_but_not_trade_ready(reads):
    """An unset SDK sentinel cannot escape as a neutral field or authorize a trade."""
    client, _, _ = reads
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[
        sdk.PortfolioPosition(instrument_uid='uid', instrument_type='share',
                              quantity=sdk.Quotation(units=1, nano=0), current_price=money(5))])
    data = TInvestStrategyData(client)
    snapshot = data.get_portfolio('account')
    assert snapshot.positions[0].blocked is None
    assert snapshot.positions[0].blocked_lots is None
    with pytest.raises(ValueError, match='blocked'):
        TInvestExecutionData(client, data).get_destination('account')
