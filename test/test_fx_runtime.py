"""Real Runner/SDK-adapter multicurrency flow against strict offline broker doubles."""
import inspect
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import (
    InstrumentsService, MarketDataService, OperationsService, OrdersService,
)

from autorepeater import runner as application
from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.order_plan import OrderIntent
from autorepeater.cash_facts import order_cash_bound


def money(currency, amount):
    """Exact SDK money, including fractional fees; no binary floats."""
    value = D(amount)
    units = int(value)
    return sdk.MoneyValue(currency=currency, units=units, nano=int((value-units)*10**9))


def quotation(amount):
    """The same dimensionless units/nano representation as the SDK book."""
    value = money('rub', amount)
    return sdk.Quotation(units=value.units, nano=value.nano)


@pytest.fixture(name='sdk_runtime')
def fixture_sdk_runtime():
    """Source, cash identity, nominal-percent bond and FX have independent observations."""
    client = SimpleNamespace(**{
        name: create_autospec(inspect.unwrap(service), instance=True, spec_set=True)
        for name, service in [('operations', OperationsService), ('orders', OrdersService),
                              ('instruments', InstrumentsService),
                              ('market_data', MarketDataService)]})
    state = SimpleNamespace(cash={'rub': D(1000), 'cny': D(0)}, held=D(0), closed=False,
                            requests=[], events=[])
    instrument = sdk.Instrument(uid='C', ticker='BOND', instrument_type='bond', class_code='ANY',
                                currency='cny', lot=1, api_trade_available_flag=True)
    pair = sdk.Currency(uid='cny-rub', iso_currency_name='CNY', currency='rub', lot=1,
                        nominal=money('cny', '1'), api_trade_available_flag=True,
                        buy_available_flag=True, sell_available_flag=True)
    client.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=instrument)
    client.instruments.bond_by.return_value = sdk.BondResponse(instrument=sdk.Bond(
        uid='C', currency='cny', nominal=money('cny', '100'), aci_value=money('cny', '2')))
    client.instruments.currencies.return_value = sdk.CurrenciesResponse(instruments=[pair])
    client.instruments.currency_by.side_effect = lambda **kwargs: sdk.CurrencyResponse(
        instrument=sdk.Currency(uid=kwargs['id'], iso_currency_name=(
            'CNY' if kwargs['id'] == 'cash-cny' else 'RUB')))

    def position(uid, quantity, price, kind='bond'):
        return sdk.PortfolioPosition(
            instrument_uid=uid, instrument_type=kind, quantity=quotation(quantity),
            current_price=money('rub', price), blocked=False, blocked_lots=quotation(0))

    def portfolio(*, account_id):
        state.events.append(('portfolio', account_id))
        if account_id == '0004':
            # Broker RUB mark700 is preserved but the native book60 * FX12 is used.
            return sdk.PortfolioResponse(positions=[position('C', 1, 700)])
        positions = [position('cash-' + code, amount, 1 if code == 'rub' else 10, 'currency')
                     for code, amount in state.cash.items() if amount]
        if state.held:
            positions.append(position('C', state.held, 700))
        return sdk.PortfolioResponse(positions=positions)

    client.operations.get_portfolio.side_effect = portfolio
    client.operations.get_positions.side_effect = lambda **kwargs: sdk.PositionsResponse(
        money=[money(code, amount) for code, amount in state.cash.items()], blocked=[],
        securities=[], futures=[], options=[], limits_loading_in_progress=False)
    client.orders.get_orders.return_value = sdk.GetOrdersResponse(orders=[])

    def book(*, instrument_id, depth):
        assert depth == 1
        price = 58 if instrument_id == 'C' else 12
        return sdk.GetOrderBookResponse(
            instrument_uid=instrument_id, orderbook_ts=datetime.now(timezone.utc),
            bids=[sdk.Order(price=quotation(price), quantity=10000)],
            asks=[sdk.Order(price=quotation(price), quantity=10000)])

    client.market_data.get_order_book.side_effect = book

    def status(*, instrument_id):
        state.events.append(('status', instrument_id))
        return sdk.GetTradingStatusResponse(
            instrument_uid=instrument_id, api_trade_available_flag=True,
            bestprice_order_available_flag=not (state.closed and instrument_id == 'cny-rub'))

    client.market_data.get_trading_status.side_effect = status

    def caps(*, request):
        assert request.account_id == 'dst'
        currency = 'cny' if request.instrument_id == 'C' else 'rub'
        amount = state.cash[currency]
        maximum = int(amount/60) if currency == 'cny' else int(amount/12)
        state.events.append(('caps', request.instrument_id))
        return sdk.GetMaxLotsResponse(
            currency=currency, buy_limits=sdk.BuyLimitsView(
                buy_money_amount=quotation(amount), buy_max_lots=maximum),
            sell_limits=sdk.SellLimitsView(sell_max_lots=10000))

    client.orders.get_max_lots.side_effect = caps

    def submit(**kwargs):
        assert kwargs['account_id'] == 'dst'
        assert kwargs['order_type'] == sdk.OrderType.ORDER_TYPE_BESTPRICE
        assert 'price' not in kwargs
        assert kwargs['direction'] == sdk.OrderDirection.ORDER_DIRECTION_BUY
        uid, lots = kwargs['instrument_id'], kwargs['quantity']
        state.events.append(('submit', uid))
        state.requests.append(kwargs['order_id'])
        currency, amount = ('rub', D(lots*12)) if uid == 'cny-rub' else ('cny', D(lots*60))
        state.cash[currency] -= amount + 1
        if uid == 'cny-rub':
            state.cash['cny'] += lots
        else:
            state.held += lots
        return sdk.PostOrderResponse(
            instrument_uid=uid, order_id='broker-' + uid,
            direction=sdk.OrderDirection.ORDER_DIRECTION_BUY,
            execution_report_status=sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
            lots_requested=lots, lots_executed=lots, total_order_amount=money(currency, amount),
            executed_commission=money(currency, '1'))

    client.orders.post_order.side_effect = submit
    return client, state


def launch(client, *, debug=False, params=None):
    """Use the actual public Runner without entering a real SDK Client."""
    config = AccountConfig('synthetic', '0004', D('.1'), D('.01'))
    prepared = PreparedStrategy(
        AccountStrategy, PreparedAccountSource('0004', config), 'synthetic')
    with patch.object(application.Runner, '_create_client', autospec=True) as connect:
        connect.return_value.__enter__.return_value = client
        with patch('autorepeater.repeater.reporting.print_rebalance_plan') as report:
            application.Runner('synthetic', prepared, 'dst',
                               params if params is not None else
                               application.RunnerParams(debug)).run_sync()
    return report.call_args.args[0]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('available', [False, True])
def test_configured_runner_selects_exact_fx_uid(sdk_runtime, reverse, available):
    """The public Runner carries selection into valuation, funding and refreshed execution."""
    client, state = sdk_runtime
    pairs = client.instruments.currencies.return_value.instruments
    other = replace(pairs[0], uid='other-cny-rub', api_trade_available_flag=available,
                    buy_available_flag=available, sell_available_flag=available)
    pairs.insert(0, other)
    if reverse:
        pairs.reverse()
    plan = launch(client, params=application.RunnerParams(
        False, fx_pair_uids={'CNY': 'cny-rub'}))
    assert plan.marks == {'C': D(720)}
    assert [(order.uid, order.lots) for order in plan.funding.exchanges] == [('cny-rub', 65)]
    assert state.cash == {'rub': D(219), 'cny': D(4)}
    assert state.held == 1
    assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list
            ] == ['cny-rub', 'C']
    assert not any(call.kwargs['instrument_id'] == other.uid
                   for call in client.market_data.get_order_book.call_args_list)
    assert not any(uid == other.uid for _, uid in state.events)


@pytest.mark.parametrize('selection', [None, 'missing'])
def test_runner_ambiguous_or_missing_explicit_pair_stops_before_effects(sdk_runtime, selection):
    """An absent configured route never falls back to another valid CNY pair."""
    client, state = sdk_runtime
    pairs = client.instruments.currencies.return_value.instruments
    pairs.append(replace(pairs[0], uid='other-cny-rub'))
    params = application.RunnerParams(False) if selection is None else application.RunnerParams(
        False, fx_pair_uids={'cny': selection})
    with pytest.raises(ValueError, match='ambiguous' if selection is None else 'no direct'):
        launch(client, params=params)
    assert state.cash == {'rub': D(1000), 'cny': D(0)}
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('entrypoint', ['runner', 'cli', 'cloud', 'one-shot'])
def test_launch_environment_selects_fx_pair_through_real_runner(
        sdk_runtime, monkeypatch, entrypoint):
    """All normal launch boundaries use the configured route, without catalog filtering."""
    import main  # pylint: disable=import-outside-toplevel
    from autorepeater import serverless  # pylint: disable=import-outside-toplevel
    client, state = sdk_runtime
    pairs = client.instruments.currencies.return_value.instruments
    pairs.insert(0, replace(pairs[0], uid='other-cny-rub'))
    monkeypatch.setenv('FX_PAIR_UIDS', json.dumps({'cny': 'cny-rub'}))
    monkeypatch.setenv('INVEST_TOKEN', 'offline-synthetic')
    config = AccountConfig('synthetic', '0004', D('.1'), D('.01'))
    prepared = PreparedStrategy(
        AccountStrategy, PreparedAccountSource('0004', config), 'synthetic')
    with patch.object(application.Runner, '_create_client', autospec=True,
                      spec_set=True) as connect:
        connect.return_value.__enter__.return_value = client
        if entrypoint == 'runner':
            application.Runner('synthetic', prepared, 'dst').run_sync()
        elif entrypoint == 'cli':
            monkeypatch.setattr('sys.argv', [
                'main.py', '--algoritm', 'ACCOUNT', '-s', 'synthetic', '-d', 'dst'])
            with patch.object(main, 'prepare_strategy', autospec=True,
                              spec_set=True, return_value=prepared), patch.object(
                    application.Runner, 'run', application.Runner.run_sync):
                main.main()
        elif entrypoint == 'cloud':
            with patch.object(serverless, 'prepare_strategy', autospec=True,
                              spec_set=True, return_value=prepared):
                serverless.handler({'queryStringParameters': {
                    'algoritm': 'ACCOUNT', 'src': 'synthetic', 'dst': 'dst'}}, None)
        else:
            for key, value in {'ALGORITM': 'ACCOUNT', 'SRC_ACCOUNT': 'synthetic',
                               'DST_ACCOUNT': 'dst', 'ONE_SHOT_DEBUG': '0'}.items():
                monkeypatch.setenv(key, value)
            example = (Path(__file__).resolve().parents[1] / 'README.md').read_text(
                encoding='utf-8')
            example = example.partition('### Один локальный проход')[2]
            example = example.partition("<<'PY'\n")[2].partition('\nPY\n')[0]
            assert example
            with patch('autorepeater.strategies.prepare_strategy', autospec=True,
                       spec_set=True, return_value=prepared):
                # pylint: disable=exec-used
                exec(compile(example, __file__, 'exec'), {})
    assert state.held == 1
    assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list
            ] == ['cny-rub', 'C']


def test_public_runtime_bond_fx_then_native_buy_without_history_or_price(sdk_runtime):
    """The missing runtime wiring is exercised end to end through real adapters."""
    client, state = sdk_runtime
    plan = launch(client)
    assert plan.marks == {'C': D(720)}
    assert plan.snapshot.budget == D(1000)
    assert [(order.uid, order.lots) for order in plan.funding.exchanges] == [('cny-rub', 65)]
    assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list] == [
        'cny-rub', 'C']
    assert state.cash == {'rub': D(219), 'cny': D(4)}
    assert state.held == 1
    assert len(state.requests) == len(set(state.requests)) == 2
    assert state.events.index(('status', 'cny-rub')) < state.events.index(('submit', 'cny-rub'))
    assert state.events.index(('caps', 'C'), state.events.index(('submit', 'cny-rub'))) < (
        state.events.index(('submit', 'C')))
    assert [call.kwargs['account_id'] for call in client.operations.get_portfolio.call_args_list
            ].count('0004') == 1
    client.orders.get_order_price.assert_not_called()
    client.operations.get_operations.assert_not_called()
    client.operations.get_operations_by_cursor.assert_not_called()


@pytest.mark.parametrize('closed,debug', [(True, False), (False, True)])
def test_public_runtime_closed_fx_or_debug_never_submits(sdk_runtime, closed, debug):
    """Both actual Runner controls apply to the automatically built FX plan."""
    client, state = sdk_runtime
    state.closed = closed
    plan = launch(client, debug=debug)
    assert plan.funding.demands
    assert state.cash == {'rub': D(1000), 'cny': D(0)}
    assert state.held == 0
    client.orders.post_order.assert_not_called()


def test_public_runtime_existing_native_cash_uses_iso_not_broker_rub_mark(sdk_runtime):
    """100 CNY marked1000 RUB is capital1200 and needs no exchange for the paper."""
    client, state = sdk_runtime
    state.cash = {'rub': D(0), 'cny': D(100)}
    plan = launch(client)
    assert plan.snapshot.budget == D(1200)
    assert plan.strategy.cash_floor == D(120)
    assert not plan.funding.exchanges
    assert state.cash == {'rub': D(0), 'cny': D(39)}
    assert state.held == 1
    client.orders.post_order.assert_called_once()
    assert client.orders.post_order.call_args.kwargs['instrument_id'] == 'C'
    assert ('status', 'cny-rub') in state.events


@pytest.mark.parametrize('changes', [
    {'executed_commission': money('rub', '-1')},
    {'executed_commission': money('cny', '1')},
    {'total_order_amount': None},
    {'total_order_amount': money('rub', '0'), 'executed_commission': money('rub', '0')},
])
def test_public_runtime_incomplete_money_preserves_fill_but_defers_paper(sdk_runtime, changes):
    """Bad monetary fields never invalidate/repair FILL identity or synthesize cash."""
    client, state = sdk_runtime
    submit = client.orders.post_order.side_effect

    def incomplete(**kwargs):
        response = submit(**kwargs)
        for name, value in changes.items():
            setattr(response, name, value)
        return response

    client.orders.post_order.side_effect = incomplete
    launch(client)
    client.orders.post_order.assert_called_once()
    assert state.held == 0
    assert state.cash == {'rub': D(219), 'cny': D(65)}
    assert client.operations.get_positions.call_count == 3


def test_sdk_money_fields_are_optional_bounds_separate_from_receipt_identity():
    """PostOrder and GetOrderState preserve native facts without claiming exact cash."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    fields = {'instrument_uid': 'C', 'order_id': 'actual-broker',
              'direction': sdk.OrderDirection.ORDER_DIRECTION_BUY,
              'execution_report_status': (
                  sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL),
              'lots_requested': 1, 'lots_executed': 1,
              'total_order_amount': money('cny', '60.000000001'),
              'executed_commission': money('cny', '.5')}
    orders.post_order.return_value = sdk.PostOrderResponse(**fields)
    orders.get_order_state.return_value = sdk.OrderState(**fields)
    executor = TInvestOrderExecutor(SimpleNamespace(orders=orders),
                                    order_id_factory=lambda: 'one-request')
    first = executor.submit_order('dst', OrderIntent('C', 'BUY', 1, 1, {(): D(1)}))
    polled = executor.get_order_state('dst', first.order_id)
    for result in (first, polled):
        assert isinstance(result.cash, dict)
        assert not result.cash
        assert result.amounts.order_id == 'actual-broker'
        assert result.amounts.total.amount == D('60.000000001')
        assert result.amounts.executed_commission.amount == D('.5')
        assert order_cash_bound(result, result.amounts).amount.amount == D('60.500000001')
    assert orders.post_order.call_args.kwargs['order_id'] == 'one-request'
    orders.get_order_state.assert_called_once_with(account_id='dst', order_id='actual-broker')
