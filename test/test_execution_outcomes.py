"""Pass outcome contracts for RUB and native execution on controlled snapshots."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from test.test_fx_execution import receipt as native_receipt, setup_plan
from test.test_fx_funding import NOW
from test.test_order_execution import receipt as rub_receipt
from test.test_order_plan import destination, leaf, opposing_virtual_changes_plan, rules
from unittest.mock import call, create_autospec

import pytest

from autorepeater import execution_market as market_module
from autorepeater.execution import ExecutionState, OrderExecutor, execute_plan
from autorepeater.execution_data import ExecutionData
from autorepeater.execution_market import _sale_fee
from autorepeater.fx_data import ORDER_BOOK_MAX_AGE_SECONDS
from autorepeater.order_plan import OrderPlan, build_order_plan


def test_opposing_virtual_changes_produce_noop():
    """Per-leaf gaps net to zero without requiring any broker calls."""
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    outcome = execute_plan('dst', opposing_virtual_changes_plan(), data, executor)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        ExecutionState.NOOP, 'finished', 'no_execution')
    assert not outcome.receipts
    assert not data.mock_calls and not executor.mock_calls


@pytest.mark.parametrize('quantity,cap,cash,state,reason', [
    (D(1), 1, '100', 'NOOP', 'no_execution'),
    (D('1.5'), 1, '100', 'PARTIAL', 'remaining_trade_delta'),
    (D(2), 0, '100', 'PARTIAL', 'remaining_trade_delta'),
    (D(2), 1, '0', 'PARTIAL', 'remaining_trade_delta'),
])
def test_rub_outcome_without_orders(quantity, cap, cash, state, reason):
    """Empty order queues still report an eligible unmet trading delta."""
    holdings = {} if not cap else {'A': D(1)}
    plan = build_order_plan(leaf(target={'A': quantity}), destination(holdings, cash=cash),
                            {(): holdings}, {'A': D(1)}, rules('A', buy=cap))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    result = execute_plan('dst', plan, data, executor)
    assert result.state is ExecutionState(state)
    assert (result.stage, result.reason) == ('finished', reason)
    assert not result.receipts
    assert not plan.sells and not plan.buys
    assert not data.mock_calls and not executor.mock_calls


def test_sell_delta_survives_zero_available_quantity():
    """A temporary quantity cap does not erase an eligible initial sale."""
    holdings = {'A': D(1), 'B': D(1)}
    snapshot = replace(destination(holdings), available_quantities={'A': D(0), 'B': D(1)})
    plan = build_order_plan(leaf(target={'B': D(1)}, sell=True), snapshot,
                            {(): holdings}, dict.fromkeys(holdings, D(1)), rules('A', 'B'))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    outcome = execute_plan('dst', plan, data, executor)
    assert plan.sell_deltas == {'A': D(1)}
    assert not plan.sells
    assert (outcome.state, outcome.stage, outcome.reason) == (
        'PARTIAL', 'finished', 'remaining_trade_delta')
    assert not outcome.receipts
    assert not data.mock_calls and not executor.mock_calls


def test_direct_order_plan_keeps_remaining_sell_delta():
    """Old constructor callers still distinguish a capped sale from a completed queue."""
    holdings = {'A': D(2), 'B': D(1)}
    snapshot = destination(holdings)
    plan = build_order_plan(leaf(target={'B': D(1)}, sell=True), snapshot,
                            {(): holdings}, dict.fromkeys(holdings, D(1)),
                            rules('A', 'B', sell=1))
    legacy = OrderPlan(plan.strategy, plan.snapshot, plan.ownership, plan.marks, plan.rules,
                       plan.money, plan.sells, plan.buys)
    intent, = plan.sells
    assert intent.lots == 1
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = destination({'A': D(1), 'B': D(1)})
    data.get_trade_rules.return_value = plan.rules
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.return_value = rub_receipt(intent)

    outcome = execute_plan('dst', legacy, data, executor)

    assert (outcome.state, outcome.stage, outcome.reason) == (
        'PARTIAL', 'finished', 'remaining_trade_delta')
    assert outcome.receipts == (rub_receipt(intent),)
    assert executor.mock_calls == [call.submit_order('dst', intent)]
    assert data.mock_calls == [call.get_destination('dst'), call.get_trade_rules('dst', ['A', 'B'])]


def test_rub_position_mismatch_stops_before_buys():
    """A supplied snapshot that disagrees with fixed ownership is not exhausted."""
    plan = build_order_plan(leaf(), destination(), {(): {}}, {'X': D(1)}, rules('X'))
    plan = replace(plan, snapshot=destination({'X': D(1)}))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    outcome = execute_plan('dst', plan, data, executor)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        'DEFERRED', 'before_buys', 'positions_mismatch')
    assert not outcome.receipts
    assert not data.mock_calls and not executor.mock_calls


@pytest.mark.parametrize('a_rule,b_target,state', [
    (True, 1, 'PARTIAL'), (False, 1, 'FINISHED'), (False, 2, 'PARTIAL'),
])
def test_rub_outcome_uses_refreshed_rules_and_each_fill_once(a_rule, b_target, state):
    """Keep unmasked observed rules, replacing the whole map at each refresh."""
    target = {'A': D('.5'), 'B': D(b_target), 'C': D(1)}
    plan = build_order_plan(leaf(target=target), destination(), {(): {}},
                            dict.fromkeys(target, D(1)), rules(*target, buy=1))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = destination({'B': D(1)})
    data.get_trade_rules.return_value = rules(*('A', 'B', 'C') if a_rule else ('B', 'C'))
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda _account, intent: rub_receipt(intent)
    outcome = execute_plan('dst', plan, data, executor)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        state, 'finished', 'remaining_trade_delta' if state == 'PARTIAL' else 'queue_completed')
    assert [(item.uid, item.lots_executed) for item in outcome.receipts] == [('B', 1), ('C', 1)]
    assert data.mock_calls == [call.get_destination('dst'),
                               call.get_trade_rules('dst', ['A', 'B', 'C'])]
    assert [item[0] for item in executor.mock_calls] == ['submit_order', 'submit_order']


@pytest.mark.parametrize('retain_rules', [False, True])
def test_rub_sell_uses_accepted_lot_and_replaces_rules_even_when_empty(retain_rules):
    """The final sale delta uses the submitted lot, independent of a later lot change."""
    holdings = {'X': D(10)}
    plan = build_order_plan(leaf(target={'A': D('.5')}, sell=True), destination(holdings),
                            {(): holdings}, {'X': D(1), 'A': D(1)},
                            {**rules('A'), **rules('X', lot=10)})
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = destination()
    data.get_trade_rules.return_value = rules('X') if retain_rules else {}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    intent, = plan.sells
    executor.submit_order.return_value = rub_receipt(intent)
    result = execute_plan('dst', plan, data, executor)
    assert (result.state, result.stage, result.reason) == (
        'FINISHED', 'finished', 'queue_completed')
    assert result.receipts == (rub_receipt(intent),)
    assert data.mock_calls == [call.get_destination('dst'), call.get_trade_rules('dst', ['A', 'X'])]
    assert executor.mock_calls == [call.submit_order('dst', intent)]


@pytest.mark.parametrize('blocker,stage,reason', [
    ('missing_amount', 'after_exchange', 'cash_attribution_unavailable'),
    ('market_closed', 'before_buys', 'fx_trading_unavailable'),
    ('cash_unavailable', 'before_buys', 'cash_unavailable'),
])
def test_native_outcome_preserves_first_fx_stop(blocker, stage, reason):
    """The first guard after an accepted FX fill controls the result."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(0 if blocker == 'cash_unavailable' else 172),
                                       'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda _account, order: native_receipt(
        order, None if blocker == 'missing_amount' else '828')
    if blocker == 'market_closed':
        fx.get_trade_rules.side_effect = lambda _account, pair: replace(
            rules(pair.uid)[pair.uid], bestprice_order_available=not executor.submit_order.called)
    outcome = broker.execute('dst', plan, data, executor, value)
    assert (outcome.state, outcome.stage, outcome.reason) == ('PARTIAL', stage, reason)
    assert [item.uid for item in outcome.receipts] == ['cny-rub']
    executor.submit_order.assert_called_once()


def test_native_missing_positive_target_rule_remains_a_fatal_validation_error():
    """A rule missing on refresh must not make an invalid FX plan look complete."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = {}
    executor.submit_order.side_effect = lambda _account, order: native_receipt(order, '828')
    with pytest.raises(ValueError, match='missing settlement currency'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()


def test_native_cash_mismatch_stops_before_first_buy():
    """The same cash ownership guard can stop a pass that has yet to submit."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    plan = replace(plan, snapshot=replace(plan.snapshot, available_cash={}))
    outcome = broker.execute('dst', plan, data, executor, value)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        'DEFERRED', 'before_buys', 'cash_unavailable')
    assert not outcome.receipts
    assert not data.mock_calls and not executor.mock_calls


def test_native_position_mismatch_stops_before_first_buy():
    """A ready native snapshot must agree with the fixed ownership map."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    assert plan.funding.demands and not plan.sells
    positions = {'C': D(1)}
    plan = replace(plan, snapshot=replace(
        plan.snapshot, quantities=positions, available_quantities=positions))

    outcome = broker.execute('dst', plan, data, executor, value)

    assert (outcome.state, outcome.stage, outcome.reason) == (
        'DEFERRED', 'before_buys', 'positions_mismatch')
    assert outcome.receipts == ()
    assert not data.mock_calls and not executor.mock_calls


def test_native_fx_fill_and_buy_leave_an_independent_fractional_sell_delta():
    """Funding the dependent BUY completely cannot erase an unrelated SELL remainder."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'C': D(1)}, budget='1300', floor='100', sell=True),
        own={**rules('R'), **rules('C', currency='cny', buy=0)}, holdings={'R': D('.5')})
    assert not plan.sells and plan.sell_deltas == {'R': D('.5')}
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(219), 'cny': D(65)})
    data.get_trade_rules.return_value = {**rules('R'), **rules('C', currency='cny', buy=1)}
    executor.submit_order.side_effect = lambda _account, order: native_receipt(
        order, '780' if order.uid == 'cny-rub' else '60', '1',
        'rub' if order.uid == 'cny-rub' else 'cny')
    outcome = broker.execute('dst', plan, data, executor, value)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        'PARTIAL', 'finished', 'remaining_trade_delta')
    assert [item.uid for item in outcome.receipts] == ['cny-rub', 'C']
    executor.get_order_state.assert_not_called()


def test_native_half_lot_reports_remaining_delta_without_trade_reads():
    """A fractional gap is visible even when neither planner can produce an intent."""
    broker, value, plan, data, executor, fx, books = setup_plan(
        leaf(target={'C': D('.5')}, budget='1000', floor='100'),
        own=rules('C', currency='cny'))
    assert not plan.sells and not plan.funding.demands
    fx.reset_mock()
    books.reset_mock()
    outcome = broker.execute('dst', plan, data, executor, value)
    assert (outcome.state, outcome.stage, outcome.reason) == (
        'PARTIAL', 'finished', 'remaining_trade_delta')
    assert not outcome.receipts and not data.mock_calls and not executor.mock_calls
    assert fx.mock_calls == [call.get_quote(value.fx.quotes['cny'].pair)]
    assert books.mock_calls == [call.get_security_quote('C')]


@pytest.mark.parametrize('stage', ['sales', 'before_buys'])
def test_currency_closes_at_repeated_preflight_before_first_fill(stage):
    """A refreshed quote and caps can stop the pass after initial preflight."""
    selling = stage == 'sales'
    broker, value, plan, data, executor, fx, _ = setup_plan(
        leaf(target={'C': D(1)}, budget='2400', floor='100', sell=selling),
        own={**rules('R'), **rules('C', currency='cny', buy=0)},
        holdings={'R': D(1)} if selling else {})
    assert plan.funding.demands
    assert [intent.uid for intent in plan.sells] == (['R'] if selling else [])
    quote = value.fx.quotes['cny']
    expired = quote.market_time + timedelta(seconds=ORDER_BOOK_MAX_AGE_SECONDS + 1)
    # Initial snapshot timestamp, quote age check and freshness validation.
    initial_times = iter((NOW, NOW, NOW))
    broker.clock = lambda: next(initial_times, expired)
    fx.reset_mock()
    fx.get_quote.side_effect = [quote, replace(quote, market_time=expired, received_time=expired)]
    open_rule = rules(quote.pair.uid, lot=quote.pair.lot_size)[quote.pair.uid]
    fx.get_trade_rules.side_effect = [open_rule, replace(open_rule, api_trade_available=False)]

    outcome = broker.execute('dst', plan, data, executor, value)

    assert (outcome.state, outcome.stage, outcome.reason) == (
        'DEFERRED', stage, 'fx_trading_unavailable')
    assert outcome.receipts == ()
    assert not data.mock_calls and not executor.mock_calls
    assert fx.mock_calls == [
        call.get_quote(quote.pair), call.get_trade_rules('dst', quote.pair),
    ] * 2


def test_currency_closes_before_second_sale_and_reports_sales_stage():
    """A required FX preflight may stop the sales queue after a filled RUB sale."""
    broker, value, plan, data, executor, fx, _ = setup_plan(
        leaf(target={'C': D(1)}, budget='2400', floor='100', sell=True),
        own={**rules('R', 'S'), **rules('C', currency='cny', buy=0)},
        holdings={'R': D(1), 'S': D(1)})
    assert plan.funding.demands and len(plan.sells) == 2
    data.get_destination.return_value = replace(
        plan.snapshot, quantities={'S': D(1)}, available_quantities={'S': D(1)})
    data.get_trade_rules.return_value = plan.rules
    executor.submit_order.side_effect = lambda _account, order: native_receipt(order)
    fx.get_trade_rules.side_effect = lambda _account, pair: replace(
        rules(pair.uid)[pair.uid], api_trade_available=not executor.submit_order.called)
    result = broker.execute('dst', plan, data, executor, value)
    assert (result.state, result.stage, result.reason) == (
        'PARTIAL', 'sales', 'fx_trading_unavailable')
    assert [item.uid for item in result.receipts] == ['R']
    executor.submit_order.assert_called_once_with('dst', plan.sells[0])


@pytest.mark.parametrize('filled_sale', [False, True])
def test_native_sale_unavailable_outcome_retains_confirmed_fills(filled_sale):
    """The same sale guard reports DEFERRED or PARTIAL at different points."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'Y': D(1)}, budget='2000', sell=True), own=rules('R', 'S', 'Y'),
        holdings={'R': D(1), 'S': D(1), 'Y': D(1)})
    assert [intent.uid for intent in plan.sells] == ['R', 'S']
    if not filled_sale:
        plan = replace(plan, rules={**plan.rules, **rules('R', sell=0)})
    data.get_destination.return_value = replace(
        plan.snapshot, quantities={'S': D(1), 'Y': D(1)},
        available_quantities={'S': D(1), 'Y': D(1)})
    data.get_trade_rules.return_value = {**plan.rules, **rules('S', sell=0)}
    executor.submit_order.side_effect = lambda _account, order: native_receipt(order)
    result = broker.execute('dst', plan, data, executor, value)
    assert (result.state, result.stage, result.reason) == (
        'PARTIAL' if filled_sale else 'DEFERRED', 'sales', 'sale_unavailable')
    assert [item.uid for item in result.receipts] == (['R'] if filled_sale else [])
    assert executor.submit_order.call_count == int(filled_sale)


@pytest.mark.parametrize('blocker,reason', [
    ('settlement', 'settlement_timeout'), ('fee', 'cash_attribution_unavailable'),
    ('sales_credit', 'cash_unavailable'),
])
def test_native_sale_stops_keep_filled_receipt(blocker, reason, monkeypatch):
    """Settlement and proceeds guards do not invalidate the confirmed sale."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'Y': D(1)}, budget='2000', sell=True), own=rules('R', 'Y'),
        holdings={'R': D(1), 'Y': D(1)})
    snapshot = plan.snapshot
    if blocker == 'fee':
        monkeypatch.setattr(market_module, '_sale_fee', create_autospec(
            _sale_fee, spec_set=True, return_value=False))
    if blocker != 'settlement':
        snapshot = replace(snapshot, quantities={'Y': D(1)}, available_quantities={'Y': D(1)})
    if blocker == 'sales_credit':
        snapshot = replace(snapshot, available_cash={})
    data.get_destination.return_value = snapshot
    data.get_trade_rules.return_value = plan.rules
    executor.submit_order.side_effect = lambda _account, order: native_receipt(order)
    monkeypatch.setattr(market_module, 'SETTLEMENT_TIMEOUT', 0)
    result = broker.execute('dst', plan, data, executor, value)
    assert (result.state, result.stage, result.reason) == ('PARTIAL', 'after_sales', reason)
    assert result.receipts == (native_receipt(plan.sells[0]),)
    executor.submit_order.assert_called_once_with('dst', plan.sells[0])


def test_native_buy_settlement_timeout_keeps_buy_receipt(monkeypatch):
    """Settlement before the next purchase stops with the first complete fill."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100'), own=rules('A', 'B'))
    data.get_destination.return_value = plan.snapshot
    data.get_trade_rules.return_value = plan.rules
    executor.submit_order.side_effect = lambda _account, order: native_receipt(order, '450')
    monkeypatch.setattr(market_module, 'SETTLEMENT_TIMEOUT', 0)
    result = broker.execute('dst', plan, data, executor, value)
    assert (result.state, result.stage, result.reason) == (
        'PARTIAL', 'after_buy', 'settlement_timeout')
    assert [item.uid for item in result.receipts] == ['A']
    executor.submit_order.assert_called_once()
