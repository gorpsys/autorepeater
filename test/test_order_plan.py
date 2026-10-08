"""Physical lot bounds preserve fractional owners and isolated money."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_plan import StrategyDecision, StrategyPlan, TradeMode, exact_sum
from autorepeater.order_plan import build_order_plan
from autorepeater.order_plan import OrderIntent, take_pieces, validate_intent


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
    assert intent.lots == 1
    assert intent.pieces == {(0,): D('7.5'), (1,): D('2.5')}
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
    """unready defers everything."""
    caplog.set_level('INFO', logger='tinkoffBot')
    result = build_order_plan(leaf(sell=True), replace(destination({'X': D(20)}), **change),
                              {(): {'X': D(20)}}, {'X': D(1)}, rules('X'))
    assert not result.sells and not result.buys
    assert 'defer' in caplog.text.lower()


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
def test_sale_intersects_own_cap_and_free_physical_quantity(cap, free, expected):
    """sale intersects own cap and free physical quantity."""
    snapshot = replace(destination({'X': D(20)}), available_quantities={'X': D(free)})
    result = build_order_plan(leaf(target={'Y': D(1)}, sell=True), snapshot,
                              {(): {'X': D(20)}}, {'X': D(1), 'Y': D(1)},
                              rules('X', 'Y', lot=10, sell=cap))
    assert sum(item.lots for item in result.sells) == expected


@pytest.mark.parametrize('rule', [None, replace(rules('X')['X'], bestprice_order_available=False)])
def test_unavailable_sale_metadata_defers(rule):
    """unavailable sale metadata defers."""
    result = build_order_plan(leaf(sell=True), destination({'X': D(20)}),
                              {(): {'X': D(20)}}, {'X': D(1)}, {'X': rule} if rule else {})
    assert not result.sells


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
