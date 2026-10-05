"""Pure permissions and component budgets, without a runtime policy switch."""
from dataclasses import replace
from decimal import Decimal as D, getcontext, localcontext, ROUND_UP
import ast
from pathlib import Path

import pytest

from autorepeater.portfolio import TargetPortfolio
from autorepeater.rebalance_policy import (
    allocation_drift, component_drift, leaf_decision, allocate_component_budgets,
)
from autorepeater.strategy_allocation import combine_profiles
from autorepeater.strategy_data import InstrumentType, PortfolioEntry
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, TradeMode, exact_product, exact_sum, validate_context,
)


def holding(uid, quantity, price='1'):
    """A security already valued by the common destination marks."""
    return PortfolioEntry(uid, InstrumentType.SHARE, 'rub', D(price), D(quantity), '')


def control(quantities, prices=None):
    """Source estimates deliberately differ from common marks when requested."""
    return TargetPortfolio({uid: D(value) for uid, value in quantities.items()},
                           prices or dict.fromkeys(quantities, D(1)))


def context(budget='100', positions=(), previous='0', force=False, path=()):
    """Root force is explicitly false; descendants can inherit a reduction."""
    return StrategyContext(path, D(budget), D(previous), force, positions,
                           {entry.uid: entry.current_price for entry in positions})


def components(capitals, reserves=('0', '0'), exposures=None):
    """Independent UIDs make the recovered capital unambiguous."""
    exposures = exposures or tuple(exact_sum((D(1), D(r).copy_negate())) for r in reserves)
    profiles = tuple(AllocationProfile({str(i): f}, {str(i): D(1)}, D(r))
                     for i, (f, r) in enumerate(zip(exposures, reserves)))
    positions = tuple(holding(str(i), exact_product(D(c), f))
                      for i, (c, f) in enumerate(zip(capitals, exposures)))
    return profiles, positions


def distribute(capitals, budget, weights=('.6', '.3'), reserves=('0', '0'),
               *, force=False):
    """Supply only neutral profiles and a parent context to the allocator."""
    profiles, positions = components(capitals, reserves)
    ctx = context(budget, positions, previous='1000000', force=force)
    return allocate_component_budgets(ctx, profiles, tuple(map(D, weights)), D('.2'))


def test_leaf_union_marks_and_half_l1():
    """Absent UIDs are zeros; source prices cannot change shared valuation."""
    positions = (holding('A', '60', '2'), holding('old', '40', '2'))
    target = control({'A': '50', 'new': '50', 'zero': '0'},
                     {'A': D(100), 'new': D(999), 'zero': D(0)})
    marks = {'A': D(2), 'old': D(2), 'new': D(2), 'zero': D(0)}
    assert allocation_drift(positions, target, marks) == D('.5')
    assert allocation_drift(tuple(reversed(positions)), target, marks) == D('.5')


@pytest.mark.parametrize('positions,target', [
    ((), control({'A': '1'})), ((holding('A', '0'),), control({'A': '1'})),
    ((holding('A', '1'),), control({})), ((holding('A', '1'),), control({'A': '0'})),
])
def test_empty_investment_or_control_has_no_metric(positions, target):
    """No invented drift or division by zero for initially empty/control-zero cases."""
    marks = {'A': positions[0].current_price if positions else D(1)}
    assert allocation_drift(positions, target, marks) is None


@pytest.mark.parametrize('limit,mode', [('.1', TradeMode.BUY_ONLY),
                                        ('.0999999999999999999999999999', TradeMode.REBALANCE)])
def test_leaf_strict_limit(limit, mode):
    """Exactly ten percent half-L1 does not permit sales."""
    ctx = context(positions=(holding('A', '60'), holding('B', '40')))
    decision = leaf_decision(ctx, control({'A': '50', 'B': '50'}), D(limit))
    assert decision.mode == mode
    assert decision.metric == D('.1') and decision.limit == D(limit)
    assert not decision.redistribution_allowed


def test_leaf_reduction_reason_and_internal_signal_are_independent():
    """Numerical reserve reduction alone cannot force, but internal drift still can."""
    ctx = context('95', (holding('A', '90'),), previous='100')
    assert leaf_decision(ctx, control({'A': '95'}), D(0)).mode == TradeMode.BUY_ONLY
    forced = replace(ctx, budget_reduction_requires_rebalance=True)
    result = leaf_decision(forced, control({}), D(0))
    assert result.mode == TradeMode.REBALANCE and result.metric is None
    assert result.reason == 'capital_reduction'
    ctx = replace(ctx, marks={**ctx.marks, 'B': D(1)})
    assert leaf_decision(ctx, control({'B': '1'}), D('.2')).mode == TradeMode.REBALANCE


@pytest.mark.parametrize('actual,expected', [('12', '.2'), ('13', '.3')])
def test_component_relative_error(actual, expected):
    """10% to 12% is exactly 20%, not two percentage points."""
    assert component_drift((D(actual), D(100) - D(actual)), (D('.1'), D('.9'))) == D(expected)
    result = distribute((actual, str(D(100) - D(actual))), '100', weights=('.1', '.9'))
    assert result.decision.redistribution_allowed == (actual == '13')


def test_deficit_target_weights_take_priority_above_limit():
    """The accepted 80/20 case must not use the proportional 72/18 split."""
    result = distribute(('80000', '20000'), '100000')
    assert tuple(c.budget for c in result.children) == (D(60000), D(30000))
    assert tuple(c.budget_reduction_requires_rebalance for c in result.children) == (True, False)
    assert result.decision.redistribution_allowed


def test_deficit_below_limit_reduces_capital_without_redistribution():
    """67/33 keeps its proportions in the smaller 90k pool."""
    result = distribute(('67000', '33000'), '100000')
    assert tuple(c.budget for c in result.children) == (D(60300), D(29700))
    assert all(c.budget_reduction_requires_rebalance for c in result.children)
    assert result.decision.mode == TradeMode.REBALANCE
    assert not result.decision.redistribution_allowed


@pytest.mark.parametrize('budget,force', [('90', False), ('95', False),
                                        ('89.99999999999999999999999999', True)])
def test_reserve_boundary_without_epsilon(budget, force):
    """A=M and A<M<S are reserve-only; any strictly M<A cuts capital."""
    result = distribute(('60', '40'), budget, weights=('.6', '.4'), reserves=('.1', '.1'))
    assert all(c.budget_reduction_requires_rebalance == force for c in result.children)
    assert not result.decision.redistribution_allowed


def test_mixed_reserves_and_nested_unallocated_capital():
    """A includes intentional cash; reserve_fraction includes only leaf reserves."""
    result = distribute(('60', '40'), '90', weights=('.6', '.4'), reserves=('.1', '.2'))
    assert not any(c.budget_reduction_requires_rebalance for c in result.children)
    nested = combine_profiles((AllocationProfile({'X': D('.9')}, {'X': D(1)}, D('.1')),),
                              (D('.5'),))
    assert nested.reserve_fraction == D('.05')
    profiles, positions = components(('60', '40'), ('.05', '.05'), (D('.45'), D('.45')))
    result = allocate_component_budgets(context('90', positions), profiles,
                                        (D('.6'), D('.4')), D('.2'))
    assert all(c.budget_reduction_requires_rebalance for c in result.children)


@pytest.mark.parametrize('parent_force', [False, True])
def test_parent_force_survives_local_reserve_exception(parent_force):
    """At S=100 R=10 A=90 M=95 only the ancestor changes the SELL obligation."""
    result = distribute(('60', '40'), '95', weights=('.6', '.4'), reserves=('.1', '.1'),
                        force=parent_force)
    assert all(c.budget_reduction_requires_rebalance == parent_force for c in result.children)
    assert not result.decision.redistribution_allowed


def test_force_only_reduced_children_even_with_parent_permission():
    """Increasing or unchanged budgets retain the child's own criterion."""
    result = distribute(('80', '20'), '100', weights=('.8', '.2'), force=True)
    assert not any(c.budget_reduction_requires_rebalance for c in result.children)
    result = distribute(('80', '20'), '100', weights=('.6', '.4'), force=True)
    assert tuple(c.budget_reduction_requires_rebalance for c in result.children) == (True, False)


def test_initial_partial_pool_and_zero_capital():
    """First allocation distributes 90%, preserving 10% at the parent."""
    result = distribute(('0', '0'), '10000')
    assert tuple(c.budget for c in result.children) == (D(6000), D(3000))
    assert result.unallocated_budget == D(1000)
    assert result.decision.mode == TradeMode.BUY_ONLY and result.decision.metric is None
    assert not any(c.budget_reduction_requires_rebalance for c in result.children)
    inherited = distribute(('0', '0'), '10000', force=True)
    assert not any(c.budget_reduction_requires_rebalance for c in inherited.children)
    assert not inherited.decision.redistribution_allowed


def test_surplus_to_deficits_and_no_surplus():
    """Overweight holdings keep C; surplus funds monetary underweights."""
    result = distribute(('65', '25'), '100', weights=('.7', '.3'))
    assert tuple(c.budget for c in result.children) == (D(70), D(30))
    result = distribute(('72', '28'), '100', weights=('.7', '.3'))
    assert tuple(c.budget for c in result.children) == (D(72), D(28))
    result = distribute(('72', '23'), '100', weights=('.7', '.3'))
    assert tuple(c.budget for c in result.children) == (D(72), D(28))
    assert result.decision.mode == TradeMode.BUY_ONLY


def test_nested_scale_paths_and_unassigned_economic_value():
    """Unassigned U enters B once and never becomes fictitious spendable cash."""
    profiles, positions = components(('0', '0'))
    ctx = context('10000', positions + (holding('old', '10000'),), path=(2,))
    result = allocate_component_budgets(ctx, profiles, (D('.6'), D('.3')), D('.2'))
    assert result.unassigned == {'old': D(10000)}
    assert tuple(c.path for c in result.children) == ((2, 0), (2, 1))
    nested = allocate_component_budgets(result.children[0], profiles, (D('.6'), D('.3')), D('.2'))
    assert tuple(c.budget for c in nested.children) == (D(3600), D(1800))
    assert nested.unallocated_budget == D(600)


@pytest.mark.parametrize('capitals,budget', [
    (('1', '1', '1'), '3.000000000000000000000000001'),
    (('0', '0', '0'), '1e-80'), (('1e80', '1e80', '1e80'), '1e80'),
])
def test_exact_budget_conservation(capitals, budget):
    """Preserve corrected residuals through addition, paths and force comparisons."""
    result = distribute(capitals, budget, weights=('.3', '.3', '.3'), reserves=('0',) * 3)
    pool = exact_product(D(budget), D('.9'))
    assert exact_sum(c.budget for c in result.children) == pool
    assert exact_sum((pool, result.unallocated_budget)) == D(budget)
    assert all(c.budget > 0 for c in result.children)
    for child in result.children:
        validate_context(child)
    assert getcontext().prec == 28


def test_force_compares_corrected_not_rounded_products():
    """The residual receiver is unchanged despite a smaller intermediate quotient."""
    third = '0.3333333333333333333333333334'
    weights = (D(third),) * 3
    profiles, positions = components((third,) * 3, ('0',) * 3)
    ctx = context('1', positions, previous='2', force=True)
    result = allocate_component_budgets(ctx, profiles, weights, D(0))
    assert result.children[0].budget == D(third)
    flags = tuple(c.budget_reduction_requires_rebalance for c in result.children)
    assert flags == (False, True, True)


@pytest.mark.parametrize('limit', [D(-1), D(1), D('NaN'), D('Infinity'), '0', True])
def test_bad_policy_limit(limit):
    """No coercion of settings supplied by a strategy."""
    with pytest.raises(ValueError):
        leaf_decision(context(), control({}), limit)
    with pytest.raises(ValueError):
        allocate_component_budgets(context(), (), (), limit)


@pytest.mark.parametrize('target,marks', [
    (control({'A': '-1'}), {'A': D(1)}), (control({'A': '1'}), {}),
    (control({'A': '1'}), {'A': D(0)}), (control({'A': '1'}), {'A': D('NaN')}),
    (TargetPortfolio({'A': D(1)}, {}), {'A': D(1)}), (object(), {'A': D(1)}),
])
def test_bad_control_or_marks_fails_before_metric(target, marks):
    """Even an empty invested portfolio cannot mask malformed control data."""
    with pytest.raises(ValueError):
        allocation_drift((), target, marks)


@pytest.mark.parametrize('capitals,weights', [
    ((), ()), ((D(-1),), (D(1),)), ((D('NaN'),), (D(1),)),
    ((D(1),), (D(0),)), ((D(1),), (D(2),)), ((D(1), D(2)), (D(1),)),
    ([D(1)], (D(1),)), ((D(1),), [D(1)]),
])
def test_bad_component_metric_inputs(capitals, weights):
    """Bad amounts or target fractions are errors rather than fallback modes."""
    with pytest.raises(ValueError):
        component_drift(capitals, weights)


def test_invalid_profiles_context_and_zero_child_abort():
    """Zero f and zero final budget abort the entire distribution."""
    with pytest.raises(ValueError, match='fraction'):
        allocate_component_budgets(context(), (AllocationProfile({}, {}, D(0)),), (D(1),), D(0))
    with localcontext() as decimal_context:
        decimal_context.Emin = -10
        with pytest.raises(ValueError, match='positive'):
            distribute(('0', '0'), '1e-37', weights=('.01', '.99'))
    with pytest.raises(ValueError, match='budget_reduction'):
        leaf_decision(context(force=True), control({}), D(0))


def test_pure_module_import_boundaries():
    """Policy imports mathematics and neutral models, never I/O or algorithms."""
    path = Path(__file__).parents[1] / 'autorepeater/rebalance_policy.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert set(imports) <= {'dataclasses', 'decimal', 'autorepeater.portfolio',
                            'autorepeater.strategy_plan', 'autorepeater.strategy_allocation'}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {'open', 'print'} for node in ast.walk(tree))


def test_above_limit_overrides_reserve_only_deficit():
    """A<=M<S cannot disable reduction when the parent's own threshold is exceeded."""
    result = distribute(('80', '20'), '95', reserves=('.5', '.5'))
    assert tuple(c.budget for c in result.children) == (D(57), D('28.5'))
    assert tuple(c.budget_reduction_requires_rebalance for c in result.children) == (True, False)
    assert result.decision.redistribution_allowed


def test_leaf_without_control_stays_buy_only_and_empty_component_metric():
    """Unavailable metrics carry a reason, never an artificial permission to sell."""
    ctx = context('95', (holding('A', '90'),), previous='100')
    decision = leaf_decision(ctx, control({}), D(0))
    assert decision.mode == TradeMode.BUY_ONLY and decision.metric is None
    assert decision.reason == 'control_unavailable'
    assert component_drift((D(0), D(0)), (D('.6'), D('.3'))) is None


def test_target_partition_retries_negative_residual():
    """A subnormal rounding context may overallocate products; retry rather than clip."""
    with localcontext() as decimal_context:
        decimal_context.Emin = -10
        decimal_context.rounding = ROUND_UP
        result = distribute(('0', '0', '0'), '1e-37', weights=('.3',) * 3, reserves=('0',) * 3)
        assert exact_sum(child.budget for child in result.children) == D('1e-37')
        assert all(child.budget > 0 for child in result.children)


@pytest.mark.parametrize('budget', ['0.99999999999999999999999999999',
                                  '1000000000000000000000000000.1'])
def test_full_pool_cannot_overspend_or_lose_parent_residual(budget):
    """W=1 exhausts exactly B, even when B has more than 28 significant digits."""
    result = distribute(('0', '0'), budget, weights=('.6', '.4'))
    assert result.unallocated_budget == 0
    assert exact_sum(child.budget for child in result.children) == D(budget)
    assert all(0 < child.budget <= D(budget) for child in result.children)


@pytest.mark.parametrize('capital', ['50000', '100000', '500000'])
def test_monthly_2000_funds_children_without_reserving_twice(capital):
    """The economic pool receives 1800; leaf reserves are not subtracted here."""
    total = D(capital)
    old = (total * D('.6'), total * D('.3'))
    result = distribute(tuple(map(str, old)), str(total + D(2000)), reserves=('.01', '.01'))
    assert tuple(c.budget for c in result.children) == (old[0] + D(1200), old[1] + D(600))
    assert not any(c.budget_reduction_requires_rebalance for c in result.children)
    for child in result.children:
        quantities = {entry.uid: entry.quantity for entry in child.positions}
        assert leaf_decision(child, TargetPortfolio(quantities, child.marks), D(0)).mode == (
            TradeMode.BUY_ONLY)


def test_price_growth_does_not_force_reserve_restoration():
    """The agreed GOLD and simultaneous 5% composite growth examples stay buy-only."""
    ctx = context('104970', (holding('GOLD', '9940', '10.5'),), previous='105000')
    assert leaf_decision(ctx, control({'GOLD': '9880'}), D(0)).mode == TradeMode.BUY_ONLY
    result = distribute(('63000', '42000'), '104950', weights=('.6', '.4'),
                        reserves=('.01', '.01'))
    assert result.decision.metric == 0 and result.decision.reason == 'reserve_shortfall'
    assert not any(c.budget_reduction_requires_rebalance for c in result.children)


def test_shared_positions_keep_value_and_repeated_occurrences(caplog):
    """Budget policy preserves 7.5/2.5 ownership and a common price through nesting."""
    profiles = (AllocationProfile({'X': D('.5')}, {'X': D(999)}, D('.1')),
                AllocationProfile({'X': D('.25')}, {'X': D(1)}, D('.2')))
    ctx = context('2000', (holding('X', '10', '100'),), path=(3,))
    result = allocate_component_budgets(ctx, profiles, (D('.6'), D('.4')), D('.2'))
    assert tuple(c.positions[0].quantity for c in result.children) == (D('7.5'), D('2.5'))
    assert tuple(c.previous_budget for c in result.children) == (D(1500), D(1000))
    assert all(c.marks['X'] == 100 for c in result.children)
    assert 'Ambiguous' in caplog.text and '(3, 0)' in caplog.text and '(3, 1)' in caplog.text
    nested = allocate_component_budgets(result.children[0], (profiles[0],) * 2,
                                        (D('.5'),) * 2, D('.2'))
    assert tuple(c.path for c in nested.children) == ((3, 0, 0), (3, 0, 1))
    assert exact_sum(c.positions[0].quantity for c in nested.children) == D('7.5')


def test_inconsistent_partition_cannot_escape_allocator(monkeypatch):
    """Fail closed if a partition provider ever violates exact pool conservation."""
    monkeypatch.setattr('autorepeater.rebalance_policy._target_budgets',
                        lambda _budget, _weights, pool: (pool, pool))
    with pytest.raises(ValueError, match='conserve'):
        distribute(('0', '0'), '100')
