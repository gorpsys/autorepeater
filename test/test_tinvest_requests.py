"""Only API rate-limit failures are retried, with unchanged request arguments."""
import inspect
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call, create_autospec, patch
from test.test_order_execution import sale_plan

import pytest
from grpc import StatusCode
from t_tech import invest
from t_tech.invest.services import OrdersService

from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.execution import OrderExecutionError
from autorepeater.order_plan import OrderIntent
from autorepeater.tinvest_execution_data import _read
from autorepeater.tinvest_requests import call_api


@pytest.mark.parametrize('attempts', [0, 1, 7, 101])
def test_rate_limit_retries_until_success(attempts, caplog):
    """No retry cap; each rejected attempt waits exactly ten seconds."""
    method = Mock(side_effect=[
        *[invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)
          for _ in range(attempts)], 'success'])
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep, caplog.at_level(
            logging.WARNING, logger='tinkoffBot'):
        assert call_api(method, account_id='dst', token='must-not-be-logged') == 'success'
    assert sleep.call_args_list == [call(10)] * attempts
    assert method.call_args_list == [
        call(account_id='dst', token='must-not-be-logged')] * (attempts + 1)
    assert len(caplog.records) == attempts
    assert all('must-not-be-logged' not in record.message for record in caplog.records)


@pytest.mark.parametrize('error', [
    invest.RequestError(StatusCode.UNAVAILABLE, 'transport', None),
    invest.RequestError(StatusCode.DEADLINE_EXCEEDED, 'timeout', None),
    invest.RequestError(StatusCode.UNAUTHENTICATED, 'credentials', None),
    ValueError('bad data'), TypeError('programming error'),
])
def test_other_errors_stop_retries(error):
    """The first non-quota error, after any number of quota failures, propagates."""
    method = Mock(side_effect=[
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None), error, 'never'])
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep:
        with pytest.raises(type(error)) as captured:
            call_api(method)
    assert captured.value is error
    sleep.assert_called_once_with(10)
    assert method.call_count == 2


def test_post_order_retries_keep_one_idempotency_key():
    """Rate-limited submissions repeat the same intent and order_id, not new orders."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    orders.post_order.side_effect = [
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None),
        invest.PostOrderResponse(
            instrument_uid='X', direction=invest.OrderDirection.ORDER_DIRECTION_SELL,
            order_id='id', lots_requested=10, lots_executed=10, execution_report_status=(
                invest.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL))]
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep, patch(
            'autorepeater.order_execution.uuid4', return_value='request-id') as key:
        result = TInvestOrderExecutor(SimpleNamespace(orders=orders)).submit_order(
            'dst', sale_plan().sells[0])
    assert result.status == 'FILL'
    key.assert_called_once_with()
    sleep.assert_called_once_with(10)
    assert orders.mock_calls == [call.post_order(
        account_id='dst', instrument_id='X', quantity=10,
        direction=invest.OrderDirection.ORDER_DIRECTION_SELL,
        order_type=invest.OrderType.ORDER_TYPE_BESTPRICE, order_id='request-id')] * 2


@pytest.mark.parametrize('code', [StatusCode.DEADLINE_EXCEEDED, StatusCode.UNAVAILABLE])
def test_post_order_quota_then_failure_preserves_request_and_cause(code, caplog):
    """Quota retries stop on transport failure with the original submission context."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    secret = 'private-transport-details'
    error = invest.RequestError(code, secret, {'token': secret})
    orders.post_order.side_effect = [
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, secret, None), error,
        invest.PostOrderResponse(),
    ]
    factory = Mock(return_value='request-id')
    executor = TInvestOrderExecutor(SimpleNamespace(orders=orders), order_id_factory=factory)
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep:
        with pytest.raises(OrderExecutionError) as raised:
            executor.submit_order('dst', OrderIntent('X', 'SELL', 10, 1, {(): Decimal(10)}))
    assert raised.value.request_id == 'request-id'
    assert raised.value.__cause__ is error
    assert factory.mock_calls == [call()]
    assert sleep.mock_calls == [call(10)]
    assert orders.mock_calls == [call.post_order(
        account_id='dst', instrument_id='X', quantity=10,
        direction=invest.OrderDirection.ORDER_DIRECTION_SELL,
        order_type=invest.OrderType.ORDER_TYPE_BESTPRICE, order_id='request-id')] * 2
    assert any(record.levelno == logging.ERROR and 'request_id=request-id' in record.message
               for record in caplog.records)
    assert secret not in caplog.text


def test_execution_reads_use_shared_quota_wrapper():
    """All account, status and own-cap reads funnel through the execution reader."""
    method = Mock(side_effect=[
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None), 'read'])
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep:
        assert _read(method, account_id='dst') == 'read'
    sleep.assert_called_once_with(10)
    assert method.call_args_list == [call(account_id='dst')] * 2


def test_order_status_quota_retries_preserve_broker_id():
    """Pending sale status checks also retry quota errors without submitting again."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    orders.get_order_state.side_effect = [
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None),
        invest.OrderState(
            order_id='broker-id', instrument_uid='uid',
            direction=invest.OrderDirection.ORDER_DIRECTION_SELL,
            lots_requested=10, lots_executed=10, execution_report_status=(
                invest.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL)),
    ]
    with patch('autorepeater.tinvest_requests.time.sleep') as sleep:
        result = TInvestOrderExecutor(SimpleNamespace(orders=orders)).get_order_state(
            'dst', 'broker-id')
    assert result.status == 'FILL'
    sleep.assert_called_once_with(10)
    assert orders.mock_calls == [
        call.get_order_state(account_id='dst', order_id='broker-id')] * 2
