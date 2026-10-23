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
from autorepeater import grpc_deadline
from autorepeater.order_plan import OrderIntent
from autorepeater.tinvest_execution_data import _read
from autorepeater.tinvest_requests import call_api


@pytest.mark.parametrize('code,status', [
    (StatusCode.DEADLINE_EXCEEDED, 'DEADLINE_EXCEEDED'),
    (StatusCode.INTERNAL, 'INTERNAL'), ('private-code', 'UNKNOWN'),
])
def test_request_failure_logs_method_status_and_duration_only(monkeypatch, caplog, code, status):
    """Diagnostics must identify the timed-out RPC without logging SDK text or arguments."""
    secret = 'private-details-and-token'
    error = invest.RequestError(code, secret, {'authorization': secret})
    calls = []

    def shares(**kwargs):
        calls.append(kwargs)
        raise error

    clock = iter([100, 110.125])
    monkeypatch.setattr('autorepeater.tinvest_requests.time.perf_counter', lambda: next(clock))
    with caplog.at_level(logging.ERROR, logger='tinkoffBot'):
        with pytest.raises(invest.RequestError) as raised:
            call_api(shares, token=secret)
    assert raised.value is error
    assert calls == [{'token': secret}]
    assert [record.message for record in caplog.records] == [
        f'API request failed: method=shares status={status} elapsed_seconds=10.125']
    assert secret not in caplog.text
    record, = caplog.records
    assert (record.api_method, record.grpc_status, record.elapsed_seconds) == (
        'shares', status, 10.125)


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


@pytest.fixture(name='budget_time')
def fixture_budget_time(monkeypatch):
    """Advance only local monotonic time and quota sleeps, never wall time."""
    now = [100.0]

    def advance(seconds):
        now[0] += seconds

    sleep = Mock(side_effect=advance)
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: now[0], raising=False)
    monkeypatch.setattr('autorepeater.tinvest_requests.time.sleep', sleep)
    return now, advance, sleep


@pytest.mark.parametrize('prior_success', [False, True])
def test_scope_expiration_stops_read_without_transport_cause(budget_time, prior_success):
    """Exhaustion before the first or between successful RPCs is a local timeout."""
    _, advance, sleep = budget_time
    method = Mock(return_value='response')
    with grpc_deadline.read_deadline(30):
        if prior_success:
            assert call_api(method, account_id='first') == 'response'
        advance(30)
        with pytest.raises(TimeoutError, match='^SDK read deadline expired$') as raised:
            call_api(method, account_id='never')
    assert raised.value.__cause__ is None
    assert not isinstance(raised.value, invest.RequestError)
    assert method.call_args_list == ([call(account_id='first')] if prior_success else [])
    sleep.assert_not_called()
    assert call_api(method, account_id='next-account') == 'response'


@pytest.mark.parametrize('budget,waits', [
    (3, [3]), (10, [10]), (15, [10, 5]), (30, [10, 10, 10]),
])
def test_quota_waits_and_retries_share_budget(budget_time, caplog, budget, waits):
    """Every retry spends the original budget; the last real quota error is the cause."""
    now, _, sleep = budget_time
    secret = 'private-quota-details-and-token'
    errors = [invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, secret, {'token': secret})
              for _ in waits]
    method = Mock(side_effect=[*errors, 'never'])
    with grpc_deadline.read_deadline(budget):
        with pytest.raises(TimeoutError, match='^SDK read deadline expired$') as raised:
            call_api(method, account_id='dst', token=secret)
    assert raised.value.__cause__ is errors[-1]
    assert raised.value.__cause__.code == StatusCode.RESOURCE_EXHAUSTED
    assert method.call_args_list == [call(account_id='dst', token=secret)] * len(waits)
    assert sleep.call_args_list == [call(seconds) for seconds in waits]
    assert now[0] == 100 + budget
    assert secret not in caplog.text
    assert secret not in str(raised.value)


def test_quota_rpc_time_also_reduces_wait_budget(budget_time):
    """Slow rejected requests leave less time for the following quota pause."""
    now, advance, sleep = budget_time
    errors = [invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)
              for _ in range(2)]
    durations = iter([7, 4])
    rejections = iter(errors)

    def read():
        advance(next(durations))
        raise next(rejections)

    method = Mock(side_effect=read)
    with grpc_deadline.read_deadline(30):
        with pytest.raises(TimeoutError) as raised:
            call_api(method)
    assert raised.value.__cause__ is errors[-1]
    assert method.call_args_list == [call(), call()]
    assert sleep.call_args_list == [call(10), call(9)]
    assert now[0] == 130


def test_quota_exhausting_budget_does_not_sleep(budget_time):
    """An RPC that consumes the last instant cannot trigger a further quota pause."""
    _, advance, sleep = budget_time
    error = invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)

    def read():
        advance(30)
        raise error

    method = Mock(side_effect=read)
    with grpc_deadline.read_deadline(30):
        with pytest.raises(TimeoutError) as raised:
            call_api(method)
    assert raised.value.__cause__ is error
    method.assert_called_once_with()
    sleep.assert_not_called()


def test_quota_sleep_overshoot_does_not_start_retry(budget_time):
    """Scheduler delays cannot authorize a new request after the absolute deadline."""
    _, advance, sleep = budget_time
    sleep.side_effect = lambda _: advance(31)
    error = invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)
    method = Mock(side_effect=[error, 'never'])
    with grpc_deadline.read_deadline(30):
        with pytest.raises(TimeoutError) as raised:
            call_api(method)
    assert raised.value.__cause__ is error
    method.assert_called_once_with()
    sleep.assert_called_once_with(10)


def test_quota_retry_can_succeed_with_unchanged_arguments(budget_time):
    """Successful retries do not reset the budget for the next read."""
    _, advance, sleep = budget_time
    method = Mock(side_effect=[
        invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None), 'response'])
    with grpc_deadline.read_deadline(30):
        assert call_api(method, account_id='dst') == 'response'
        assert grpc_deadline.remaining_read_timeout() == 20
        advance(17)
        assert call_api(method=Mock(return_value='next')) == 'next'
        assert grpc_deadline.remaining_read_timeout() == 3
    assert method.call_args_list == [call(account_id='dst')] * 2
    sleep.assert_called_once_with(10)
    assert grpc_deadline.remaining_read_timeout() is None


def test_interceptor_expiration_on_retry_keeps_actual_quota_cause(budget_time):
    """The scope may expire between the wrapper check and unary interception."""
    _, advance, sleep = budget_time
    error = invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)
    continuation = Mock()

    def retry():
        if method.call_count == 1:
            raise error
        advance(20)
        return grpc_deadline.UnaryDeadlineInterceptor().intercept_unary_unary(
            continuation, SimpleNamespace(timeout=None), 'never')

    method = Mock(side_effect=retry)
    with grpc_deadline.read_deadline(30):
        with pytest.raises(TimeoutError) as raised:
            call_api(method)
    assert raised.value.__cause__ is error
    assert method.call_args_list == [call(), call()]
    sleep.assert_called_once_with(10)
    continuation.assert_not_called()


@pytest.mark.parametrize('quota_first', [False, True])
@pytest.mark.parametrize('code', [StatusCode.DEADLINE_EXCEEDED, StatusCode.UNAVAILABLE,
                                  StatusCode.INTERNAL, StatusCode.UNAUTHENTICATED])
def test_scoped_transport_error_retains_identity_at_deadline(budget_time, quota_first, code):
    """An actual transport failure wins over local expiration, without new retries."""
    _, advance, sleep = budget_time
    error = invest.RequestError(code, 'private-transport-details', None)

    def fail():
        if quota_first and method.call_count == 1:
            raise invest.RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota', None)
        advance(30)
        raise error

    method = Mock(side_effect=fail)
    with grpc_deadline.read_deadline(30):
        with pytest.raises(invest.RequestError) as raised:
            call_api(method)
    assert raised.value is error
    assert method.call_count == (2 if quota_first else 1)
    assert sleep.call_args_list == ([call(10)] if quota_first else [])
