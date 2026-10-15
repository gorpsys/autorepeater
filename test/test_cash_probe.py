"""Offline tests for the explicit sandbox monetary-facts experiment."""
from decimal import Decimal
import inspect
from types import SimpleNamespace
from unittest.mock import create_autospec, patch
from test import sandbox_cash_live as probe

import pytest
from t_tech.invest import (
    MoneyValue, Operation, OperationTrade, OperationType, PostOrderResponse,
    OrderState, OrderDirection, OrderType, GetOrderPriceResponse, Quotation, Order,
)
from t_tech.invest.services import OperationsService, OrdersService, MarketDataService

from autorepeater.strategy_data import InstrumentInfo, InstrumentType
from scripts.sandbox_lifecycle import SandboxFailure, SandboxLifecycle, safe_call
from scripts.sandbox_support import SandboxSession


def test_money_keeps_exact_units_and_nano():
    """No float and no silent unit/currency conversion."""
    assert probe.money(MoneyValue(currency='cny', units=-12, nano=-123)) == {
        'currency': 'cny', 'units': -12, 'nano': -123, 'amount': '-12.000000123'}
    assert probe.money(object()) is None


@pytest.mark.parametrize('value', [
    SimpleNamespace(currency='secret', units=1, nano=0),
    SimpleNamespace(currency='rub', units=True, nano=0)])
def test_bad_money_is_not_echoed(value):
    """Diagnostics never interpolate rejected arbitrary provider strings."""
    with pytest.raises(SandboxFailure) as caught:
        probe.money(value)
    assert 'secret' not in str(caught.value)


def test_operation_uses_allowlist_and_retains_trade_identity():
    """Operation description, SDK repr and account fields are not published."""
    operation = Operation(
        id='operation', parent_operation_id='parent', instrument_uid='bond',
        operation_type=OperationType.OPERATION_TYPE_SELL,
        payment=MoneyValue(currency='cny', units=1000, nano=0),
        trades=[OperationTrade(trade_id='trade', quantity=1)], type='synthetic-secret')
    result = probe.operation_facts(operation)
    assert result['id'] == 'operation' and result['parent_operation_id'] == 'parent'
    assert result['trades'][0]['trade_id'] == 'trade'
    assert 'synthetic-secret' not in str(result)


def test_links_require_broker_or_trade_ids_not_matching_instrument():
    """Same UID and timestamp are not a cash-attribution contract."""
    order = {'order_id': 'broker', 'stages': [{'trade_id': 'trade'}]}
    operations = [{'id': 'unrelated', 'parent_operation_id': '', 'trades': []},
                  {'id': 'broker', 'parent_operation_id': '', 'trades': []},
                  {'id': 'commission', 'parent_operation_id': 'broker', 'trades': []},
                  {'id': 'other', 'parent_operation_id': '', 'trades': [{'trade_id': 'trade'}]}]
    result = probe.operation_links(order, operations)
    assert {item['operation_id'] for item in result} == {'broker', 'commission', 'other'}


def test_cursor_requires_all_pages_and_does_not_repeat_cursor():
    """Pagination cannot silently truncate evidence."""
    operations = create_autospec(inspect.unwrap(OperationsService), instance=True, spec_set=True)
    operations.get_operations_by_cursor.side_effect = [
        SimpleNamespace(items=[], has_next=True, next_cursor='next'),
        SimpleNamespace(items=[], has_next=True, next_cursor='next')]
    with pytest.raises(SandboxFailure, match='cursor pagination'):
        probe.cursor_operations(SimpleNamespace(operations=operations), 'owned', None)
    assert operations.get_operations_by_cursor.call_count == 2


def test_failure_evidence_is_written_and_account_cleanup_runs(tmp_path, monkeypatch):
    """A failed observation still preserves evidence and owned-account cleanup."""
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    lifecycle = create_autospec(SandboxLifecycle, instance=True, spec_set=True)
    with patch.object(probe, 'run_probe', side_effect=SandboxFailure('synthetic-secret')):
        with pytest.raises(SandboxFailure):
            probe.test_sandbox_cash_facts(SimpleNamespace(lifecycle=lifecycle))
    artifact = (tmp_path / 'cash-facts.json').read_text(encoding='utf-8')
    assert 'synthetic-secret' not in artifact
    assert '"cleanup_confirmed": true' in artifact
    lifecycle.cleanup.assert_called_once_with()


def test_entrypoint_requires_sandbox_token(monkeypatch):
    """Never fall back to a production credential."""
    monkeypatch.delenv('SANDBOX_TOKEN', raising=False)
    assert probe.main() == 2


def test_number_is_decimal():
    """Commission differences at nano precision remain visible."""
    assert probe.number(MoneyValue(currency='rub', units=1, nano=1)) == Decimal('1.000000001')


def test_observe_can_forward_a_method_argument_to_safe_call():
    """Exercise the real read wrapper, not a mocked observation path."""
    def read(*, account_id):
        return account_id

    method = create_autospec(read, spec_set=True)
    method.return_value = 'snapshot'
    assert probe.observe(safe_call, {}, 'Read', method=method, account_id='owned') == 'snapshot'
    method.assert_called_once_with(account_id='owned')


@pytest.mark.parametrize('uncertain', [False, True])
def test_submit_never_repeats_mutation_and_reads_broker_id(uncertain):
    """Exercise submit without mocking the evidence helpers or safe_call."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    market = create_autospec(inspect.unwrap(MarketDataService), instance=True, spec_set=True)
    market.get_order_book.return_value = SimpleNamespace(
        asks=[Order(price=Quotation(units=12, nano=0), quantity=10)])
    orders.get_order_price.return_value = GetOrderPriceResponse()
    direction = OrderDirection.ORDER_DIRECTION_BUY
    facts = {'order_id': 'broker', 'instrument_uid': 'currency', 'direction': direction,
             'lots_requested': 10, 'lots_executed': 10, 'execution_report_status': probe.FILL}
    orders.post_order.return_value = PostOrderResponse(**facts)
    orders.get_order_state.return_value = OrderState(**facts)
    if uncertain:
        orders.post_order.side_effect = SandboxFailure('uncertain submission')
    session = SandboxSession(SimpleNamespace(orders=orders, market_data=market), SimpleNamespace())
    session.order_id_factory = lambda: 'request'
    instrument = InstrumentInfo('currency', 'CNYRUB_TOM', '', InstrumentType.CURRENCY,
                                'CETS', 1000, 'rub', True)
    report = {'orders': []}
    if uncertain:
        with pytest.raises(SandboxFailure):
            probe.submit(session, 'owned', instrument, 'BUY', report, lots=10)
        orders.get_order_state.assert_not_called()
    else:
        probe.submit(session, 'owned', instrument, 'BUY', report, lots=10)
        orders.get_order_state.assert_called_once_with(account_id='owned', order_id='broker')
        assert report['orders'][0]['state']['order_id'] == 'broker'
    orders.post_order.assert_called_once_with(
        account_id='owned', instrument_id='currency', direction=direction,
        quantity=10, order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='request')


def test_bond_estimate_uses_currency_price_not_percentage():
    """GetOrderPrice has no price_type field; distinguish the two input units."""
    price = probe.estimate_price(Quotation(units=100, nano=499900000), {'amount': '1000'})
    assert probe.number(price) == Decimal('1004.999')
    original = Quotation(units=12, nano=0)
    assert probe.estimate_price(original, None) is original


def test_reverse_lookup_reads_operation_id_without_submitting_orders():
    """The documented reverse route is checked separately from broker-ID matching."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    orders.get_order_state.side_effect = SandboxFailure('not found')
    report = {'orders': [{'state': {'order_id': 'broker'}}],
              'observations': [{'legacy': [
                  {'id': 'operation', 'type': 'OPERATION_TYPE_SELL'},
                  {'id': 'fee', 'type': 'OPERATION_TYPE_BROKER_FEE'}]}]}
    probe.reverse_operation_links(SimpleNamespace(client=SimpleNamespace(orders=orders)),
                                  'owned', report)
    orders.get_order_state.assert_called_once_with(account_id='owned', order_id='operation')
    orders.post_order.assert_not_called()
    assert report['reverse_operation_lookups'] == [{
        'operation_id': 'operation', 'order': None, 'matches_known_broker_order': False}]
