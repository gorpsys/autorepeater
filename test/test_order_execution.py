"""SDK boundary and one-pass stopping rules, using offline fakes only."""
from dataclasses import replace
from decimal import Decimal as D
import inspect
from types import SimpleNamespace
from unittest.mock import Mock, call, create_autospec, patch

from test.test_order_plan import destination, leaf, parent, rules

import pytest
from grpc import StatusCode
from t_tech.invest import OrderDirection, OrderType, OrderExecutionReportStatus
from t_tech.invest.exceptions import RequestError
from t_tech.invest import PostOrderResponse
from t_tech.invest.services import OrdersService

from autorepeater.execution_data import ExecutionData, ExecutionDataError
from autorepeater.execution import (
    ExecutionReceipt, OrderExecutionError, OrderExecutor, execute_plan,
)
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.order_plan import OrderIntent, build_order_plan


def sale_plan():
    """Two independent sale UIDs and a purchase, no presumed sale proceeds."""
    return build_order_plan(leaf(target={'Y': D(10)}, budget='100', sell=True),
                            destination({'X': D(10), 'Z': D(10)}, cash='10'),
                            {(): {'X': D(10), 'Z': D(10)}},
                            {'X': D(1), 'Y': D(1), 'Z': D(1)}, rules('X', 'Y', 'Z'))


def receipt(intent, status='FILL', requested=None, executed=None, cash=None):
    """Receipt never declares guessed net proceeds."""
    return ExecutionReceipt(intent.uid, intent.side, 'id', status,
                            intent.lots if requested is None else requested,
                            intent.lots if executed is None else executed, cash or {})


@pytest.mark.parametrize('status', ['NEW', 'PARTIALLYFILL', 'REJECTED', 'CANCELLED',
                                    'UNSPECIFIED', 'unknown'])
def test_nonfill_sale_stops_remaining_sells_and_all_buys(status):
    """nonfill sale stops remaining sells and all buys."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent, status)
    data = Mock()
    with pytest.raises(OrderExecutionError):
        execute_plan('offline', sale_plan(), data, executor)
    assert executor.submit_order.call_count == 1
    data.get_destination.assert_not_called()


@pytest.mark.parametrize('requested,executed', [(9, 10), (10, 9), (11, 11), (True, 10)])
def test_both_counts_must_match_submitted(requested, executed):
    """both counts must match submitted."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, requested=requested, executed=executed)
    with pytest.raises(OrderExecutionError):
        execute_plan('offline', sale_plan(), Mock(), executor)
    assert executor.submit_order.call_count == 1


def test_fresh_money_and_caps_replan_no_fake_proceeds():
    """fresh money and caps replan no fake proceeds."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = destination(cash='3')
    data.get_trade_rules.return_value = rules('X', 'Y', 'Z', buy=2)
    result = execute_plan('offline', sale_plan(), data, executor)
    assert [(item.uid, item.side, item.lots_requested) for item in result] == [
        ('X', 'SELL', 10), ('Z', 'SELL', 10), ('Y', 'BUY', 2)]
    data.get_destination.assert_called_once_with('offline')


def test_fresh_active_order_defers_buys():
    """fresh active order defers buys."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = replace(destination(), active_orders=('old',))
    assert len(execute_plan('offline', sale_plan(), data, executor)) == 2


def test_sdk_enum_conversion_at_boundary():
    """sdk enum conversion at boundary."""
    client = Mock(spec_set=['orders'])
    client.orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    client.orders.post_order.return_value = PostOrderResponse(
        order_id='id', execution_report_status=(
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL),
        lots_requested=10, lots_executed=10)
    intent = sale_plan().sells[0]
    with patch('autorepeater.order_execution.uuid4', return_value='request-id'):
        result = TInvestOrderExecutor(client).submit_order('offline', intent)
    assert client.orders.mock_calls == [call.post_order(
        account_id='offline', instrument_id='X', quantity=10,
        direction=OrderDirection.ORDER_DIRECTION_SELL,
        order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='request-id')]
    assert result.status == 'FILL' and not result.cash


def test_programming_error_is_not_masked():
    """programming error is not masked."""
    client = Mock()
    client.orders.post_order.side_effect = TypeError('bug')
    with pytest.raises(TypeError, match='bug'):
        TInvestOrderExecutor(client).submit_order('offline', sale_plan().sells[0])


def test_no_fake_receipt_proceeds_or_commission_price_credit():
    """no fake receipt proceeds or commission price credit."""
    client = Mock()
    client.orders.post_order.return_value = SimpleNamespace(
        order_id='id', execution_report_status=(
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL),
        lots_requested=10, lots_executed=10, executed_order_price=D(500),
        total_order_amount=D(4990), executed_commission=D(10))
    assert not TInvestOrderExecutor(client).submit_order('offline', sale_plan().sells[0]).cash


def test_unknown_purchase_result_stops_next_purchase():
    """unknown purchase result stops next purchase."""
    plan = build_order_plan(leaf(target={'A': D(2), 'B': D(2)}), destination(),
                            {(): {}}, {'A': D(1), 'B': D(1)}, rules('A', 'B'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent, 'NEW')
    with pytest.raises(OrderExecutionError):
        execute_plan('offline', plan, Mock(), executor)
    assert executor.submit_order.call_count == 1


def test_next_purchase_uses_fresh_reduced_cash_and_caps():
    """next purchase uses fresh reduced cash and caps."""
    plan = build_order_plan(leaf(target={'A': D(2), 'B': D(2)}), destination(),
                            {(): {}}, {'A': D(1), 'B': D(1)}, rules('A', 'B'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = destination({'A': D(2)}, cash='1')
    data.get_trade_rules.return_value = rules('A', 'B', buy=1)
    result = execute_plan('offline', plan, data, executor)
    assert [(item.uid, item.lots_requested) for item in result] == [('A', 2), ('B', 1)]


def test_scoped_sale_cannot_unlock_neighbor_money_without_permission():
    """scoped sale cannot unlock neighbor money without permission."""
    tree = parent((leaf((0,), {'A': D(10)}, budget='10', sell=True),
                   leaf((1,), {'B': D(10)}, budget='10')))
    plan = build_order_plan(tree, destination({'X': D(10)}, cash='0'),
                            {(0,): {'X': D(10)}, (1,): {}},
                            {'X': D(1), 'A': D(1), 'B': D(1)}, rules('X', 'A', 'B'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash={'rub': D(10)} if intent.side == 'SELL' else {})
    data = Mock()
    data.get_destination.return_value = destination(cash='20')
    data.get_trade_rules.return_value = rules('X', 'A', 'B')
    result = execute_plan('offline', plan, data, executor)
    assert [(item.uid, item.side) for item in result] == [('X', 'SELL'), ('A', 'BUY')]


@pytest.mark.parametrize('exception', [OrderExecutionError('unknown')])
def test_neutral_transport_stops_with_original_cause(exception):
    """neutral transport stops with original cause."""
    executor = Mock()
    executor.submit_order.side_effect = exception
    with pytest.raises(OrderExecutionError) as raised:
        execute_plan('offline', sale_plan(), Mock(), executor)
    assert raised.value is exception
    assert executor.submit_order.call_count == 1


def test_sdk_transport_preserves_cause_without_retry():
    """sdk transport preserves cause without retry."""
    error = RequestError(StatusCode.UNAVAILABLE, 'offline', None)
    client = Mock()
    client.orders.post_order.side_effect = error
    with pytest.raises(OrderExecutionError) as raised:
        TInvestOrderExecutor(client).submit_order('offline', sale_plan().sells[0])
    assert raised.value.__cause__ is error
    assert client.orders.post_order.call_count == 1


@pytest.mark.parametrize('response', [None, SimpleNamespace(order_id='id')])
def test_malformed_sdk_response_cannot_confirm_sale(response):
    """malformed sdk response cannot confirm sale."""
    client = Mock()
    client.orders.post_order.return_value = response
    with pytest.raises(OrderExecutionError):
        execute_plan('offline', sale_plan(), Mock(), TInvestOrderExecutor(client))
    assert client.orders.post_order.call_count == 1


def test_fresh_read_transport_stops_after_already_filled_sales():
    """fresh read transport stops after already filled sales."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    error = ExecutionDataError('offline refresh')
    data.get_destination.side_effect = error
    with pytest.raises(OrderExecutionError) as raised:
        execute_plan('offline', sale_plan(), data, executor)
    assert raised.value.__cause__ is error
    assert executor.submit_order.call_count == 2


@pytest.mark.parametrize('fresh', [destination({'A': D(2)}, cash='0'),
                                  replace(destination(), limits_ready=False),
                                  replace(destination(), active_orders=('pending',))])
def test_next_buy_defers_if_money_or_readiness_changes(fresh):
    """next buy defers if money or readiness changes."""
    plan = build_order_plan(leaf(target={'A': D(2), 'B': D(2)}), destination(),
                            {(): {}}, {'A': D(1), 'B': D(1)}, rules('A', 'B'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = fresh
    data.get_trade_rules.return_value = rules('A', 'B')
    assert len(execute_plan('offline', plan, data, executor)) == 1


def test_initial_active_old_order_stops_next_independent_pass():
    """initial active old order stops next independent pass."""
    plan = replace(sale_plan(), snapshot=replace(destination(), active_orders=('old',)))
    executor = Mock()
    assert not execute_plan('offline', plan, Mock(), executor)
    executor.submit_order.assert_not_called()


def test_sale_position_attribution_remains_fixed_after_refresh():
    """sale position attribution remains fixed after refresh."""
    tree = parent((leaf((0,), {'X': D(0), 'Y': D(20)}, budget='20', sell=True),
                   leaf((1,), {'X': D(10)}, budget='10')))
    plan = build_order_plan(tree, destination({'X': D(20)}, cash='0'),
                            {(0,): {'X': D('17.5')}, (1,): {'X': D('2.5')}},
                            {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert plan.sells[0].pieces == {(0,): D(10)}
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash={'rub': D(20)} if intent.side == 'SELL' else {})
    data = Mock()
    data.get_destination.return_value = destination({'X': D(10)}, cash='100')
    data.get_trade_rules.return_value = rules('X', 'Y', lot=10)
    assert [(item.uid, item.side) for item in execute_plan('offline', plan, data, executor)] == [
        ('X', 'SELL'), ('Y', 'BUY')]
    assert executor.submit_order.call_args.args[1].pieces == {(0,): D(10)}
    assert executor.submit_order.call_args.args[1].lots == 1


def test_nested_redistribution_permission_not_erased_by_parent():
    """nested redistribution permission not erased by parent."""
    inner = replace(parent((leaf((0, 0), {'A': D(10)}, budget='10', sell=True),
                            leaf((0, 1), {'B': D(10)}, budget='10'))), path=(0,))
    tree = parent((inner, leaf((1,), {'C': D(10)}, budget='10')), redistribute=True)
    plan = build_order_plan(tree, destination({'X': D(10)}, cash='0'),
                            {(0, 0): {'X': D(10)}, (0, 1): {}, (1,): {}},
                            dict.fromkeys(('X', 'A', 'B', 'C'), D(1)), rules('X', 'A', 'B', 'C'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash={'rub': D(10)} if intent.side == 'SELL' else {})
    data = Mock()
    data.get_destination.return_value = destination(cash='30')
    data.get_trade_rules.return_value = rules('X', 'A', 'B', 'C')
    assert [(item.uid, item.side) for item in execute_plan('offline', plan, data, executor)] == [
        ('X', 'SELL'), ('A', 'BUY')]


def test_unsettled_positions_defer_and_do_not_reattribute_protected_owner():
    """unsettled positions defer and do not reattribute protected owner."""
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = destination({'X': D(10)}, cash='100')
    data.get_trade_rules.return_value = rules('X', 'Y', 'Z')
    result = execute_plan('offline', sale_plan(), data, executor)
    assert [item.side for item in result] == ['SELL', 'SELL']


def test_root_transfer_permission_allows_fresh_actual_money_to_sibling():
    """root transfer permission allows fresh actual money to sibling."""
    tree = parent((leaf((0,), {'A': D(10)}, budget='10', sell=True),
                   leaf((1,), {'B': D(10)}, budget='10')), redistribute=True)
    plan = build_order_plan(tree, destination({'X': D(10)}, cash='0'),
                            {(0,): {'X': D(10)}, (1,): {}},
                            {'X': D(1), 'A': D(1), 'B': D(1)}, rules('X', 'A', 'B'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.side_effect = [destination(cash='20'),
                                        destination({'A': D(10)}, cash='10')]
    data.get_trade_rules.return_value = rules('X', 'A', 'B')
    assert [item.uid for item in execute_plan('offline', plan, data, executor)] == ['X', 'A', 'B']


def test_unassigned_fresh_money_uses_own_node_without_global_transfer_permission():
    """unassigned fresh money uses own node without global transfer permission."""
    tree = parent((leaf((0,), {'Y': D(10)}, budget='10'),), unassigned={'X': D(10)})
    plan = build_order_plan(tree, destination({'X': D(10)}, cash='0'), {(0,): {}},
                            {'X': D(1), 'Y': D(1)}, rules('X', 'Y'))
    executor = Mock()
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = Mock()
    data.get_destination.return_value = destination(cash='10')
    data.get_trade_rules.return_value = rules('X', 'Y')
    assert [item.uid for item in execute_plan('offline', plan, data, executor)] == ['X', 'Y']


@pytest.mark.parametrize('documented', [True, False])
def test_unequal_isolated_sales_preserve_cash_origins(documented, caplog):
    """Fresh account money cannot assign one seller's proceeds to another seller."""
    tree = parent((leaf((0,), {'A': D(100)}, budget='100', sell=True),
                   leaf((1,), {'B': D(100)}, budget='100', sell=True)))
    plan = build_order_plan(tree, destination({'X': D(150), 'Y': D(50)}, cash='0'),
                            {(0,): {'X': D(150)}, (1,): {'Y': D(50)}},
                            dict.fromkeys(('A', 'B', 'X', 'Y'), D(1)), rules('A', 'B', 'X', 'Y'))
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash={'rub': D(intent.lots)} if documented and intent.side == 'SELL' else {})
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.side_effect = [destination(cash='200'),
                                        destination({'A': D(100)}, cash='100'),
                                        destination({'A': D(100), 'B': D(50)}, cash='50')]
    data.get_trade_rules.return_value = rules('A', 'B', 'X', 'Y')
    with caplog.at_level('INFO', logger='tinkoffBot'):
        result = execute_plan('offline', plan, data, executor)
    expected = [('X', 'SELL', 150), ('Y', 'SELL', 50)]
    if documented:
        expected += [('A', 'BUY', 100), ('B', 'BUY', 50)]
    else:
        assert any('Defer scoped purchases' in line for line in caplog.messages)
    assert [(item.uid, item.side, item.lots_requested) for item in result] == expected


def test_refresh_replaces_entire_buy_queue_with_new_candidates():
    """A reduced own lot cap frees money for a UID absent from the first queue."""
    limits = {uid: replace(rule, buy_money_amount=D(20))
              for uid, rule in rules('A', 'B', 'C').items()}
    plan = build_order_plan(leaf(target={'A': D(1), 'B': D(2), 'C': D(1)}),
                            destination(cash='20'), {(): {}},
                            {'A': D(10), 'B': D(5), 'C': D(1)}, limits)
    assert [item.uid for item in plan.buys] == ['A', 'B']
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.side_effect = [destination({'A': D(1)}, cash='10'),
                                        destination({'A': D(1), 'B': D(1)}, cash='5'),
                                        destination({'A': D(1), 'B': D(1), 'C': D(1)}, cash='4')]
    data.get_trade_rules.return_value = {**limits, 'B': replace(limits['B'], buy_max_lots=1)}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    assert [(item.uid, item.lots_requested) for item in execute_plan(
        'offline', plan, data, executor)] == [('A', 1), ('B', 1), ('C', 1)]


def test_unknown_common_sale_cannot_launder_isolated_sale_cash(caplog):
    """One common seller cannot identify account money from a protected subtree."""
    inner = replace(parent((leaf((0, 0), {'A': D(100)}, budget='100', sell=True),
                            leaf((0, 1), {'B': D(100)}, budget='100'))), path=(0,))
    tree = parent((inner, leaf((1,), {'C': D(100)}, budget='100')),
                  redistribute=True, unassigned={'Z': D(50)})
    plan = build_order_plan(tree, destination({'X': D(150), 'Z': D(50)}, cash='0'),
                            {(0, 0): {'X': D(150)}, (0, 1): {}, (1,): {}},
                            dict.fromkeys(('A', 'B', 'C', 'X', 'Z'), D(1)),
                            rules('A', 'B', 'C', 'X', 'Z'))
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: receipt(intent)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.side_effect = [destination(cash='200'),
                                        destination({'A': D(100)}, cash='100')]
    data.get_trade_rules.return_value = rules('A', 'B', 'C', 'X', 'Z')
    with caplog.at_level('INFO', logger='tinkoffBot'):
        result = execute_plan('offline', plan, data, executor)
    assert [(item.uid, item.side) for item in result] == [('X', 'SELL'), ('Z', 'SELL')]
    assert any('Defer scoped purchases' in line for line in caplog.messages)


@pytest.mark.parametrize('budget,held_a,held_b,price_b,cash', [
    ('125', '62', '100', '10', '38'),
    ('100', '1', '99', '0.1', '99.1'),
])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('credit', ['unknown', 'isolated', 'common_zero'])
def test_flat_mixed_sale_scopes_require_proven_credits(
        budget, held_a, held_b, price_b, cash, reverse, credit, caplog):
    """Unassigned root proceeds cannot unlock an isolated child's unknown cash."""
    # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    marks = {'A': D(1), 'B': D(price_b), 'H': D(1), 'X': D(1),
             'Z': D('0.1') if price_b == '0.1' else D(1)}
    tree = parent((
        leaf((0,), {'H': D(held_a), 'A': D(budget) - D(held_a)},
             budget=budget, sell=True),
        leaf((1,), {'H': D(held_b), 'B': (D(budget) - D(held_b)) / D(price_b)},
             budget=budget),
    ), unassigned={'Z': D(1) if price_b == '0.1' else D(50)})
    tree = replace(tree, target=replace(tree.target, prices={
        uid: marks[uid] for uid in tree.target.quantities}))
    owned = {(0,): {'H': D(held_a), 'X': D(99) if price_b == '0.1' else D(38)},
             (1,): {'H': D(held_b)}}
    quantities = {'H': D(held_a) + D(held_b), 'X': owned[(0,)]['X'],
                  'Z': tree.unassigned['Z']}
    limits = rules(*marks)
    plan = build_order_plan(tree, destination(quantities, cash='0'), owned, marks, limits)
    if reverse:
        plan = replace(plan, sells=tuple(reversed(plan.sells)))
    assert [(intent.uid, intent.pieces) for intent in plan.sells] == (
        [('Z', {(): quantities['Z']}), ('X', {(0,): quantities['X']})] if reverse else
        [('X', {(0,): quantities['X']}), ('Z', {(): quantities['Z']})])
    assert plan.money == {(0,): D(0), (1,): D(0)}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash=({'rub': quantities['X']} if credit == 'isolated' and intent.uid == 'X'
                      else {'rub': D(0)} if credit == 'common_zero' and intent.uid == 'Z'
                      else {}))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)

    def fresh_destination(_account):
        bought = {item.args[1].uid: D(item.args[1].lots) for item in executor.mock_calls
                  if item.args[1].side == 'BUY'}
        spent = sum((amount * marks[uid] for uid, amount in bought.items()), D(0))
        return destination({'H': quantities['H'], **bought}, cash=str(D(cash) - spent))

    data.get_destination.side_effect = fresh_destination
    data.get_trade_rules.return_value = limits
    with caplog.at_level('INFO', logger='tinkoffBot'):
        result = execute_plan('offline', plan, data, executor)
    expected = [(intent.uid, 'SELL', intent.lots) for intent in plan.sells]
    if credit == 'isolated':
        expected.append(('A', 'BUY', int(quantities['X'])))
    assert [(item.uid, item.side, item.lots_requested) for item in result] == expected
    assert executor.mock_calls == [
        call.submit_order('offline', intent) for intent in plan.sells] + (
        [call.submit_order('offline', OrderIntent(
            'A', 'BUY', int(quantities['X']), 1, {(0,): quantities['X']}))]
        if credit == 'isolated' else [])
    assert data.mock_calls == [call.get_destination('offline'),
                               call.get_trade_rules('offline', sorted(marks))]
    if credit == 'unknown':
        assert any('Defer scoped purchases' in line for line in caplog.messages)


@pytest.mark.parametrize('documented', [False, True])
def test_all_common_sale_scopes_preserve_unknown_fallback_and_proven_credits(documented):
    """Common unknown receipts allow fallback; fully proven receipts retain their caps."""
    tree = parent((leaf((0,), {'A': D(100)}, sell=True),
                   leaf((1,), {'B': D(100)})), redistribute=True, unassigned={'Z': D(50)})
    limits = rules('A', 'B', 'X', 'Z')
    plan = build_order_plan(tree, destination({'X': D(150), 'Z': D(50)}, cash='0'),
                            {(0,): {'X': D(150)}, (1,): {}},
                            dict.fromkeys(limits, D(1)), limits)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: receipt(
        intent, cash={'rub': D(10)} if documented and intent.side == 'SELL' else {})
    amount = D(10) if documented else D(100)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.side_effect = [destination(cash='200'),
                                        destination({'A': amount}, cash=str(D(200) - amount))]
    data.get_trade_rules.return_value = limits
    assert [(item.uid, item.side, item.lots_requested) for item in execute_plan(
        'offline', plan, data, executor)] == [
            ('X', 'SELL', 150), ('Z', 'SELL', 50),
            ('A', 'BUY', int(amount)), ('B', 'BUY', int(amount))]
    assert executor.mock_calls == [
        *(call.submit_order('offline', intent) for intent in plan.sells),
        call.submit_order('offline', OrderIntent('A', 'BUY', int(amount), 1, {(0,): amount})),
        call.submit_order('offline', OrderIntent('B', 'BUY', int(amount), 1, {(1,): amount})),
    ]
    assert data.mock_calls == [
        call.get_destination('offline'), call.get_trade_rules('offline', sorted(limits)),
        call.get_destination('offline'), call.get_trade_rules('offline', sorted(limits)),
    ]


@pytest.mark.parametrize('cash', [{'rub': D(-1)}, {'rub': D('NaN')}, {'rub': 1}, []])
def test_invalid_confirmed_cash_stops_before_refresh(cash, caplog):
    """Malformed monetary facts cannot grant funding or permit another sale."""
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: replace(receipt(intent), cash=cash)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    with caplog.at_level('ERROR', logger='tinkoffBot'), pytest.raises(
            OrderExecutionError, match='invalid receipt cash') as raised:
        execute_plan('offline', sale_plan(), data, executor)
    assert isinstance(raised.value.__cause__, ValueError)
    assert executor.submit_order.call_count == 1
    assert not data.mock_calls
    assert any('invalid confirmed cash' in line for line in caplog.messages)
