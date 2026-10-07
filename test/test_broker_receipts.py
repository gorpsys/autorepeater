"""Factual broker receipts through public boundaries, entirely offline."""
from dataclasses import replace
from decimal import Decimal
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import Mock, call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import (
    MoneyValue, OrderDirection, OrderExecutionReportStatus, OrderState, OrderType,
    PostOrderResponse, RequestError,
)
from t_tech.invest.services import OrdersService

from autorepeater import execution as execution_module
from autorepeater.execution import ExecutionReceipt, OrderExecutionError, execute_plan
from autorepeater.execution_data import ExecutionData, ExecutionSnapshot, TradeRules
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.order_plan import OrderIntent, build_order_plan
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_plan import StrategyDecision, StrategyPlan, TradeMode


BUY = OrderDirection.ORDER_DIRECTION_BUY
SELL = OrderDirection.ORDER_DIRECTION_SELL
FILL = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
NEW = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW
PARTIAL = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL
STATUSES = [
    (FILL, 'FILL', 2), (NEW, 'NEW', 0), (PARTIAL, 'PARTIALLYFILL', 1),
    (OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED, 'REJECTED', 0),
    (OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED, 'CANCELLED', 0),
]
METHODS = ['submit_order', 'get_order_state']
MISSING = object()
FIELDS = ('instrument_uid', 'direction', 'execution_report_status', 'order_id',
          'lots_requested', 'lots_executed')
SECRET = 'malformed-private-value'
BAD_FIELDS = [
    *[(field, value, 'invalid order identity')
      for field in ('instrument_uid', 'order_id')
      for value in (None, '', 1, False, [])],
    *[('direction', value, 'invalid order direction') for value in (
        None, 'SELL', 'ORDER_DIRECTION_BUY', SECRET, 1, 2, 999, True, False, [],
        SimpleNamespace(name='ORDER_DIRECTION_SELL'), OrderType.ORDER_TYPE_LIMIT,
        OrderDirection.ORDER_DIRECTION_UNSPECIFIED)],
    *[('execution_report_status', value, 'invalid order status') for value in (
        None, 'FILL', 'EXECUTION_REPORT_STATUS_FILL', SECRET, 1, 999, True, False, [],
        SimpleNamespace(name='EXECUTION_REPORT_STATUS_FILL'), BUY,
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_UNSPECIFIED)],
    *[(field, value, 'invalid order lot counts')
      for field in ('lots_requested', 'lots_executed')
      for value in (None, True, False, -1, '2', SECRET, 2.0, Decimal(2), [])],
    ('lots_requested', 1, 'invalid order lot counts'),
    ('lots_executed', 3, 'invalid order lot counts'),
    *[(field, MISSING, reason) for field, reason in (
        ('instrument_uid', 'invalid order identity'), ('order_id', 'invalid order identity'),
        ('direction', 'invalid order direction'),
        ('execution_report_status', 'invalid order status'),
        ('lots_requested', 'invalid order lot counts'),
        ('lots_executed', 'invalid order lot counts'))],
]


def sdk_response(method, **changes):
    """A complete real SDK DTO; corrupt only the field under examination."""
    response_type = PostOrderResponse if method == 'submit_order' else OrderState
    return response_type(**{
        'instrument_uid': 'expected-uid', 'direction': SELL, 'order_id': 'broker-id',
        'execution_report_status': FILL, 'lots_requested': 2, 'lots_executed': 2,
        **changes,
    })


def corrupted_response(method, field, value):
    """Missing-field objects are deliberately malformed, never positive fixtures."""
    response = sdk_response(method)
    if value is MISSING:
        return SimpleNamespace(**{name: getattr(response, name) for name in FIELDS
                                  if name != field})
    setattr(response, field, value)
    return response


def sdk_executor():
    """Strict service surface, no SDK Client construction or network."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    factory = Mock(return_value='request-id')
    executor = TInvestOrderExecutor(SimpleNamespace(orders=orders), order_id_factory=factory)
    return executor, orders, factory


def invoke(executor, method, side='SELL'):
    """Exercise public adapter methods with a valid independent intent."""
    if method == 'submit_order':
        return executor.submit_order('dst', OrderIntent(
            'expected-uid', side, 2, 1, {(): Decimal(2)}))
    return executor.get_order_state('dst', 'broker-id')


def expected_call(method, side='SELL'):
    """Every public conversion consumes precisely one SDK response."""
    if method == 'submit_order':
        return call.post_order(
            account_id='dst', instrument_id='expected-uid', quantity=2,
            direction=BUY if side == 'BUY' else SELL,
            order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='request-id')
    return call.get_order_state(account_id='dst', order_id='broker-id')


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('side,direction', [('BUY', BUY), ('SELL', SELL)])
@pytest.mark.parametrize('status_case', STATUSES)
def test_supported_statuses_preserve_broker_facts(method, side, direction, status_case):
    """Valid pending/rejected responses stay factual rather than becoming FILL."""
    executor, orders, factory = sdk_executor()
    status, name, executed = status_case
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = sdk_response(
        method, direction=direction, execution_report_status=status, lots_executed=executed)
    assert invoke(executor, method, side) == ExecutionReceipt(
        'expected-uid', side, 'broker-id', name, 2, executed, {})
    assert orders.mock_calls == [expected_call(method, side)]
    assert factory.mock_calls == ([call()] if method == 'submit_order' else [])


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('field,value,reason', BAD_FIELDS)
def test_malformed_fields_stop_with_order_error(method, field, value, reason, caplog):
    """Bad broker facts are execution failures, without coercion or unsafe diagnostics."""
    executor, orders, factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = corrupted_response(method, field, value)
    with pytest.raises(OrderExecutionError, match=reason) as raised:
        invoke(executor, method)
    assert raised.value.request_id == ('request-id' if method == 'submit_order' else None)
    identifier = 'request_id=request-id' if method == 'submit_order' else 'order_id=broker-id'
    assert any(record.levelno == logging.ERROR and identifier in record.getMessage()
               for record in caplog.records)
    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text
    assert orders.mock_calls == [expected_call(method)]
    assert factory.mock_calls == ([call()] if method == 'submit_order' else [])


@pytest.mark.parametrize('request_id', [None, 'request-id'])
def test_order_error_context_preserves_message_and_args(request_id):
    """Optional diagnostics preserve the existing exception text and positional API."""
    error = (OrderExecutionError('offline stop') if request_id is None else
             OrderExecutionError('offline stop', request_id=request_id))
    assert str(error) == 'offline stop'
    assert error.args == ('offline stop',)
    assert error.request_id == request_id
    pytest.raises(TypeError, OrderExecutionError, 'offline stop', 'positional-id')


@pytest.mark.parametrize('changes', [
    {'uid': ''}, {'side': 'HOLD'}, {'lots': 0}, {'lots': True}, {'lot_size': 0},
    {'pieces': {(): Decimal(1)}},
])
def test_invalid_intent_never_creates_request_id_or_calls_sdk(changes):
    """Validation precedes all submission side effects, including ID generation."""
    executor, orders, factory = sdk_executor()
    intent = replace(OrderIntent('expected-uid', 'SELL', 2, 1, {(): Decimal(2)}), **changes)
    with pytest.raises(ValueError):
        executor.submit_order('dst', intent)
    factory.assert_not_called()
    assert orders.mock_calls == []


@pytest.mark.parametrize('code', [StatusCode.DEADLINE_EXCEEDED, StatusCode.UNAVAILABLE])
def test_unknown_submission_keeps_request_context_without_retry(code, monkeypatch, caplog):
    """The sent ID survives transport failure without logging private error details."""
    executor, orders, factory = sdk_executor()
    error = RequestError(code, f'token={SECRET} kwargs={SECRET}', {'metadata': SECRET})
    orders.post_order.side_effect = error
    sleeper = Mock()
    monkeypatch.setattr('autorepeater.tinvest_requests.time.sleep', sleeper)
    with pytest.raises(OrderExecutionError) as raised:
        invoke(executor, 'submit_order')
    assert raised.value.request_id == 'request-id'
    assert raised.value.__cause__ is error
    assert str(raised.value) == 'order submission failed; stopping pass'
    assert any(record.levelno == logging.ERROR and 'request_id=request-id' in record.message
               and 'expected-uid' in record.message and 'SELL' in record.message
               and 'lots=2' in record.message for record in caplog.records)
    assert SECRET not in caplog.text
    assert SECRET not in str(raised.value)
    assert orders.mock_calls == [expected_call('submit_order')]
    assert factory.mock_calls == [call()]
    sleeper.assert_not_called()


def test_valid_response_logs_distinct_request_and_broker_facts(caplog):
    """Correlation uses the validated response, including a foreign UID and direction."""
    executor, orders, factory = sdk_executor()
    orders.post_order.return_value = sdk_response(
        'submit_order', instrument_uid='other-uid', direction=BUY, message=SECRET)
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        result = invoke(executor, 'submit_order')
    assert result == ExecutionReceipt('other-uid', 'BUY', 'broker-id', 'FILL', 2, 2, {})
    assert any(record.levelno == logging.INFO and 'request_id=request-id' in record.message
               and 'broker_order_id=broker-id' in record.message
               and 'uid=other-uid' in record.message and 'side=BUY' in record.message
               and 'status=FILL' in record.message and 'requested=2' in record.message
               and 'executed=2' in record.message for record in caplog.records)
    assert SECRET not in caplog.text
    assert orders.mock_calls == [expected_call('submit_order')]
    assert factory.mock_calls == [call()]


@pytest.mark.parametrize('method', METHODS)
def test_sdk_programming_error_propagates_unchanged(method):
    """Unexpected SDK errors are neither translated nor retried."""
    executor, orders, factory = sdk_executor()
    error = TypeError('programming error')
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.side_effect = error
    with pytest.raises(TypeError) as raised:
        invoke(executor, method)
    assert raised.value is error
    assert orders.mock_calls == [expected_call(method)]
    assert factory.mock_calls == ([call()] if method == 'submit_order' else [])


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('response', [None, SimpleNamespace()])
def test_missing_response_is_order_error(method, response):
    """An absent response must not escape as a generic data/programming error."""
    executor, orders, _factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = response
    with pytest.raises(OrderExecutionError, match='invalid order direction'):
        invoke(executor, method)
    assert orders.mock_calls == [expected_call(method)]


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('requested,executed', [(0, 0), (2, 0), (2, 1)])
def test_valid_counts_are_not_rewritten_to_match_intent(method, requested, executed):
    """Full confirmation belongs to the neutral executor, not DTO conversion."""
    executor, orders, _factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = sdk_response(
        method, lots_requested=requested, lots_executed=executed)
    result = invoke(executor, method)
    assert result == ExecutionReceipt(
        'expected-uid', 'SELL', 'broker-id', 'FILL', requested, executed, {})
    assert orders.mock_calls == [expected_call(method)]


@pytest.mark.parametrize('method', METHODS)
def test_foreign_identity_is_preserved_for_neutral_guard(method):
    """The converter returns facts even when they differ from the request."""
    executor, orders, _factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = sdk_response(
        method, instrument_uid='other-uid', direction=BUY, order_id='other-broker-id')
    assert invoke(executor, method) == ExecutionReceipt(
        'other-uid', 'BUY', 'other-broker-id', 'FILL', 2, 2, {})
    assert orders.mock_calls == [expected_call(method)]


@pytest.mark.parametrize('method', METHODS)
def test_identity_is_not_normalized(method):
    """Nonempty broker strings are preserved verbatim."""
    executor, orders, _factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = sdk_response(
        method, instrument_uid=' uid ', order_id=' broker ')
    result = invoke(executor, method)
    assert result == ExecutionReceipt(' uid ', 'SELL', ' broker ', 'FILL', 2, 2, {})


@pytest.mark.parametrize('method', METHODS)
def test_money_estimates_do_not_become_cash(method):
    """SDK monetary estimates cannot prove spendable sale proceeds."""
    executor, orders, _factory = sdk_executor()
    sdk_method = orders.post_order if method == 'submit_order' else orders.get_order_state
    sdk_method.return_value = sdk_response(
        method, executed_order_price=MoneyValue('rub', 500, 0),
        total_order_amount=MoneyValue('rub', 1000, 0),
        executed_commission=MoneyValue('rub', 10, 0))
    assert not invoke(executor, method).cash
    assert orders.mock_calls == [expected_call(method)]


def trading_case():
    """A real plan that would sell once, settle, then buy the replacement."""
    strategy = StrategyPlan(
        (), Decimal(10), TargetPortfolio({'buy-uid': Decimal(2)}, {'buy-uid': Decimal(1)}),
        Decimal(0), StrategyDecision(TradeMode.REBALANCE, False, 'test', None, None), (), {})
    quantities = {'expected-uid': Decimal(2)}
    snapshot = ExecutionSnapshot(
        PortfolioSnapshot(()), Decimal(10), quantities, {'expected-uid': Decimal(1)},
        {'rub': Decimal(8)}, dict(quantities), (), True)
    rules = dict.fromkeys(('expected-uid', 'buy-uid'),
                          TradeRules(1, 'rub', True, True, Decimal(10), 10, 10))
    plan = build_order_plan(strategy, snapshot, {(): quantities},
                            {'expected-uid': Decimal(1), 'buy-uid': Decimal(1)}, rules)
    assert [(intent.uid, intent.side) for intent in (*plan.sells, *plan.buys)] == [
        ('expected-uid', 'SELL'), ('buy-uid', 'BUY')]
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = replace(
        snapshot, quantities={}, available_quantities={}, available_cash={'rub': Decimal(10)})
    data.get_trade_rules.return_value = rules
    return plan, data


@pytest.fixture(name='clock')
def fixture_clock(monkeypatch):
    """Restore production polling bounds locally and advance time without waiting."""
    state = SimpleNamespace(now=0)
    def advance(seconds):
        state.now += seconds
    sleeper = Mock(side_effect=advance)
    monkeypatch.setattr(execution_module, 'SETTLEMENT_TIMEOUT', 30)
    monkeypatch.setattr(execution_module, 'SETTLEMENT_RETRY_INTERVAL', 2)
    monkeypatch.setattr(execution_module, 'monotonic', lambda: state.now)
    monkeypatch.setattr(execution_module, 'sleep', sleeper)
    return sleeper


@pytest.mark.parametrize('status,executed', [(FILL, 2), (NEW, 0), (PARTIAL, 1)])
@pytest.mark.parametrize('changes', [
    {'instrument_uid': 'other-uid'}, {'direction': BUY},
    {'instrument_uid': 'other-uid', 'direction': BUY}, {'lots_requested': 3},
])
def test_mismatched_sale_stops_before_polling_or_next_buy(status, executed, changes, clock):
    """A factual but mismatched initial receipt cannot authorize further activity."""
    plan, data = trading_case()
    executor, orders, factory = sdk_executor()
    orders.post_order.side_effect = [
        sdk_response('submit_order', execution_report_status=status, lots_executed=executed,
                     **changes),
        sdk_response('submit_order', instrument_uid='buy-uid', direction=BUY),
    ]
    orders.get_order_state.return_value = sdk_response('get_order_state')
    with pytest.raises(OrderExecutionError, match='order not fully confirmed'):
        execute_plan('dst', plan, data, executor)
    assert orders.mock_calls == [expected_call('submit_order')]
    assert data.mock_calls == []
    assert factory.mock_calls == [call()]
    clock.assert_not_called()


@pytest.mark.parametrize('stage', METHODS)
@pytest.mark.parametrize('field,value,reason', [
    ('instrument_uid', '', 'invalid order identity'),
    ('direction', True, 'invalid order direction'),
    ('order_id', None, 'invalid order identity'),
    ('execution_report_status', 1, 'invalid order status'),
    ('lots_requested', True, 'invalid order lot counts'),
    ('lots_executed', 3, 'invalid order lot counts'),
])
def test_malformed_receipt_stops_neutral_pass(stage, field, value, reason, clock):
    """Both response boundaries propagate the order error before the replacement BUY."""
    plan, data = trading_case()
    executor, orders, factory = sdk_executor()
    orders.post_order.return_value = (
        corrupted_response(stage, field, value) if stage == 'submit_order'
        else sdk_response('submit_order', execution_report_status=NEW, lots_executed=0))
    orders.get_order_state.return_value = corrupted_response(stage, field, value)
    with pytest.raises(OrderExecutionError, match=reason):
        execute_plan('dst', plan, data, executor)
    assert orders.mock_calls == [expected_call('submit_order')] + (
        [expected_call('get_order_state')] if stage == 'get_order_state' else [])
    assert data.mock_calls == []
    assert factory.mock_calls == [call()]
    assert clock.mock_calls == ([call(2)] if stage == 'get_order_state' else [])


@pytest.mark.parametrize('changes,reason', [
    pytest.param({'order_id': 'other-broker-id'}, 'sale order ID mismatch', id='broker-id'),
    pytest.param({'instrument_uid': 'other-uid'}, 'order not fully confirmed', id='uid'),
    pytest.param({'direction': BUY}, 'order not fully confirmed', id='side'),
    pytest.param({'lots_requested': 3}, 'order not fully confirmed', id='requested-count'),
    pytest.param({'lots_executed': 1}, 'order not fully confirmed', id='incomplete-fill'),
])
def test_mismatched_polling_facts_stop_before_refresh_or_next_buy(changes, reason, clock):
    """Valid SDK polling facts must still match the sale before the pass can continue."""
    plan, data = trading_case()
    executor, orders, factory = sdk_executor()
    orders.post_order.return_value = sdk_response(
        'submit_order', execution_report_status=NEW, lots_executed=0)
    orders.get_order_state.return_value = sdk_response('get_order_state', **changes)
    with pytest.raises(OrderExecutionError, match=reason):
        execute_plan('dst', plan, data, executor)
    assert orders.mock_calls == [expected_call('submit_order'), expected_call('get_order_state')]
    assert data.mock_calls == []
    assert factory.mock_calls == [call()]
    clock.assert_called_once_with(2)


@pytest.mark.parametrize('status,executed', [(NEW, 0), (PARTIAL, 1)])
def test_matching_pending_sale_polls_broker_id_then_buys(status, executed, clock):
    """Existing SELL polling still confirms factual pending receipts without resubmission."""
    plan, data = trading_case()
    executor, orders, factory = sdk_executor()
    orders.post_order.side_effect = [
        sdk_response('submit_order', execution_report_status=status, lots_executed=executed),
        sdk_response('submit_order', instrument_uid='buy-uid', direction=BUY,
                     order_id='buy-broker-id'),
    ]
    orders.get_order_state.return_value = sdk_response('get_order_state')
    result = execute_plan('dst', plan, data, executor)
    assert [(item.uid, item.side, item.order_id) for item in result] == [
        ('expected-uid', 'SELL', 'broker-id'), ('buy-uid', 'BUY', 'buy-broker-id')]
    assert orders.mock_calls == [expected_call('submit_order'), expected_call('get_order_state'),
                                call.post_order(
                                    account_id='dst', instrument_id='buy-uid', quantity=2,
                                    direction=BUY, order_type=OrderType.ORDER_TYPE_BESTPRICE,
                                    order_id='request-id')]
    assert data.mock_calls == [call.get_destination('dst'),
                               call.get_trade_rules('dst', ['buy-uid', 'expected-uid'])]
    assert factory.mock_calls == [call(), call()]
    clock.assert_called_once_with(2)
