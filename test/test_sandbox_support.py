"""Offline setup and failure diagnostics exercise real SDK-shaped responses."""
import inspect
import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import (GetOrdersResponse, MoneyValue, Operation, OperationsResponse,
                           OperationState, OperationTrade, OperationType, OrderDirection,
                           OrderExecutionReportStatus, OrderState, OrderType, PostOrderResponse,
                           RequestError)
from t_tech.invest.services import OperationsService, OrdersService, SandboxService

from autorepeater.execution_data import ExecutionData, ExecutionSnapshot
from autorepeater.strategy_data import (InstrumentInfo, InstrumentMatch, InstrumentType,
                                        PriceQuote, StrategyData, DataAccessError)

from scripts.sandbox_lifecycle import SandboxFailure, list_exception_facts, safe_call
from scripts.sandbox_support import SandboxSession, write_failure_dump
from scripts import sandbox_support


@pytest.mark.parametrize('details,expected', [
    ('70001', '70001'), ('private token value', None),
])
def test_exception_facts_preserve_transport_code_without_secrets(details, expected):
    """Safe evidence retains numeric API codes, never arbitrary details or metadata."""
    cause = RequestError(StatusCode.INTERNAL, details, {'authorization': 'private token'})
    error = DataAccessError('private transport representation')
    error.__cause__ = cause
    facts = list_exception_facts(error)
    assert [item['type'] for item in facts] == ['DataAccessError', 'RequestError']
    assert facts[1]['grpc_status'] == 'INTERNAL'
    assert facts[1].get('api_code') == expected
    assert 'private' not in json.dumps(facts)


def test_exception_facts_bound_cycles_and_respect_suppressed_context():
    """Malformed cause chains terminate and suppressed unrelated context stays hidden."""
    error = RuntimeError('private')
    error.__cause__ = error
    assert len(list_exception_facts(error)) == 1
    error.__cause__ = None
    error.__context__ = ValueError('private')
    error.__suppress_context__ = True
    assert len(list_exception_facts(error)) == 1
    error.__suppress_context__ = False
    assert len(list_exception_facts(error)) == 2


def test_exception_facts_capture_application_location_only(monkeypatch):
    """The transport call site is useful without locals, source lines or SDK frames."""
    from autorepeater.tinvest_requests import call_api  # pylint: disable=import-outside-toplevel

    def failing_api(**_):
        raise RequestError(StatusCode.DEADLINE_EXCEEDED, 'private', {'token': 'private'})

    monkeypatch.setattr(sandbox_support.time, 'sleep', lambda _: None)
    with pytest.raises(RequestError) as raised:
        call_api(failing_api)
    facts = list_exception_facts(raised.value)
    frame, = facts[0]['frames']
    assert (frame['file'], frame['function']) == ('autorepeater/tinvest_requests.py', 'call_api')
    assert frame['line'] > 0
    assert 'private' not in json.dumps(facts)


@pytest.fixture(name='session')
def fixture_session():
    """No real client construction or network."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    sandbox = create_autospec(inspect.unwrap(SandboxService), instance=True, spec_set=True)
    operations = create_autospec(inspect.unwrap(OperationsService), instance=True, spec_set=True)
    client = SimpleNamespace(orders=orders, sandbox=sandbox, operations=operations)
    lifecycle = SimpleNamespace(created=['dst'])
    return SandboxSession(client, lifecycle, order_id_factory=lambda: 'stable-request')


def test_pay_in_keeps_nano_and_never_retries(session):
    """The broker receives exactly the requested units and nano once."""
    session.pay_in('dst', Decimal('2000.000000001'))
    amount = session.client.sandbox.sandbox_pay_in.call_args.kwargs['amount']
    assert (amount.currency, amount.units, amount.nano) == ('rub', 2000, 1)
    session.client.sandbox.sandbox_pay_in.assert_called_once_with(
        account_id='dst', amount=amount)


def test_setup_requires_actual_broker_fill_before_positions(session, monkeypatch):
    """Setup cannot enter the app with only assumed fills or stale positions."""
    response = PostOrderResponse(
        order_id='broker', instrument_uid='uid', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=2, lots_executed=2)
    session.client.orders.post_order.return_value = response
    session.client.orders.get_order_state.return_value = OrderState(
        order_id='broker', instrument_uid='uid', direction=response.direction,
        execution_report_status=response.execution_report_status, lots_requested=2, lots_executed=2)
    monkeypatch.setattr(session, 'state', lambda _: SimpleNamespace(quantities={}))
    settled = []
    monkeypatch.setattr(session, 'wait_positions', lambda *args: settled.append(args))
    instrument = SimpleNamespace(uid='uid', lot=10)
    session.buy_setup('dst', instrument, 2)
    session.client.orders.post_order.assert_called_once_with(
        account_id='dst', instrument_id='uid', quantity=2,
        direction=OrderDirection.ORDER_DIRECTION_BUY,
        order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='stable-request')
    session.client.orders.get_order_state.assert_called_once_with(
        account_id='dst', order_id='broker')
    assert settled == [('dst', {'uid': Decimal(20)})]


def test_malformed_setup_response_stops_without_retry(session, monkeypatch):
    """A broker response for another UID must not finance later setup."""
    monkeypatch.setattr(session, 'state', lambda _: SimpleNamespace(quantities={}))
    session.client.orders.post_order.return_value = PostOrderResponse(
        order_id='broker', instrument_uid='other', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=1, lots_executed=1)
    with pytest.raises(SandboxFailure, match='identity'):
        session.buy_setup('dst', SimpleNamespace(uid='uid', lot=1), 1)
    session.client.orders.post_order.assert_called_once()
    session.client.orders.get_order_state.assert_not_called()


def test_dump_failure_cannot_prevent_cleanup(tmp_path):
    """Even repeated broken account reads leave safe evidence and allow finally cleanup."""
    events = []

    def broken_dump(_):
        events.append('dump')
        raise RuntimeError('private transport details')
    session = SimpleNamespace(lifecycle=SimpleNamespace(created=['one', 'two']),
                              diagnostic=broken_dump, configs={'name': 'TEST'}, quotes={},
                              records=[], scenario='initial')

    def cleanup():
        events.append('cleanup')
    try:
        write_failure_dump(session, tmp_path / 'failure.json')
    finally:
        cleanup()
    assert events == ['dump', 'dump', 'cleanup']
    dump = json.loads((tmp_path / 'failure.json').read_text())
    assert dump['accounts'] == {'one': {'error': 'RuntimeError'}, 'two': {'error': 'RuntimeError'}}
    assert 'private' not in json.dumps(dump)


@pytest.mark.parametrize('amount', [Decimal(0), Decimal('-1'), Decimal('NaN'),
                                    Decimal('.0000000001'), 2000])
def test_invalid_pay_in_never_calls_sdk(session, amount):
    """Bad money cannot become a rounded or uncertain broker mutation."""
    with pytest.raises(ValueError):
        session.pay_in('dst', amount)
    session.client.sandbox.sandbox_pay_in.assert_not_called()


def test_pay_in_transport_failure_never_retries(session):
    """Pay-in lacks idempotency, so an unknown result stops setup immediately."""
    session.client.sandbox.sandbox_pay_in.side_effect = RuntimeError('private')
    with pytest.raises(SandboxFailure):
        session.pay_in('dst', Decimal(2000))
    session.client.sandbox.sandbox_pay_in.assert_called_once()


@pytest.mark.parametrize('failure', ['none', 'no_time', 'empty', 'price', 'currency',
                                     'lot', 'quoteuid'])
def test_setup_exact_ticker_full_metadata_and_quote(session, monkeypatch, failure):
    """Only exact eligible metadata may fund real setup, never a silent replacement."""
    from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = [
        InstrumentMatch('wrong', 'OTHER', 'other', InstrumentType.ETF, 'TQTF'),
        InstrumentMatch('uid', 'GOLD', 'gold', InstrumentType.ETF, 'TQTF')]
    data.get_instrument.return_value = InstrumentInfo(
        'uid', 'GOLD', 'gold', InstrumentType.ETF, 'TQTF',
        0 if failure == 'lot' else 10, 'usd' if failure == 'currency' else 'rub',
        failure != 'empty')
    data.get_last_prices.return_value = [PriceQuote(
        'wrong' if failure == 'quoteuid' else 'uid',
        Decimal(0) if failure == 'price' else Decimal('1.000000001'),
        None if failure == 'no_time' else datetime(2026, 10, 7, tzinfo=timezone.utc))]
    monkeypatch.setattr(session, 'data', data)
    if failure in ('none', 'no_time'):
        assert session.instrument('GOLD').uid == 'uid'
        assert session.quotes['GOLD'].price == '1.000000001'
        assert session.quotes['GOLD'].time == (
            None if failure == 'no_time' else '2026-10-07T00:00:00+00:00')
        data.get_last_prices.assert_called_once_with(['uid'])
    else:
        with pytest.raises(SandboxFailure):
            session.instrument('GOLD')
    data.get_instrument.assert_called_once_with('uid')
    data.find_instruments.assert_called_once_with('GOLD')


def test_fresh_position_polling_and_timeout(session, monkeypatch):
    """Physical equality and readiness are both required; timeout never becomes a skip."""
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    pending = ExecutionSnapshot(None, Decimal(100), {}, {}, {'rub': Decimal(100)}, {}, (), False)
    ready = ExecutionSnapshot(None, Decimal(100), {'uid': Decimal(20)}, {}, {}, {}, (), True)
    execution.get_destination.side_effect = [pending, ready, pending]
    monkeypatch.setattr(session, 'execution', execution)
    clock = iter([0, 1, 2, 33])
    monkeypatch.setattr(session, 'monotonic', lambda: next(clock))
    delays = []
    monkeypatch.setattr(session, 'sleep', delays.append)
    assert session.wait_positions('dst', {'uid': Decimal(20)}) is ready
    with pytest.raises(SandboxFailure, match='settle'):
        session.wait_positions('dst', {'uid': Decimal(20)})
    assert delays == [2]
    assert all(call.kwargs == {'account_id': 'dst'}
               for call in execution.get_destination.call_args_list)


@pytest.mark.parametrize('status', ['NEW', 'REJECTED', 'SHORTFILL', 'BROKERNEW'])
def test_pending_setup_only_polls_original_broker_id(session, monkeypatch, status):
    """A delayed fill is read using the existing broker ID; no second PostOrder."""
    pending = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW
    rejected = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED
    fill = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
    response = PostOrderResponse(
        order_id='broker', instrument_uid='uid', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=rejected if status == 'REJECTED' else pending,
        lots_requested=2, lots_executed=0)
    state = OrderState(order_id='broker', instrument_uid='uid', direction=response.direction,
                       execution_report_status=pending if status == 'BROKERNEW' else fill,
                       lots_requested=2, lots_executed=1 if status == 'SHORTFILL' else 2)
    session.client.orders.post_order.return_value = response
    session.client.orders.get_order_state.return_value = state
    monkeypatch.setattr(session, 'state', lambda _: SimpleNamespace(quantities={}))
    monkeypatch.setattr(session, 'wait_positions', lambda *_: None)
    monkeypatch.setattr(session, 'sleep', lambda _: None)
    clock = iter([0, 1, 40])
    monkeypatch.setattr(session, 'monotonic', lambda: next(clock))
    if status == 'NEW':
        session.buy_setup('dst', SimpleNamespace(uid='uid', lot=10), 2)
        assert session.client.orders.get_order_state.call_count == 2
        assert all(call.kwargs == {'account_id': 'dst', 'order_id': 'broker'}
                   for call in session.client.orders.get_order_state.call_args_list)
    else:
        with pytest.raises(SandboxFailure):
            session.buy_setup('dst', SimpleNamespace(uid='uid', lot=10), 2)
    session.client.orders.post_order.assert_called_once()


def test_setup_final_state_must_still_be_fill(session, monkeypatch):
    """A PostOrder FILL cannot override a contradictory fresh broker state."""
    session.client.orders.post_order.return_value = PostOrderResponse(
        order_id='broker', instrument_uid='uid', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=1, lots_executed=1)
    session.client.orders.get_order_state.return_value = OrderState(
        order_id='broker', instrument_uid='uid', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
        lots_requested=1, lots_executed=0)
    monkeypatch.setattr(session, 'state', lambda _: SimpleNamespace(quantities={}))
    with pytest.raises(SandboxFailure, match='broker state'):
        session.buy_setup('dst', SimpleNamespace(uid='uid', lot=10), 1)


@pytest.mark.parametrize('lots', [0, True, Decimal(2), -1])
def test_invalid_setup_lots_never_submit(session, lots):
    """Invalid requests cannot allocate a request ID or mutate the account."""
    with pytest.raises(ValueError):
        session.buy_setup('dst', SimpleNamespace(uid='uid', lot=10), lots)
    session.client.orders.post_order.assert_not_called()


def test_setup_allocates_one_id_and_none_for_invalid_lots(session, monkeypatch):
    """An injected ID is allocated exactly once, even if PostOrder fails."""
    ids = create_autospec(lambda: 'stable-request', spec_set=True,
                          return_value='stable-request')
    session.order_id_factory = ids
    monkeypatch.setattr(session, 'state', lambda _: SimpleNamespace(quantities={}))
    instrument = SimpleNamespace(uid='uid', lot=10)
    with pytest.raises(ValueError):
        session.buy_setup('dst', instrument, 0)
    ids.assert_not_called()
    session.client.orders.post_order.side_effect = RequestError(
        StatusCode.FAILED_PRECONDITION, '30052', {'token': 'private'})
    with pytest.raises(SandboxFailure):
        session.buy_setup('dst', instrument, 1)
    ids.assert_called_once_with()
    session.client.orders.post_order.assert_called_once_with(
        account_id='dst', instrument_id='uid', quantity=1,
        direction=OrderDirection.ORDER_DIRECTION_BUY,
        order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='stable-request')


@pytest.mark.parametrize('details,code', [('30052', '30052'), ('private token', None)])
def test_setup_error_preserves_only_safe_transport_facts(details, code):
    """Setup failures retain status/code without an unsafe chained traceback."""
    def post_order(**_):
        raise RequestError(StatusCode.FAILED_PRECONDITION, details, {'token': 'private'})

    with pytest.raises(SandboxFailure) as caught:
        safe_call(post_order, order_id='stable-request')
    facts = list_exception_facts(caught.value)
    transport = next(item for item in facts if 'grpc_status' in item)
    assert transport['grpc_status'] == 'FAILED_PRECONDITION'
    assert transport.get('api_code') == code
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert 'private' not in str(caught.value) + json.dumps(facts)


def test_order_journal_observes_only_verified_app_facts():
    """Diagnostics record IDs and ordering without capturing arbitrary SDK log details."""
    journal = sandbox_support.OrderJournal()
    messages = [
        'private transport details',
        'Order response: request_id=req broker_order_id=broker uid=uid side=BUY '
        'status=FILL requested=2 executed=2',
        'Submitting order: account=dst uid=uid side=BUY lots=2',
        'Confirmed order: uid=uid side=BUY order_id=broker status=FILL requested=2 executed=2',
    ]
    for message in messages:
        journal.emit(logging.LogRecord('tinkoffBot', logging.INFO, '', 0, message, (), None))
    assert [event['event'] for event in journal.events] == ['response', 'submit', 'confirm']
    assert journal.events[0]['request_id'] == 'req'
    assert journal.events[0]['requested'] == 2
    assert 'private' not in json.dumps(journal.events)


def test_order_journal_records_safe_rpc_failures_and_quota_waits():
    """The exact RPC and duration are retained, but arbitrary transport text is ignored."""
    journal = sandbox_support.OrderJournal()
    for message in (
            'API request failed: method=shares status=DEADLINE_EXCEEDED elapsed_seconds=10.125',
            'API rate limit exhausted: method=find_instrument; retrying in 10 seconds',
            'API request failed: method=shares private token'):
        journal.emit(logging.LogRecord('tinkoffBot', logging.ERROR, '', 0, message, (), None))
    assert len(journal.events) == 2
    failure, quota = journal.events[0], journal.events[1]
    assert {key: value for key, value in failure.items() if key != 'time'} == {
        'event': 'api_failure', 'method': 'shares', 'status': 'DEADLINE_EXCEEDED',
        'elapsed_seconds': 10.125}
    assert {key: value for key, value in quota.items() if key != 'time'} == {
        'event': 'quota_retry', 'method': 'find_instrument', 'delay_seconds': 10}
    assert 'private' not in json.dumps(journal.events)


def test_allowlisted_dump_reconstructs_cash_positions_and_operations(
        session, monkeypatch, tmp_path):
    """Explicit DTO fields retain nanos and trade evidence without raw SDK objects."""
    from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel
    stamp = datetime(2026, 10, 7, tzinfo=timezone.utc)
    money = MoneyValue(currency='rub', units=2, nano=1)
    order = OrderState(
        order_id='broker', instrument_uid='uid', direction=OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
        lots_requested=2, lots_executed=2)
    operation = Operation(id='op', instrument_uid='uid', figi='figi',
                          operation_type=OperationType.OPERATION_TYPE_BUY,
                          state=OperationState.OPERATION_STATE_EXECUTED,
                          quantity=20, quantity_rest=0, date=stamp, payment=money, price=money,
                          trades=[OperationTrade(
                              trade_id='trade', quantity=20, date_time=stamp, price=money)])
    session.client.orders.get_orders.return_value = GetOrdersResponse(orders=[order])
    session.client.operations.get_operations.return_value = OperationsResponse(
        operations=[operation])
    state = ExecutionSnapshot(None, Decimal('2.000000001'), {'uid': Decimal(20)},
                              {'uid': Decimal('2.000000001')}, {'rub': Decimal(5)}, {}, (), True)
    monkeypatch.setattr(session, 'state', lambda _: state)
    write_failure_dump(session, tmp_path / 'dump.json')
    document = json.loads((tmp_path / 'dump.json').read_text())
    account = document['accounts']['dst']
    assert account['positions'] == {'uid': '20'}
    assert account['marks'] == {'uid': '2.000000001'}
    assert account['cash'] == {'rub': '5'}
    assert account['operations'][0]['trades'][0]['trade_id'] == 'trade'
    assert account['orders'][0]['order_id'] == 'broker'
    session.client.operations.get_operations.assert_called_once_with(account_id='dst')
