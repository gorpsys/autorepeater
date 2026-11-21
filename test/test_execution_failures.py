"""Fatal exits retain exactly the fills accepted in the current pass."""
from dataclasses import replace
from test.test_order_execution import fixture_clock, receipt, sale_plan
from unittest.mock import call, create_autospec

import pytest

from autorepeater import execution
from autorepeater.execution import OrderExecutionError, OrderExecutor, execute_plan
from autorepeater.execution_data import ExecutionData

__all__ = ['fixture_clock']


@pytest.mark.parametrize('error_type', [OrderExecutionError, TimeoutError])
def test_submission_error_uses_only_the_current_pass_receipts(error_type):
    """Reusing the same exception cannot carry a previous pass's fill forward."""
    plan = sale_plan()
    filled = receipt(plan.sells[0])
    error = error_type('submission failed')
    cause = OSError('offline')
    error.__cause__ = cause
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = [filled, error, error]
    with pytest.raises(error_type) as caught:
        execute_plan('offline', plan, data, executor)
    assert caught.value is error and error.__cause__ is cause
    assert execution.confirmed_receipts_from_error(error) == (filled,)
    with pytest.raises(error_type) as caught:
        execute_plan('offline', plan, data, executor)
    assert caught.value is error and error.__cause__ is cause
    assert execution.confirmed_receipts_from_error(error) is None
    assert executor.mock_calls == [call.submit_order('offline', order) for order in (
        *plan.sells, plan.sells[0])]
    assert not data.mock_calls


def test_invalid_second_receipt_stops_with_the_first_fill():
    """An unexpected FILL response cannot confirm the intent or extend the queue."""
    plan = sale_plan()
    filled = receipt(plan.sells[0])
    invalid = replace(receipt(plan.sells[1]), uid='unexpected')
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = [filled, invalid]
    with pytest.raises(OrderExecutionError) as caught:
        execute_plan('offline', plan, data, executor)
    assert execution.confirmed_receipts_from_error(caught.value) == (filled,)
    assert executor.mock_calls == [call.submit_order('offline', order) for order in plan.sells]
    assert not data.mock_calls


def test_polling_order_error_keeps_first_fill(clock):
    """Preserve identity, request ID and traceback for the second intent's poll failure."""
    plan = sale_plan()
    filled = receipt(plan.sells[0])
    pending = replace(receipt(plan.sells[1], 'NEW', executed=0), order_id='pending')
    error = OrderExecutionError('poll failed', request_id='offline-request')
    cause = OSError('offline')

    def fail_poll(_account, _order):
        raise error from cause

    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = [filled, pending]
    executor.get_order_state.side_effect = fail_poll
    with pytest.raises(OrderExecutionError) as caught:
        execute_plan('offline', plan, data, executor)
    assert caught.value is error and error.__cause__ is cause
    assert caught.traceback[-1].name == 'fail_poll'
    assert error.request_id == 'offline-request'
    assert execution.confirmed_receipts_from_error(error) == (filled,)
    assert executor.mock_calls == [*(call.submit_order('offline', order) for order in plan.sells),
                                   call.get_order_state('offline', 'pending')]
    assert not data.mock_calls
    clock.assert_called_once_with(2)


def test_reused_submission_timeout_does_not_attach_fills_when_polling(clock):
    """Only a timeout raised directly by submit_order carries the current receipts."""
    plan = sale_plan()
    filled = receipt(plan.sells[0])
    pending = replace(receipt(plan.sells[1], 'NEW', executed=0), order_id='pending')
    error = TimeoutError('offline timeout')
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = [filled, error, filled, pending]
    executor.get_order_state.side_effect = error
    with pytest.raises(TimeoutError) as caught:
        execute_plan('offline', plan, data, executor)
    assert caught.value is error
    assert execution.confirmed_receipts_from_error(error) == (filled,)
    with pytest.raises(TimeoutError) as caught:
        execute_plan('offline', plan, data, executor)
    assert caught.value is error
    assert execution.confirmed_receipts_from_error(error) is None
    assert executor.mock_calls == [*(call.submit_order('offline', order)
                                     for order in plan.sells * 2),
                                   call.get_order_state('offline', 'pending')]
    assert not data.mock_calls
    clock.assert_called_once_with(2)


@pytest.mark.parametrize('error_type,value', [
    (OrderExecutionError, 'not a tuple'), (TimeoutError, ('not a receipt',)),
    (ValueError, ()),
])
def test_helper_ignores_unsupported_context(error_type, value):
    """Only supported exceptions with an actual tuple of receipts are readable."""
    error = error_type('offline error')
    error.__dict__['confirmed_receipts'] = value
    assert execution.confirmed_receipts_from_error(error) is None
