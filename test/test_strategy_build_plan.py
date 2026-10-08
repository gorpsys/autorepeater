"""Direct offline acceptance of new plans while the old runtime remains active."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.composite_strategy import (
    CompositeSnapshot, CompositeStrategy, PreparedCompositeComponent, PreparedCompositeSource,
)
from autorepeater.index_config import AllocationDriftRange, IndexConfig, IndexInstrument
from autorepeater.index_strategy import IndexQuote, IndexStrategy
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_allocation import build_marks, recovered_capital
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.strategy_data import InstrumentType, PortfolioEntry
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, StrategyDecision, StrategyPlan, TradeMode,
    exact_sum, validate_plan,
)


D = Decimal


def entry(uid, quantity, price='10'):
    """A long security at a shared destination mark."""
    return PortfolioEntry(uid, InstrumentType.SHARE, 'RUB', D(price), D(quantity), '')


def leaf(kind='INDEX', reserve='.1', limit='.1', uids=('a',)):
    """Equal-weight leaves use their real, unchanged target calculators."""
    if kind == 'ACCOUNT':
        config = AccountConfig('source', '00123', D(reserve), D(limit))
        strategy = AccountStrategy(PreparedAccountSource('00123', config))
        positions = {uid: entry(uid, '100') for uid in uids}
        return strategy, (positions, D(1000) * len(uids))
    config = IndexConfig('MODEL', D('.05'), [
        IndexInstrument(uid, D(1), D(1), D(1), D(10), D(100) / len(uids), D(100))
        for uid in uids], D(reserve), (AllocationDriftRange(D(0), None, False, D(limit)),))
    return IndexStrategy(config), {uid: IndexQuote(uid, D(10), 1) for uid in uids}


def composite(children, weights, limit='.2'):
    """Saved factories permit repeated occurrences without registry or file reads."""
    components = tuple(PreparedCompositeComponent(
        'OPAQUE', str(index), D(weight), PreparedStrategy(lambda item: item, child, 'model'))
        for index, (child, weight) in enumerate(zip(children, weights)))
    return CompositeStrategy(PreparedCompositeSource('MODEL', components, D(limit)))


# pylint: disable-next=too-many-arguments
def context(strategy, snapshot, budget, positions=(), *, force=False, previous=None):
    """Prepare neutral marks and current capital from the same snapshot."""
    profile = strategy.allocation_profile(snapshot)
    marks = build_marks(positions, (profile,))
    capital = recovered_capital(positions, profile, marks) if previous is None else D(previous)
    return StrategyContext((), D(budget), capital, force, positions, marks)


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
def test_leaf_main_control_and_floor_use_same_snapshot(kind):
    """Control reserves invested value once; main reserves full budget once."""
    strategy, snapshot = leaf(kind, uids=('a', 'b'))
    inputs = context(strategy, snapshot, '1000', (entry('a', '40'), entry('b', '40')))
    original = strategy.build_target
    strategy.build_target = Mock(wraps=original)
    plan = strategy.build_plan(snapshot, inputs)
    assert strategy.build_target.call_args_list[0].args == (snapshot, D(1000))
    assert strategy.build_target.call_args_list[1].args == (snapshot, D(800))
    assert plan.target == original(snapshot, D(1000))
    assert plan.cash_floor == D(100)
    assert plan.decision.mode == TradeMode.BUY_ONLY
    assert plan.decision.metric == 0
    assert not plan.children
    validate_plan(plan, inputs)


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('force', [False, True])
def test_explicit_force_and_empty_investment(kind, force):
    """A numeric reduction alone and zero control never fabricate a sell signal."""
    strategy, snapshot = leaf(kind)
    inputs = context(strategy, snapshot, '100', force=force, previous='200')
    strategy.build_target = Mock(wraps=strategy.build_target)
    plan = strategy.build_plan(snapshot, inputs)
    assert strategy.build_target.call_count == 1
    assert plan.decision.mode == (TradeMode.REBALANCE if force else TradeMode.BUY_ONLY)
    assert plan.decision.metric is None


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('budget', [D(0), D(-1), D('NaN'), True])
def test_all_main_budgets_are_positive_before_builder(kind, budget):
    """Invalid allocation is rejected before using a leaf calculator."""
    strategy, snapshot = leaf(kind)
    inputs = replace(context(strategy, snapshot, '100'), budget=budget)
    strategy.build_target = Mock(side_effect=AssertionError('builder must not run'))
    with pytest.raises(ValueError, match='budget'):
        strategy.build_plan(snapshot, inputs)
    strategy.build_target.assert_not_called()


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('quantities', [{}, {'a': D(0)}, {'a': D(-1)}])
def test_leaf_rejects_empty_zero_or_short_main(kind, quantities):
    """The financial plan has a narrower domain than general TargetPortfolio."""
    strategy, snapshot = leaf(kind)
    strategy.build_target = Mock(return_value=TargetPortfolio(quantities, {'a': D(10)}))
    with pytest.raises(ValueError, match='positive quantity|nonnegative'):
        strategy.build_plan(snapshot, context(strategy, snapshot, '100'))


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('control', [{}, {'a': D(0)}])
@pytest.mark.parametrize('force', [False, True])
def test_unavailable_control_preserves_valid_main_and_force(kind, control, force):
    """Only the internal drift metric depends on a usable control."""
    strategy, snapshot = leaf(kind)
    inputs = context(strategy, snapshot, '100', (entry('a', '10'),),
                     force=force, previous='200')
    main = TargetPortfolio({'a': D(9), 'zero': D(0)}, {'a': D(10), 'zero': D(0)})
    inputs = replace(inputs, marks={**inputs.marks, 'zero': D(0)})
    strategy.build_target = Mock(side_effect=[main, TargetPortfolio(control, {'a': D(10)})])
    plan = strategy.build_plan(snapshot, inputs)
    assert plan.target is main
    assert plan.decision.metric is None
    assert plan.decision.mode == (TradeMode.REBALANCE if force else TradeMode.BUY_ONLY)


@pytest.mark.parametrize('kind', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('quantity, expected', [('60', TradeMode.BUY_ONLY),
                                                ('61', TradeMode.REBALANCE)])
def test_leaf_own_threshold_is_strict(kind, quantity, expected):
    """Equality to the configured limit does not permit internal sales."""
    strategy, snapshot = leaf(kind, reserve='0', uids=('a', 'b'))
    positions = (entry('a', quantity), entry('b', str(100 - D(quantity))))
    plan = strategy.build_plan(snapshot, context(strategy, snapshot, '1000', positions))
    assert plan.decision.mode == expected
    assert plan.decision.redistribution_allowed is False


def test_index_main_budget_crossing_can_enable_internal_sales():
    """The accepted 5.5% drift crosses limits when only full budget increases."""
    strategy, snapshot = leaf(reserve='0', uids=('a', 'b'))
    strategy.config.allocation_drift_limits = (
        AllocationDriftRange(D(0), D(160000), False, D('.059242183')),
        AllocationDriftRange(D(160000), None, False, D('.052455282')),
    )
    positions = (entry('a', '5550'), entry('b', '4450'))
    plans = [strategy.build_plan(snapshot, context(strategy, snapshot, budget, positions))
             for budget in ('159000', '160000', '161000')]
    assert [plan.decision.metric for plan in plans] == [D('.055')] * 3
    assert [plan.decision.mode for plan in plans] == [
        TradeMode.BUY_ONLY, TradeMode.REBALANCE, TradeMode.REBALANCE]
    assert plans[0].decision.limit == D('.059242183')
    assert plans[-1].decision.limit == D('.052455282')


def test_gold_growth_reserve_shortfall_does_not_force_sale():
    """A one-security root does not sell three lots just to replenish reserve."""
    strategy, snapshot = leaf(reserve='.006', limit='0')
    old = strategy.build_plan(snapshot, context(strategy, snapshot, '100000'))
    assert old.target.quantities == {'a': D(9940)}
    snapshot['a'].price = D('10.5')
    inputs = context(strategy, snapshot, '104970', (entry('a', '9940', '10.5'),))
    plan = strategy.build_plan(snapshot, inputs)
    assert inputs.previous_budget == D(105000)
    assert plan.target.quantities == {'a': D(9937)}
    assert plan.cash_floor == D('629.820')
    assert plan.decision.mode == TradeMode.BUY_ONLY
    forced = strategy.build_plan(
        snapshot, replace(inputs, budget_reduction_requires_rebalance=True))
    assert forced.decision.mode == TradeMode.REBALANCE


def test_index_empty_control_from_real_lot_cut_is_not_main_error():
    """Lot rounding can empty control even though main is executable."""
    strategy, snapshot = leaf(reserve='0')
    plan = strategy.build_plan(snapshot, context(strategy, snapshot, '100', (entry('a', '.1'),)))
    assert plan.target.quantities == {'a': D(10)}
    assert plan.decision.metric is None
    assert plan.decision.mode == TradeMode.BUY_ONLY
    with pytest.raises(ValueError, match='positive quantity'):
        strategy.build_plan(snapshot, context(strategy, snapshot, '1'))


def test_nested_repeated_children_floor_paths_shared_uid_and_unassigned():
    """Repeated occurrences keep paths, budgets and intentional cash through nesting."""
    child, child_snapshot = leaf(reserve='.1')
    inner = composite((child, child), ('.6', '.3'))
    inner_snapshot = CompositeSnapshot((child_snapshot, child_snapshot))
    root = composite((inner, child), ('.8', '.1'))
    snapshot = CompositeSnapshot((inner_snapshot, child_snapshot))
    inputs = context(root, snapshot, '1000', (entry('unknown', '2'),))
    plan = root.build_plan(snapshot, inputs)
    assert plan.unassigned == {'unknown': D(2)}
    assert plan.children[0].children[0].path == (0, 0)
    assert plan.children[0].children[1].path == (0, 1)
    assert plan.children[1].path == (1,)
    assert plan.cash_floor == D(262)
    assert plan.target.quantities == {'a': D(73)}
    assert exact_sum(item.budget for item in plan.children) == D(900)
    assert exact_sum(item.budget for item in plan.children[0].children) == D(720)
    validate_plan(plan, inputs)


@pytest.mark.parametrize('nested', [False, True])
def test_all_prices_growth_does_not_force_composite_sales(nested):
    """Uniform growth causes only a reserve deficit in both tree shapes."""
    first, first_snapshot = leaf(reserve='.01', limit='0', uids=('a',))
    second, second_snapshot = leaf(reserve='.01', limit='0', uids=('b',))
    root = composite((first, second), ('.6', '.4'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    if nested:
        root, snapshot = composite((root,), ('1',)), CompositeSnapshot((snapshot,))
    old = root.build_plan(snapshot, context(root, snapshot, '100000'))
    first_snapshot['a'].price = second_snapshot['b'].price = D('10.5')
    positions = tuple(entry(uid, str(quantity), '10.5')
                      for uid, quantity in old.target.quantities.items())
    plan = root.build_plan(snapshot, context(root, snapshot, '104950', positions))
    assert plan.decision.mode == TradeMode.BUY_ONLY
    assert plan.decision.metric == 0
    assert plan.decision.redistribution_allowed is False
    children = plan.children[0].children if nested else plan.children
    assert all(item.decision.mode == TradeMode.BUY_ONLY for item in children)
    assert exact_sum(item.budget for item in children) == D(104950)
    assert plan.cash_floor == D('1049.50')


@pytest.mark.parametrize('force', [False, True])
def test_parent_force_survives_nested_reserve_exception(force):
    """The same numeric budgets inherit different explicit parent obligations."""
    child, child_snapshot = leaf(reserve='.1', limit='0')
    inner = composite((child,), ('1',))
    root = composite((inner,), ('1',))
    snapshot = CompositeSnapshot((CompositeSnapshot((child_snapshot,)),))
    inputs = context(root, snapshot, '95', (entry('a', '9'),), force=force, previous='100')
    plan = root.build_plan(snapshot, inputs)
    expected = TradeMode.REBALANCE if force else TradeMode.BUY_ONLY
    assert plan.decision.mode == plan.children[0].decision.mode == expected
    assert plan.children[0].children[0].decision.mode == expected
    assert plan.decision.redistribution_allowed is False
    assert plan.cash_floor == D('9.5')


def test_reserve_exception_keeps_independent_child_drift():
    """The reserve exemption never cancels an independent child's criterion."""
    first, first_snapshot = leaf(reserve='.01', limit='.01', uids=('a', 'b'))
    second, second_snapshot = leaf(reserve='.01', limit='0', uids=('c',))
    root = composite((first, second), ('.6', '.4'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    positions = (entry('a', '4000'), entry('b', '2237'), entry('c', '4158'))
    inputs = context(root, snapshot, '104950', positions)
    plan = root.build_plan(snapshot, inputs)
    assert plan.decision.mode == TradeMode.BUY_ONLY
    assert plan.children[0].decision.mode == TradeMode.REBALANCE
    assert plan.children[0].decision.reason == 'allocation_drift'
    assert plan.children[1].decision.mode == TradeMode.BUY_ONLY


@pytest.mark.parametrize('quantities', [{}, {'a': D(0)}])
def test_composite_rejects_invalid_opaque_child_plan(quantities):
    """A foreign algorithm's invalid main aborts the composite plan."""
    child = Mock()
    child.allocation_profile.return_value = AllocationProfile({'a': D(1)}, {'a': D(10)}, D(0))
    child.build_plan.side_effect = lambda _snapshot, inputs: StrategyPlan(
        inputs.path, inputs.budget, TargetPortfolio(quantities, {'a': D(10)}), D(0),
        StrategyDecision(TradeMode.BUY_ONLY, False, 'model', None, None), (), {})
    root = composite((child,), ('1',))
    opaque = object()
    snapshot = CompositeSnapshot((opaque,))
    with pytest.raises(ValueError, match='positive quantity'):
        root.build_plan(snapshot, context(root, snapshot, '100'))
    assert child.build_plan.call_args.args[0] is opaque
    child.build_target.assert_not_called()


def test_shared_uid_max_price_and_exact_budget_partitions():
    """Prices merge independently from marks; corrected budgets conserve every digit."""
    first, first_snapshot = leaf(reserve='0')
    second, second_snapshot = leaf(reserve='0')
    second_snapshot['a'].price = D(20)
    root = composite((first, second), ('.3', '.7'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    budget = D('1000.12345678901234567890123456789')
    inputs = context(root, snapshot, str(budget), (entry('a', '10', '15'),))
    plan = root.build_plan(snapshot, inputs)
    assert exact_sum(item.budget for item in plan.children) == budget
    assert plan.target.prices == {'a': D(20)}
    assert plan.target.quantities['a'] == sum(item.target.quantities['a'] for item in plan.children)
    assert inputs.marks == {'a': D(15)}


def test_achieved_nonempty_main_is_valid():
    """No difference to the main target is an ordinary valid plan."""
    strategy, snapshot = leaf(reserve='0', limit='0')
    plan = strategy.build_plan(snapshot, context(strategy, snapshot, '100', (entry('a', '10'),)))
    assert plan.target.quantities == {'a': D(10)}
    assert plan.decision.mode == TradeMode.BUY_ONLY


@pytest.mark.parametrize('first_quantity, expected', [
    ('1.2', TradeMode.BUY_ONLY), ('1.3', TradeMode.REBALANCE),
])
def test_composite_own_threshold_is_strict(first_quantity, expected):
    """The parent uses its own relative share criterion without averaging children."""
    first, first_snapshot = leaf(reserve='0', limit='0', uids=('a',))
    second, second_snapshot = leaf(reserve='0', limit='0', uids=('b',))
    root = composite((first, second), ('.1', '.9'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    positions = (entry('a', first_quantity), entry('b', str(D(10) - D(first_quantity))))
    plan = root.build_plan(snapshot, context(root, snapshot, '100', positions))
    assert plan.decision.mode == expected
    assert plan.decision.redistribution_allowed == (expected == TradeMode.REBALANCE)
    assert plan.children[1].decision.mode == TradeMode.BUY_ONLY


@pytest.mark.parametrize('quantities, budgets, redistribution, modes', [
    (('8000', '2000'), ('60000', '30000'), True,
     (TradeMode.REBALANCE, TradeMode.BUY_ONLY)),
    (('6700', '3300'), ('60300', '29700'), False,
     (TradeMode.REBALANCE, TradeMode.REBALANCE)),
])
def test_composite_deficit_priority_and_child_force(quantities, budgets, redistribution, modes):
    """Target priority and capital shortage reach real one-security child plans."""
    first, first_snapshot = leaf(reserve='0', limit='0', uids=('a',))
    second, second_snapshot = leaf(reserve='0', limit='0', uids=('b',))
    root = composite((first, second), ('.6', '.3'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    positions = tuple(entry(uid, quantity) for uid, quantity in zip(('a', 'b'), quantities))
    plan = root.build_plan(snapshot, context(root, snapshot, '100000', positions))
    assert tuple(item.budget for item in plan.children) == tuple(map(D, budgets))
    assert tuple(item.decision.mode for item in plan.children) == modes
    assert plan.decision.redistribution_allowed is redistribution
    assert plan.cash_floor == D(10000)


@pytest.mark.parametrize('budget', ['0', '1E-1000026'])
def test_composite_zero_budget_aborts_before_child_builders(budget):
    """Zero parent or an underflowed child allocation fails before child builders."""
    first, first_snapshot = leaf(reserve='0', uids=('a',))
    second, second_snapshot = leaf(reserve='0', uids=('b',))
    root = composite((first, second), ('.6', '.4'))
    snapshot = CompositeSnapshot((first_snapshot, second_snapshot))
    first.build_plan = Mock(side_effect=AssertionError('children must not run'))
    second.build_plan = Mock(side_effect=AssertionError('children must not run'))
    inputs = replace(context(root, snapshot, '100'), budget=D(budget))
    with pytest.raises(ValueError, match='budget'):
        root.build_plan(snapshot, inputs)
    first.build_plan.assert_not_called()
    second.build_plan.assert_not_called()
