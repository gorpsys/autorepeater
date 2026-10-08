"""Explicit sandbox selection at the application boundary, without SDK connections."""
import inspect
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import ANY, call, create_autospec, patch

import pytest
from t_tech.invest import Client, schemas as sdk
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest.services import (
    InstrumentsService, MarketDataService, OperationsService, OrdersService, Services,
)

from autorepeater import runner as application
from autorepeater.account_config import validate_account_config
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.grpc_deadline import UnaryDeadlineInterceptor, _CallDetails
from autorepeater.strategy_contract import PreparedStrategy, Strategy


@pytest.fixture(name='wiring')
def fixture_wiring(monkeypatch):
    """Strict constructor doubles never enter the SDK's channel constructor."""
    production = create_autospec(Client, spec_set=True)
    sandbox = create_autospec(SandboxClient, spec_set=True)
    for constructor in (production, sandbox):
        constructor.return_value.__enter__.return_value = create_autospec(
            Services, instance=True, spec_set=True)
        constructor.return_value.__exit__.return_value = False
    monkeypatch.setattr(application, 'Client', production)
    monkeypatch.setattr(application, 'SandboxClient', sandbox)
    report = create_autospec(application.print_all_portfolio, spec_set=True)
    monkeypatch.setattr(application, 'print_all_portfolio', report)
    monkeypatch.setattr(application, 'configure_local_logging',
                        create_autospec(application.configure_local_logging, spec_set=True))
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    return SimpleNamespace(production=production, sandbox=sandbox, report=report,
                           prepared=PreparedStrategy(lambda _: strategy, None, 'synthetic'))


@pytest.mark.parametrize('mode', ['run', 'run_sync'])
@pytest.mark.parametrize('selection', [None, False, True])
@pytest.mark.parametrize('debug', [False, True])
def test_sandbox_wiring_selection_and_shared_adapters(wiring, mode, selection, debug):
    """Every adapter receives the selected context; old calls retain their exact args."""
    options = {} if selection is None else {'sandbox': selection}
    runner = application.Runner('synthetic-token', wiring.prepared, 'dst',
                                application.RunnerParams(debug), **options)
    wiring.production.assert_not_called()
    wiring.sandbox.assert_not_called()
    with patch.object(application, 'AutoRepeater', autospec=True, spec_set=True) as engine:
        getattr(runner, mode)()
    selected = wiring.sandbox if selection else wiring.production
    unused = wiring.production if selection else wiring.sandbox
    selected.assert_called_once_with(
        token='synthetic-token',
        target=INVEST_GRPC_API_SANDBOX if selection else INVEST_GRPC_API,
        interceptors=[ANY])
    unused.assert_not_called()
    client = selected.return_value.__enter__.return_value
    selected.return_value.__enter__.assert_called_once_with()
    selected.return_value.__exit__.assert_called_once_with(None, None, None)
    strategy, data, rules, executor = engine.call_args.args
    assert strategy is runner.strategy
    assert strategy.mock_calls == []
    assert client.mock_calls == []
    assert data._client is client  # pylint: disable=protected-access
    assert rules._client is client  # pylint: disable=protected-access
    assert rules._data is data  # pylint: disable=protected-access
    assert executor.client is client
    assert engine.return_value.mock_calls == [
        call.set_debug(debug), call.mainflow('dst') if mode == 'run' else call.sync_accounts('dst')]
    assert wiring.report.call_args_list == ([call(client)] if mode == 'run' else [])


@pytest.mark.parametrize('mode', ['run', 'run_sync'])
@pytest.mark.parametrize('timeout, expected', [(None, 10), (30, 10), (2, 2)])
def test_sandbox_wiring_installed_deadline(wiring, mode, timeout, expected):
    """The interceptor supplied to SandboxClient bounds unary requests before entry."""
    with patch.object(application, 'AutoRepeater', autospec=True, spec_set=True):
        getattr(application.Runner('synthetic', wiring.prepared, 'dst', sandbox=True), mode)()
    interceptor, = wiring.sandbox.call_args.kwargs['interceptors']
    assert isinstance(interceptor, UnaryDeadlineInterceptor)
    details = _CallDetails('/service/unary', timeout, (), None, False, None)
    continuation = create_autospec(lambda forwarded, request: None, spec_set=True)
    interceptor.intercept_unary_unary(continuation, details, 'request')
    forwarded, request = continuation.call_args.args
    assert forwarded.timeout == expected
    assert forwarded._replace(timeout=timeout) == details
    continuation.assert_called_once_with(forwarded, request)


@pytest.mark.parametrize('mode', ['run', 'run_sync'])
@pytest.mark.parametrize('stage', ['construct', 'enter', 'pass'])
def test_sandbox_wiring_failure_never_falls_back(wiring, mode, stage):
    """A failed sandbox launch escapes without creating a production client or retrying."""
    error = RuntimeError('synthetic failure')
    with patch.object(application, 'AutoRepeater', autospec=True, spec_set=True) as engine:
        if stage == 'construct':
            wiring.sandbox.side_effect = error
        elif stage == 'enter':
            wiring.sandbox.return_value.__enter__.side_effect = error
        else:
            method = 'mainflow' if mode == 'run' else 'sync_accounts'
            getattr(engine.return_value, method).side_effect = error
        runner = application.Runner('synthetic', wiring.prepared, 'dst', sandbox=True)
        with pytest.raises(RuntimeError) as raised:
            getattr(runner, mode)()
    assert raised.value is error
    wiring.sandbox.assert_called_once()
    wiring.production.assert_not_called()
    if stage == 'pass':
        wiring.sandbox.return_value.__exit__.assert_called_once_with(
            RuntimeError, error, ANY)
    else:
        engine.assert_not_called()
        wiring.sandbox.return_value.__exit__.assert_not_called()


@pytest.mark.parametrize('value', [None, 0, 1, '', 'false', 'true'])
def test_sandbox_wiring_requires_explicit_bool(wiring, value):
    """Ambiguous mode inputs fail before a strategy factory or client can run."""
    factory = create_autospec(lambda source: None, spec_set=True)
    prepared = PreparedStrategy(factory, None, 'synthetic')
    with pytest.raises(TypeError, match='sandbox.*bool'):
        application.Runner('synthetic', prepared, 'dst', sandbox=value)
    factory.assert_not_called()
    wiring.production.assert_not_called()
    wiring.sandbox.assert_not_called()


def test_sandbox_wiring_keeps_debug_only_params():
    """Transport selection is keyword-only and does not become a financial parameter."""
    assert [field.name for field in fields(application.RunnerParams)] == ['debug']
    parameter = inspect.signature(application.Runner).parameters['sandbox']
    assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


@pytest.mark.parametrize('source_id', ['0004', '2a78f51a-42c0-4a3e-8f95-b9ef692d2da1'])
def test_sandbox_wiring_finite_public_sync(wiring, source_id):
    """The real runner, engine, and adapters execute one BUY against strict SDK doubles."""
    services = SimpleNamespace(**{
        name: create_autospec(inspect.unwrap(service), instance=True, spec_set=True)
        for name, service in [('operations', OperationsService), ('orders', OrdersService),
                              ('instruments', InstrumentsService),
                              ('market_data', MarketDataService)]
    })
    wiring.sandbox.return_value.__enter__.return_value = services
    config = validate_account_config({'name': 'synthetic', 'source_account_id': source_id,
                                      'reserve': '0.01', 'allocation_drift_limit': '0.0092'})
    prepared = PreparedStrategy(
        AccountStrategy, PreparedAccountSource(source_id, config), 'synthetic')
    source = sdk.PortfolioResponse(positions=[sdk.PortfolioPosition(
        instrument_uid='uid', instrument_type='share', quantity=sdk.Quotation(units=10, nano=0),
        current_price=sdk.MoneyValue(currency='rub', units=10, nano=0),
        blocked=False, blocked_lots=sdk.Quotation(units=0, nano=0))])
    destination = sdk.PortfolioResponse(positions=[sdk.PortfolioPosition(
        instrument_uid='cash', instrument_type='currency',
        quantity=sdk.Quotation(units=100, nano=0),
        current_price=sdk.MoneyValue(currency='rub', units=1, nano=0),
        blocked=False, blocked_lots=sdk.Quotation(units=0, nano=0))])
    # Exhaustion detects any additional orchestration pass or unplanned read.
    services.operations.get_portfolio.side_effect = [source, destination]
    services.operations.get_positions.return_value = sdk.PositionsResponse(
        money=[sdk.MoneyValue(currency='rub', units=100, nano=0)], blocked=[],
        securities=[], futures=[], options=[], limits_loading_in_progress=False)
    services.orders.get_orders.return_value = sdk.GetOrdersResponse(orders=[])
    services.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', ticker='TEST', name='Synthetic',
                                  instrument_type='share', class_code='ANY', lot=1,
                                  currency='rub', api_trade_available_flag=True))
    services.market_data.get_trading_status.return_value = sdk.GetTradingStatusResponse(
        instrument_uid='uid', api_trade_available_flag=True, bestprice_order_available_flag=True)
    services.orders.get_max_lots.return_value = sdk.GetMaxLotsResponse(
        currency='rub', buy_limits=sdk.BuyLimitsView(
            buy_money_amount=sdk.Quotation(units=100, nano=0), buy_max_lots=10),
        sell_limits=sdk.SellLimitsView(sell_max_lots=0))
    services.orders.post_order.return_value = sdk.PostOrderResponse(
        instrument_uid='uid', order_id='broker-id',
        direction=sdk.OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=9, lots_executed=9)
    application.Runner('synthetic', prepared, 'dst', sandbox=True).run_sync()
    wiring.production.assert_not_called()
    wiring.report.assert_not_called()
    assert services.operations.mock_calls == [
        call.get_portfolio(account_id=source_id), call.get_portfolio(account_id='dst'),
        call.get_positions(account_id='dst')]
    assert services.instruments.mock_calls == [
        call.get_instrument_by(id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')]
    assert services.market_data.mock_calls == [call.get_trading_status(instrument_id='uid')]
    assert services.orders.mock_calls == [
        call.get_orders(account_id='dst'),
        call.get_max_lots(request=ANY),
        call.post_order(account_id='dst', instrument_id='uid', quantity=9,
                        direction=sdk.OrderDirection.ORDER_DIRECTION_BUY,
                        order_type=sdk.OrderType.ORDER_TYPE_BESTPRICE, order_id=ANY)]
    request = services.orders.get_max_lots.call_args.kwargs['request']
    assert (request.account_id, request.instrument_id) == ('dst', 'uid')
