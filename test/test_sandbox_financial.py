"""Financial observation preserves SDK calls and nano precision without leaking extras."""
from decimal import Decimal as D
import inspect
import json
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from t_tech.invest import schemas as sdk, services

from autorepeater.execution import OrderExecutionError
from autorepeater.order_execution import TInvestOrderExecutor
from scripts.sandbox_financial import observe_finances
from scripts.sandbox_multicurrency import NativeState, TradeFact, money_components


@pytest.mark.parametrize('method', ['post_order', 'get_order_state', 'get_positions'])
def test_observer_keeps_response_and_call_count(method):
    """Both broker clients share the class observer; only owned accounts are recorded."""
    service = inspect.unwrap(services.OperationsService if method == 'get_positions'
                             else services.OrdersService)
    client = object.__new__(service)
    response = (sdk.PositionsResponse(money=[sdk.MoneyValue(
        currency='cny', units=11815, nano=360295000)], blocked=[sdk.MoneyValue(
            currency='rub', units=2, nano=3)]) if method == 'get_positions' else
        (sdk.PostOrderResponse if method == 'post_order' else sdk.OrderState)(
            order_id='broker', instrument_uid='bond', lots_requested=67, lots_executed=67,
            direction=sdk.OrderDirection.ORDER_DIRECTION_SELL,
            execution_report_status=sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
            total_order_amount=sdk.MoneyValue(currency='cny', units=67264, nano=47000000),
            executed_commission=sdk.MoneyValue(currency='cny', units=33, nano=632023500),
            executed_order_price=sdk.MoneyValue(currency='cny', units=1003, nano=941000000)))
    session = SimpleNamespace(lifecycle=SimpleNamespace(created=['owned']), records=[],
                              scenario='research', monotonic=iter([10., 10.25]).__next__)
    original = create_autospec(getattr(service, method), spec_set=True, return_value=response)
    with patch.object(service, method, original):
        with observe_finances(session):
            assert getattr(client, method)(account_id='foreign') is response
            assert getattr(client, method)(account_id='owned') is response
        assert getattr(service, method) is original
    assert original.call_count == 2
    record, = session.records
    assert record['method'] == method and record['account_id'] == 'owned'
    assert record['monotonic_started'] == 10 and record['elapsed_seconds'] == .25
    assert record['started_utc'].endswith('+00:00')
    assert record['finished_utc'].endswith('+00:00')
    if method == 'get_positions':
        assert D(record['money'][0]['normalized']['value']) == D('11815.360295')
        assert record['blocked'][0]['nano']['value'] == '3'
    else:
        assert record['order_id'] == 'broker'
        assert record['executed_commission']['nano']['value'] == '632023500'
        assert D(record['executed_commission']['normalized']['value']) == D('33.6320235')
    assert 'foreign' not in json.dumps(session.records)


def test_observer_preserves_exception_without_recording_its_text():
    """No transport message, arbitrary kwargs, metadata or credentials are serialized."""
    service = inspect.unwrap(services.OrdersService)
    client = object.__new__(service)
    error = RuntimeError('sensitive transport text')
    original = create_autospec(service.post_order, spec_set=True, side_effect=error)
    session = SimpleNamespace(lifecycle=SimpleNamespace(created=['owned']), records=[],
                              scenario='research', monotonic=iter([1., 2.]).__next__)
    with patch.object(service, 'post_order', original), observe_finances(session):
        with pytest.raises(RuntimeError) as caught:
            client.post_order(account_id='owned')
    assert caught.value is error
    record, = session.records
    assert record['outcome'] == 'error' and record['elapsed_seconds'] == 1
    assert 'sensitive' not in json.dumps(record)
    original.assert_called_once_with(client, account_id='owned')


@pytest.mark.parametrize('field,value,reason', [
    ('direction', None, 'invalid order direction'),
    ('execution_report_status', None, 'invalid order status'),
    ('lots_requested', object(), 'invalid order lot counts'),
    ('lots_executed', object(), 'invalid order lot counts'),
    ('order_id', object(), 'invalid order identity'),
    ('instrument_uid', object(), 'invalid order identity'),
])
def test_malformed_order_remains_available_for_normal_validation(field, value, reason):
    """Observation must not replace adapter errors or poison the final JSON dump."""
    service = inspect.unwrap(services.OrdersService)
    client = object.__new__(service)
    response = sdk.OrderState(
        order_id='broker', instrument_uid='bond', lots_requested=1, lots_executed=1,
        direction=sdk.OrderDirection.ORDER_DIRECTION_BUY,
        execution_report_status=sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL)
    setattr(response, field, value)
    original = create_autospec(service.get_order_state, spec_set=True, return_value=response)
    session = SimpleNamespace(lifecycle=SimpleNamespace(created=['owned']), records=[],
                              scenario='research', monotonic=iter([1., 2., 3., 4.]).__next__)
    executor = TInvestOrderExecutor(SimpleNamespace(orders=client))
    with patch.object(service, 'get_order_state', original), observe_finances(session):
        assert client.get_order_state(account_id='owned', order_id='broker') is response
        with pytest.raises(OrderExecutionError, match=reason):
            executor.get_order_state('owned', 'broker')
    assert original.call_count == 2
    assert len(session.records) == 2
    assert all(record['outcome'] == 'returned' for record in session.records)
    encoded = json.dumps(session.records, allow_nan=False)
    assert 'object at' not in encoded
    assert json.loads(encoded) == session.records


def test_expected_balance_components_keep_signed_fx_principal_and_single_fees():
    """The diagnostic ledger explains BUY/SELL without any rounding model."""
    before = NativeState({}, {'rub': D(30000), 'cny': D(2000)}, {})
    trades = [TradeFact('fx', 'BUY', 1, 1000, 'rub', D(12000), D(10), 'cny'),
              TradeFact('bond', 'SELL', 67, 1, 'cny', D('67264.047'), D('33.6320235'), None)]
    components = money_components(before, trades)
    operations = {code: terms.pop('operations') for code, terms in components.items()}
    assert components == {
        'rub': {'before': D(30000), 'security_principal': D(0), 'fx_quote': D(-12000),
                'fx_base': D(0), 'commission': D(-10), 'expected': D(17990),
                'raw_expected': D(17990), 'settlement_expected': D(17990),
                'rounding_adjustment': D(0), 'settlement_status': 'supported',
                'settlement_error': None},
        'cny': {'before': D(2000), 'security_principal': D('67264.047'),
                'fx_quote': D(0), 'fx_base': D(1000), 'commission': D('-33.6320235'),
                'expected': D('70230.4149765'), 'raw_expected': D('70230.4149765'),
                'settlement_expected': D('70230.4149765'), 'rounding_adjustment': D(0),
                'settlement_status': 'supported', 'settlement_error': None}}
    assert operations == {
        'rub': [{'sequence': 1, 'uid': 'fx', 'side': 'BUY', 'cny_bond': False,
                 'before': D(30000), 'raw_delta': D(-12010), 'raw_expected': D(17990),
                 'unrounded_settlement_expected': D(17990), 'settlement_expected': D(17990),
                 'rounding_adjustment': D(0)}],
        'cny': [{'sequence': 1, 'uid': 'fx', 'side': 'BUY', 'cny_bond': False,
                 'before': D(2000), 'raw_delta': D(1000), 'raw_expected': D(3000),
                 'unrounded_settlement_expected': D(3000), 'settlement_expected': D(3000),
                 'rounding_adjustment': D(0)},
                {'sequence': 2, 'uid': 'bond', 'side': 'SELL', 'cny_bond': False,
                 'before': D(3000), 'raw_delta': D('67230.4149765'),
                 'raw_expected': D('70230.4149765'),
                 'unrounded_settlement_expected': D('70230.4149765'),
                 'settlement_expected': D('70230.4149765'), 'rounding_adjustment': D(0)}]}
