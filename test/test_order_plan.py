"""Physical lot bounds preserve fractional owners and isolated money."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from autorepeater import order_plan, purchase_plan
from autorepeater.execution_data import ActiveOrder, ExecutionSnapshot, TradeRules
from autorepeater.fx import FxSnapshot
from autorepeater.fx_funding import FundingInputs
from autorepeater.order_intent import OrderIntent, take_pieces, validate_intent
from autorepeater.order_plan import build_order_plan
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_plan import StrategyDecision, StrategyPlan, TradeMode, exact_sum


def leaf(path=(), target=None, budget='100', floor='0', sell=False):
    """A neutral financial plan; target quantities are pieces."""
    target = target or {'X': D(10)}
    return StrategyPlan(path, D(budget),
                        TargetPortfolio(target, dict.fromkeys(target, D(1))),
                        D(floor), StrategyDecision(
                            TradeMode.REBALANCE if sell else TradeMode.BUY_ONLY,
                            False, 'test', None, None), (), {})


def parent(children, redistribute=False, unassigned=None):
    """Combine main targets without changing child permissions."""
    quantities = {}
    for child in children:
        for uid, quantity in child.target.quantities.items():
            quantities[uid] = exact_sum((quantities.get(uid, D(0)), quantity))
    return StrategyPlan((), exact_sum(child.budget for child in children),
                        TargetPortfolio(quantities, dict.fromkeys(quantities, D(1))),
                        exact_sum(child.cash_floor for child in children),
                        StrategyDecision(
                            TradeMode.REBALANCE if redistribute else TradeMode.BUY_ONLY,
                            redistribute, 'test', None, None), children, unassigned or {})


def destination(quantities=None, cash='100'):
    """No live data or SDK involved."""
    quantities = quantities or {}
    return ExecutionSnapshot(PortfolioSnapshot(()), D(100), quantities,
                             dict.fromkeys(quantities, D(1)), {'rub': D(cash)},
                             dict(quantities), (), True)


def rules(*uids, lot=1, currency='rub', buy=100000000, sell=100000000):
    """Documented own limits, expressed in native currency and lots."""
    return dict.fromkeys(uids, TradeRules(lot, currency, True, True, D('1e20'), buy, sell))


def test_protected_fraction_cannot_finance_rounded_sale():
    """protected fraction cannot finance rounded sale."""
    tree = parent((leaf((0,), {'X': D(0), 'Y': D(1)}, sell=True),
                   leaf((1,), {'X': D('2.5')})))
    plan = build_order_plan(tree, destination({'X': D(10)}),
                            {(0,): {'X': D('7.5')}, (1,): {'X': D('2.5')}},
                            {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert not plan.sells


def test_fractional_sellers_form_one_physical_lot():
    """fractional sellers form one physical lot."""
    tree = parent((leaf((0,), {'X': D(0), 'Y': D(1)}, sell=True),
                   leaf((1,), {'X': D(0), 'Y': D(1)}, sell=True)))
    plan = build_order_plan(tree, destination({'X': D(10)}),
                            {(0,): {'X': D('7.5')}, (1,): {'X': D('2.5')}},
                            {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert len(plan.sells) == 1
    intent = plan.sells[0]
    assert isinstance(intent, OrderIntent)
    assert intent == OrderIntent('X', 'SELL', 1, 10, {(0,): D('7.5'), (1,): D('2.5')})
    assert exact_sum(intent.pieces.values()) == D(intent.lots * intent.lot_size)


def test_opposing_virtual_changes_do_not_trade():
    """opposing virtual changes do not trade."""
    tree = parent((leaf((0,), {'X': D(5)}, sell=True), leaf((1,), {'X': D(5)})))
    result = build_order_plan(tree, destination({'X': D(10)}),
                              {(0,): {'X': D(10)}, (1,): {}}, {'X': D(1)}, rules('X'))
    assert not result.sells
    assert not result.buys


def test_unassigned_sale_and_dust_are_separate_from_buy_only():
    """unassigned sale and dust are separate from buy only."""
    tree = parent((leaf((0,), {'Y': D(1)}),), unassigned={'X': D('23.5')})
    result = build_order_plan(tree, destination({'X': D('23.5')}), {(0,): {}},
                              {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert result.sells[0].lots == 2
    assert result.sells[0].pieces == {(): D(20)}


@pytest.mark.parametrize('change', [{'limits_ready': False}, {'active_orders': ('active',)}])
def test_unready_defers_everything(change, caplog):
    """Unready planning logs account blockers in the existing format."""
    caplog.set_level('INFO', logger='tinkoffBot')
    snapshot = replace(destination({'X': D(20)}), **change)
    result = build_order_plan(leaf(sell=True), snapshot,
                              {(): {'X': D(20)}}, {'X': D(1)}, rules('X'))
    assert not result.sells and not result.buys
    assert caplog.messages == [
        'Defer execution: active orders or unavailable/blocked limits; stage=planning '
        f'limits_ready={snapshot.limits_ready} active_orders={len(snapshot.active_orders)} '
        'available_cash={rub: 100.0}']
    assert caplog.records[0].levelname == 'INFO'


@pytest.mark.parametrize('snapshot, expected', [
    (destination(), True),
    (replace(destination(), limits_ready=False), False),
    (replace(destination(), active_orders=(ActiveOrder('id', 'X', 'NEW', 1, 0),)), False),
])
def test_ready_uses_shared_predicate_once(snapshot, expected, caplog, monkeypatch):
    """The facade reports and returns the single evaluation of the common predicate."""
    caplog.set_level('INFO', logger='tinkoffBot')
    assert order_plan.order_snapshot_ready(snapshot) is expected
    calls = []

    def predicate(value):
        calls.append(value)
        return not expected

    monkeypatch.setattr(order_plan, 'order_snapshot_ready', predicate)
    assert order_plan.ready(snapshot, stage='refresh') is not expected
    assert calls == [snapshot]
    if expected:
        assert caplog.messages == [
            'Defer execution: active orders or unavailable/blocked limits; '
            'stage=refresh limits_ready=True active_orders=0 available_cash={rub: 100.0}']
    else:
        assert not caplog.records


@pytest.mark.parametrize('module, function', [
    (order_plan, '_sale_capacities'), (purchase_plan, '_candidate'),
])
def test_sell_deferral_is_logged_before_later_calculation_fails(
        module, function, caplog, monkeypatch):
    """A failure in another UID or BUY cannot discard the earlier SELL diagnosis."""
    caplog.set_level('INFO', logger='tinkoffBot')
    diagnosis = 'Defer SELL UID A: currency or API/BESTPRICE unavailable'
    expected = ValueError('later calculation failure')

    def fail(*_args, **_kwargs):
        assert caplog.messages == [diagnosis]
        raise expected

    # A is unavailable and must be handled before B despite the input order.
    quantities = {'B': D(1), 'A': D(1)}
    monkeypatch.setattr(module, function, fail)
    with pytest.raises(ValueError, match='later calculation failure') as error:
        build_order_plan(leaf(target={'B': D(1)}, sell=True), destination(quantities),
                         {(): quantities}, {'A': D(1), 'B': D(1)}, rules('B'))
    assert error.value is expected
    assert caplog.messages == [diagnosis]


def test_funding_validation_follows_sell_and_empty_buy_deferrals(caplog):
    """Invalid FX rules are rejected after the domestic preview reports its results."""
    caplog.set_level('INFO', logger='tinkoffBot')
    funding = FundingInputs(fx_rules={'cny-rub': replace(rules('cny-rub')['cny-rub'], lot=True)})
    with pytest.raises(ValueError, match=r'trade rules\[cny-rub\]: invalid lot'):
        build_order_plan(leaf(sell=True), destination({'X': D(20)}), {(): {'X': D(20)}},
                         {'X': D(1)}, {}, funding=funding)
    assert caplog.messages == [
        'Defer SELL UID X: currency or API/BESTPRICE unavailable',
        'Defer purchases: no funded executable underweight lot']


def test_physical_calculations_use_only_loaded_inputs(monkeypatch):
    """Pure steps preserve ownership and defer reporting to the caller."""
    tree = parent((leaf((0,), {'Y': D(2)}),), unassigned={'X': D('3.5')})
    snapshot = replace(destination({'X': D('3.5')}),
                       fx_snapshot=FxSnapshot({}, datetime(2026, 10, 15, tzinfo=timezone.utc)))
    initial = {(0,): {}}
    marks = {'X': D(1), 'Y': D(1)}
    own = {**rules('X', lot=2), **rules('Y')}
    sell = OrderIntent('X', 'SELL', 1, 2, {(): D(2)})
    buy = OrderIntent('Y', 'BUY', 2, 1, {(0,): D(2)})

    def fail(*_args, **_kwargs):
        raise AssertionError('unexpected effect in physical calculation')

    with monkeypatch.context() as guard:
        guard.setattr('builtins.open', fail)
        guard.setattr('io.open', fail)
        guard.setattr('socket.socket.connect', fail)
        guard.setattr('logging.Logger._log', fail)
        ownership, money = order_plan.prepare_order_preview(tree, snapshot, initial, marks, own)
        steps = list(order_plan.iter_sale_steps(tree, snapshot, ownership, own))
        buys = purchase_plan.calculate_purchase_intents(
            tree, snapshot, ownership, marks, own, money)
        assert not purchase_plan.calculate_purchase_intents(
            tree, snapshot, ownership, marks, own, {(0,): D(0)})
        plan = order_plan.finish_order_plan(tree, snapshot, marks, own,
                                            (ownership, money, (sell,), buys),
                                            funding=FundingInputs())
    assert initial == {(0,): {}}
    assert ownership == {(0,): {}, (): {'X': D('3.5')}} and money == {(0,): D(100)}
    assert steps == [('X', sell, 'residual')]
    assert plan.sells == (sell,) and plan.buys == (buy,)
    assert plan.marks == marks and plan.marks is not marks
    assert plan.rules == own and plan.rules is not own
    assert plan.funding is not None and not plan.funding.exchanges


def test_all_targets_validated_before_unassigned_sale():
    """all targets validated before unassigned sale."""
    tree = parent((leaf((0,)), leaf((1,), {'X': D(0)})), unassigned={'Z': D(10)})
    with pytest.raises(ValueError, match='positive quantity'):
        build_order_plan(tree, destination({'Z': D(10)}), {(0,): {}, (1,): {}},
                         {'Z': D(1), 'X': D(1)}, rules('Z', 'X'))


def test_ownership_must_exactly_conserve_snapshot():
    """ownership must exactly conserve snapshot."""
    with pytest.raises(ValueError, match='ownership'):
        build_order_plan(leaf(), destination({'X': D(10)}), {(): {'X': D(9)}},
                         {'X': D(1)}, rules('X'))


@pytest.mark.parametrize('intent', [None, OrderIntent('', 'BUY', 1, 1, {(): D(1)}),
                                    OrderIntent('X', 'OTHER', 1, 1, {(): D(1)}),
                                    OrderIntent('X', 'BUY', True, 1, {(): D(1)}),
                                    OrderIntent('X', 'SELL', 1, 0, {(): D(1)}),
                                    OrderIntent('X', 'BUY', 1, 1, {}),
                                    OrderIntent('X', 'BUY', 1, 1, {(): D('.9')})])
def test_invalid_intent_rejected_before_boundary(intent):
    """invalid intent rejected before boundary."""
    with pytest.raises(ValueError):
        validate_intent(intent)


@pytest.mark.parametrize('change', [{'lot': 0}, {'lot': True}, {'currency': ''},
                                    {'api_trade_available': 1}, {'bestprice_order_available': None},
                                    {'buy_max_lots': True}, {'sell_max_lots': -1}])
def test_invalid_own_rules_rejected(change):
    """invalid own rules rejected."""
    with pytest.raises(ValueError, match='trade rules'):
        build_order_plan(leaf(), destination(), {(): {}}, {'X': D(1)},
                         {'X': replace(rules('X')['X'], **change)})


def test_missing_or_malformed_metadata_never_guessed():
    """missing or malformed metadata never guessed."""
    with pytest.raises(ValueError, match='TradeRules'):
        build_order_plan(leaf(), destination(), {(): {}}, {'X': D(1)}, {'X': None})
    with pytest.raises(ValueError, match='paths'):
        build_order_plan(leaf(), destination(), {(1,): {}}, {'X': D(1)}, rules('X'))
    with pytest.raises(ValueError, match='missing target'):
        build_order_plan(leaf(), destination(), {(): {}}, {}, rules('X'))
    with pytest.raises(ValueError, match='missing destination'):
        build_order_plan(leaf(), destination({'Z': D(1)}), {(): {'Z': D(1)}},
                         {'X': D(1)}, rules('X', 'Z'))
    with pytest.raises(ValueError, match='missing unassigned'):
        build_order_plan(parent((leaf((0,)),), unassigned={'Z': D(2)}),
                         destination({'Z': D(1)}), {(0,): {}, (): {'Z': D(1)}},
                         {'X': D(1), 'Z': D(1)}, rules('X', 'Z'))


def test_insufficient_owner_capacity_is_error():
    """insufficient owner capacity is error."""
    with pytest.raises(ValueError, match='ownership'):
        take_pieces(D(10), {(0,): D('7.5')})


@pytest.mark.parametrize('cap,free,expected', [(10, '9.9', 0), (0, '10', 0), (1, '20', 1)])
def test_sale_intersects_own_cap_and_free_physical_quantity(cap, free, expected, caplog):
    """A constrained sale reports one residual whether zero or some lots fit."""
    caplog.set_level('INFO', logger='tinkoffBot')
    snapshot = replace(destination({'X': D(20)}), available_quantities={'X': D(free)})
    result = build_order_plan(leaf(target={'Y': D(1)}, sell=True), snapshot,
                              {(): {'X': D(20)}}, {'X': D(1), 'Y': D(1)},
                              rules('X', 'Y', lot=10, sell=cap))
    assert sum(item.lots for item in result.sells) == expected
    assert caplog.messages == [
        'Defer SELL residual UID X: physical lot/ownership/own cap',
        'Defer purchases: no funded executable underweight lot']
    assert all(record.levelname == 'INFO' for record in caplog.records)


@pytest.mark.parametrize('rule', [None, replace(rules('X')['X'], bestprice_order_available=False)])
def test_unavailable_sale_metadata_defers(rule, caplog):
    """An unavailable sale logs once before the empty purchase queue."""
    caplog.set_level('INFO', logger='tinkoffBot')
    result = build_order_plan(leaf(sell=True), destination({'X': D(20)}),
                              {(): {'X': D(20)}}, {'X': D(1)}, {'X': rule} if rule else {})
    assert not result.sells
    assert caplog.messages == [
        'Defer SELL UID X: currency or API/BESTPRICE unavailable',
        'Defer purchases: no funded executable underweight lot']
    assert all(record.levelname == 'INFO' for record in caplog.records)


def test_zero_position_mark_is_allowed_but_nonzero_requires_positive_mark():
    """zero position mark is allowed but nonzero requires positive mark."""
    result = build_order_plan(leaf(target={'X': D(0), 'Y': D(1)}), destination({'X': D(0)}),
                              {(): {'X': D(0)}}, {'X': D(0), 'Y': D(1)}, rules('X', 'Y'))
    assert [intent.uid for intent in result.buys] == ['Y']
    with pytest.raises(ValueError, match='positive mark'):
        build_order_plan(leaf(), destination(), {(): {}}, {'X': D(0)}, rules('X'))


def test_unconverted_native_currency_sale_defers():
    """unconverted native currency sale defers."""
    result = build_order_plan(leaf(target={'Y': D(1)}, sell=True),
                              destination({'X': D(10)}), {(): {'X': D(10)}},
                              {'X': D(1), 'Y': D(1)}, rules('X', 'Y', currency='usd'))
    assert not result.sells and not result.buys


def test_seller_contribution_uses_its_delta_before_other_holdings():
    """seller contribution uses its delta before other holdings."""
    tree = parent((leaf((0,), {'X': D(99)}, sell=True),
                   leaf((1,), {'X': D(0), 'Y': D(1)}, sell=True)))
    result = build_order_plan(tree, destination({'X': D(109)}),
                              {(0,): {'X': D(100)}, (1,): {'X': D(9)}},
                              {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert result.sells[0].pieces == {(0,): D(1), (1,): D(9)}


def test_nearest_sale_rounding_uses_only_permitted_owner_residual():
    """A fractional target may round SELL up within the selling owner's holdings."""
    result = build_order_plan(leaf(target={'X': D('.4'), 'Y': D(1)}, sell=True),
                              destination({'X': D(10)}), {(): {'X': D(10)}},
                              {'X': D(1), 'Y': D(1)}, rules('X', 'Y', lot=10))
    assert result.sells[0].lots == 1
    assert result.sells[0].pieces == {(): D(10)}


def test_nonzero_current_position_requires_positive_mark():
    """A removed position still needs a usable valuation before any sale."""
    with pytest.raises(ValueError, match='positive mark required for destination'):
        build_order_plan(leaf(), destination({'Z': D(1)}), {(): {'Z': D(1)}},
                         {'X': D(1), 'Z': D(0)}, rules('X', 'Z'))


def test_fee_paid_from_reserve_does_not_hold_floor_again():
    """A pure second calculation admits 450 after a 450+5 debit from 1000."""
    tree = leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100')
    own = {uid: replace(rule, buy_money_amount=D(545))
           for uid, rule in rules('A', 'B').items()}
    result = build_order_plan(tree, destination({'A': D(1)}, cash='545'),
                              {(): {'A': D(1)}}, {'A': D(450), 'B': D(450)}, own)
    assert [(item.uid, item.lots) for item in result.buys] == [('B', 1)]
    assert result.money == {(): D(450)}


@pytest.mark.parametrize('cash,cap,expected', [
    ('449', '545', 0), ('545', '449', 0), ('455', '455', 1)])
def test_floor_fix_keeps_real_cash_and_own_money_caps(cash, cap, expected):
    """Removing the repeated floor cannot manufacture available money."""
    tree = leaf(target={'X': D(1)}, budget='500', floor='50')
    result = build_order_plan(tree, destination(cash=cash), {(): {}}, {'X': D(450)},
                              {'X': replace(rules('X')['X'], buy_money_amount=D(cap))})
    assert sum(item.lots for item in result.buys) == expected
