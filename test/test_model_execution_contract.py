"""The declared RUB replay model obeys the same neutral read/order port signatures."""
import inspect
from decimal import Decimal as D
from types import SimpleNamespace
from test.test_fx_funding import NOW
from test.test_fx_runtime import money
from unittest.mock import create_autospec

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import OrdersService

from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit, order_cash_bound
from autorepeater.execution import OrderExecutionError, OrderExecutor
from autorepeater.execution_data import ExecutionData
from autorepeater.fx import FxSnapshot
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.order_plan import OrderIntent
from autorepeater.valuation import SecurityData, Valuation
from scripts.check_rebalance_policy import ModelExecution


def test_model_implements_both_neutral_ports_with_valuation_and_broker_id_poll():
    """Strict port doubles forward exactly the call shapes used by the real engine."""
    model = ModelExecution({}, {'R': D(450)}, {'R': 1}, D(1000), D('.01'))
    reads = create_autospec(ExecutionData, instance=True, spec_set=True)
    orders = create_autospec(OrderExecutor, instance=True, spec_set=True)
    reads.get_destination.side_effect = model.get_destination
    reads.get_trade_rules.side_effect = model.get_trade_rules
    orders.submit_order.side_effect = model.submit_order
    orders.get_order_state.side_effect = model.get_order_state
    books = create_autospec(SecurityData, instance=True, spec_set=True)
    books.get_security_quote.return_value = SecurityQuote(
        'rub', D(450), D(450), SecurityQuoteUnit.MONEY_PER_PIECE)
    value = Valuation(FxSnapshot({}, NOW), books, clock=lambda: NOW)
    before = reads.get_destination('MODEL', valuation=value)
    assert before.fx_snapshot is value.fx
    assert before.budget == D(1000) and before.available_cash == {'rub': D(1000)}
    assert reads.get_trade_rules('MODEL', ['R'])['R'].buy_money_amount == D(1000)
    result = orders.submit_order('MODEL', OrderIntent('R', 'BUY', 1, 1, {(): D(1)}))
    assert orders.get_order_state('MODEL', result.order_id) is result
    assert model.orders == [('R', 'BUY', 1, 1)]
    after = reads.get_destination('MODEL', valuation=value)
    assert after.quantities == {'R': D(1)}
    assert after.available_cash == {'rub': D('545.5')} and after.budget == D('995.5')
    assert after.portfolio.positions[0].native_price == D(450)
    assert after.marks == {'R': D(450)}
    with pytest.raises(OrderExecutionError, match='unknown model order'):
        orders.get_order_state('MODEL', 'unsubmitted')


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_model_money_matches_explicit_sdk_bounds_without_claiming_sdk_exact_cash(side):
    """Marked fills are a model assumption; SDK fee inclusion remains unknown."""
    model = ModelExecution({'R': D(1)}, {'R': D(450)}, {'R': 1}, D(1000), D('.01'))
    intent = OrderIntent('R', side, 1, 1, {(): D(1)})
    modeled = model.submit_order('MODEL', intent)
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    orders.post_order.return_value = sdk.PostOrderResponse(
        instrument_uid='R', order_id=modeled.order_id,
        direction=(sdk.OrderDirection.ORDER_DIRECTION_BUY if side == 'BUY'
                   else sdk.OrderDirection.ORDER_DIRECTION_SELL),
        execution_report_status=sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=1, lots_executed=1, total_order_amount=money('rub', 450),
        executed_commission=money('rub', '4.5'))
    actual = TInvestOrderExecutor(SimpleNamespace(orders=orders)).submit_order('MODEL', intent)
    assert order_cash_bound(modeled, modeled.amounts) == order_cash_bound(actual, actual.amounts)
    assert isinstance(actual.cash, dict) and not actual.cash
    assert modeled.cash == ({'rub': D('445.5')} if side == 'SELL' else {})
    assert modeled.amounts.total == MoneyAmount('rub', D(450))
    assert modeled.amounts.executed_commission == MoneyAmount('rub', D('4.5'))
    orders.get_order_price.assert_not_called()
