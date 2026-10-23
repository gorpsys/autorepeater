"""A pass shares one FX valuation across opaque children and destination."""
import inspect
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from test.test_account_currency import NOW, position, valuation
from test.test_composite_strategy import make_composite
from unittest.mock import Mock, call, create_autospec, patch

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import OperationsService, OrdersService

from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.execution import OrderExecutor
from autorepeater.execution_data import AccountReadiness, ExecutionData, TradeRules
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_data import InstrumentType, PortfolioSnapshot, StrategyData
from autorepeater.strategy_plan import StrategyContext
from autorepeater.tinvest_execution_data import TInvestExecutionData

D = Decimal


def execution(data):
    """A real destination adapter with strict SDK services, never an external client."""
    client = Mock(spec_set=['operations', 'orders'])
    for name, service in [('operations', OperationsService), ('orders', OrdersService)]:
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    client.operations.get_positions.return_value = sdk.PositionsResponse(
        money=[sdk.MoneyValue(currency='cny', units=100, nano=0)], blocked=[],
        securities=[], futures=[], options=[], limits_loading_in_progress=False)
    client.orders.get_orders.return_value = sdk.GetOrdersResponse(orders=[])
    return TInvestExecutionData(client, data), client


def account():
    """Zero cash in source is irrelevant to its investable constituent weights."""
    config = AccountConfig('source', '00123', D('0.1'), D('0.01'))
    return AccountStrategy(PreparedAccountSource('00123', config))


def test_destination_cash_value_budget_and_reserve_counted_once():
    """GetPositions and portfolio cash describe one balance, not two capital terms."""
    value, _ = valuation()
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((position(
        'cash', price='10', quantity='100', kind=InstrumentType.CURRENCY),))
    adapter, _ = execution(data)
    destination = adapter.get_destination('dst', valuation=value)
    assert destination.budget == D(1200)
    assert destination.available_cash == {'cny': D(100)}
    assert destination.fx_snapshot is value.fx
    assert value.fx.to_rub(destination.portfolio.positions[0].native_cash).amount == D(1200)
    source = account()
    source_data = create_autospec(StrategyData, instance=True, spec_set=True)
    source_data.get_portfolio.return_value = PortfolioSnapshot((position('foreign'),))
    snapshot = source.load_snapshot(value.strategy_data(source_data))
    plan = source.build_plan(snapshot, StrategyContext((), destination.budget, D(0), False, (), {}))
    assert plan.cash_floor == D(120)
    assert plan.target.quantities == {'foreign': D('0.9')}


@pytest.mark.parametrize('debug', [False, True])
def test_shared_uid_composite_uses_one_book_and_one_fx_before_any_effect(debug):
    """Repeated source UIDs keep exact occurrence ownership and shared RUB marks."""
    value, books = valuation()
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    source = PortfolioSnapshot((position('foreign'),))
    destination = PortfolioSnapshot((position('foreign', 'rub', '500'), position(
        'cash', price='10', quantity='100', kind=InstrumentType.CURRENCY)))
    data.get_portfolio.side_effect = (
        lambda account_id: source if account_id == '00123' else destination)
    adapter, _ = execution(data)
    destination_state = adapter.get_destination('dst', valuation=value)
    port = create_autospec(ExecutionData, instance=True, spec_set=True)
    port.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    port.get_destination.return_value = destination_state
    port.get_trade_rules.return_value = {'foreign': TradeRules(1, 'cny', True, True, D(100), 1, 1)}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    repeater = AutoRepeater(make_composite([account(), account()], weights=['0.5', '0.5']),
                            data, port, executor)
    repeater.set_debug(debug)
    with patch('autorepeater.repeater.reporting.print_rebalance_plan') as report:
        if debug:
            assert not repeater.sync_accounts('dst', valuation=value)
        else:
            with pytest.raises(ValueError, match='FX financing is not supported'):
                repeater.sync_accounts('dst', valuation=value)
        order_plan = report.call_args.args[0]
    assert order_plan.strategy.budget == D(2400)
    assert order_plan.marks == {'foreign': D(1200)}
    assert order_plan.ownership == {(0,): {'foreign': D('0.5')},
                                    (1,): {'foreign': D('0.5')}}
    assert all(child.target.prices == {'foreign': D(1200)}
               for child in order_plan.strategy.children)
    port.get_destination.assert_called_once_with('dst', valuation=value)
    # Destination preparation is a separate read above; begin_pass discards that book.
    assert books.get_security_quote.call_count == 2
    assert executor.mock_calls == []


def test_stale_fx_fails_before_any_account_or_effect():
    """A prior RUB broker mark cannot authorize a pass with expired foreign cash rates."""
    value, _ = valuation(clock=lambda: NOW + timedelta(seconds=181))
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    port = create_autospec(ExecutionData, instance=True, spec_set=True)
    port.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    with pytest.raises(ValueError, match='stale'):
        AutoRepeater(account(), data, port, executor).sync_accounts('dst', valuation=value)
    data.get_portfolio.assert_not_called()
    assert port.mock_calls == [call.get_readiness('dst', timeout=pytest.approx(30))]
    assert executor.mock_calls == []


def test_destination_with_a_different_fx_snapshot_rejects_before_rules_or_orders():
    """Identical numerical rates do not make two independent observations one snapshot."""
    value, _ = valuation()
    other, _ = valuation()
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((position('foreign'),))
    adapter, _ = execution(data)
    destination = adapter.get_destination('dst', valuation=value)
    port = create_autospec(ExecutionData, instance=True, spec_set=True)
    port.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    port.get_destination.return_value = replace(destination, fx_snapshot=other.fx)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    data.reset_mock()
    with pytest.raises(ValueError, match='share the pass FX snapshot'):
        AutoRepeater(account(), data, port, executor).sync_accounts('dst', valuation=value)
    port.get_trade_rules.assert_not_called()
    data.get_portfolio.assert_not_called()
    assert not executor.mock_calls
