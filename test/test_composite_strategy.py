"""Ordered composition, pure arithmetic and all-or-nothing target validation."""
from copy import deepcopy
from decimal import Decimal
from test.test_strategy_contract import (
    IndependentSource, IndependentStrategy, contract_case, pure_plan_target,
)
from unittest.mock import Mock, call, create_autospec, patch

import pytest

from autorepeater import strategies
from autorepeater.composite_config import CompositeComponent, CompositeConfig
from autorepeater.composite_strategy import (
    CompositeStrategy, PreparedCompositeComponent, PreparedCompositeSource,
    prepare_composite_source,
)
from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_contract import (
    AlgorithmDefinition, PreparationContext, PreparedStrategy, Strategy, create_strategy,
)
from autorepeater.strategy_data import DataAccessError, PositionEvent, StrategyData
from autorepeater.strategy_plan import (AllocationProfile, StrategyContext, StrategyDecision,
                                        StrategyPlan, TradeMode)


def make_composite(children, weights=None, name='root', algorithms=None, sources=None):
    """Create a composition from saved factories without a registry or config I/O."""
    weights = weights or ['0.25'] * len(children)
    algorithms = algorithms or ['LEAF'] * len(children)
    sources = sources or [str(index) for index in range(len(children))]
    prepared = PreparedCompositeSource(name, tuple(
        PreparedCompositeComponent(algorithm, src, Decimal(weight),
                                   PreparedStrategy(lambda value: value, child, src))
        for child, weight, algorithm, src in zip(children, weights, algorithms, sources)),
            component_drift_limit=Decimal('0.20'))
    return CompositeStrategy(prepared)


def mock_child(target=None):
    """Strict contract mock with an opaque snapshot and a complete target."""
    child = create_autospec(Strategy, instance=True, spec_set=True)
    child.load_snapshot.side_effect = lambda data: object()
    target = target if target is not None else TargetPortfolio(
        {'uid': Decimal('1')}, {'uid': Decimal('2')})
    child.allocation_profile.return_value = AllocationProfile(
        {'uid': Decimal(1)}, {'uid': Decimal(2)}, Decimal(0))
    child.build_plan.side_effect = lambda _snapshot, context: StrategyPlan(
        context.path, context.budget, target, Decimal(0),
        StrategyDecision(TradeMode.BUY_ONLY, False, 'test leaf', None, None),
        (), {}, context.positions)
    child.event_accounts.return_value = ('dst',)
    child.should_rebalance.return_value = False
    return child


def test_preparer_uses_context_in_order_and_keeps_saved_factories():
    """The source name and each factory are frozen before any constructor runs."""
    children = [mock_child(), mock_child(), mock_child()]
    config = CompositeConfig('chosen', (
        CompositeComponent('LEAF', 'repeat', Decimal('0.4')),
        CompositeComponent('LEAF', 'repeat', Decimal('0.3')),
        CompositeComponent('OTHER', 'opaque source', Decimal('0.1'))),
            component_drift_limit=Decimal('0.20'))
    factories = [Mock(return_value=child) for child in children]
    prepared_children = [PreparedStrategy(factory, object(), component.src)
                         for factory, component in zip(factories, config.components)]
    context = create_autospec(PreparationContext, instance=True, spec_set=True)
    context.prepare.side_effect = prepared_children
    with patch('autorepeater.composite_strategy.select_composite_config',
               return_value=config) as select:
        prepared = prepare_composite_source('chosen', context)
        select.assert_called_once_with('chosen')
        assert context.prepare.call_args_list == [
            call(item.algoritm, item.src) for item in config.components]
        assert [item.prepared for item in prepared.components] == prepared_children
        for factory in factories:
            factory.assert_not_called()
    with patch('builtins.open', side_effect=AssertionError('constructor I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('constructor I/O')):
        strategy = create_strategy(PreparedStrategy(CompositeStrategy, prepared, 'chosen'))
    assert strategy.children == tuple(children)
    for factory, child_prepared in zip(factories, prepared_children):
        factory.assert_called_once_with(child_prepared.prepared_source)


def test_real_preparation_tree_repeats_siblings_without_rereading_at_creation(monkeypatch):
    """Actual COMPOSITE preparation uses cycle tracking and creates distinct occurrences."""
    monkeypatch.setattr(strategies, 'ALGORITHMS', {})
    strategies.register_algorithm('COMPOSITE', AlgorithmDefinition(
        prepare_composite_source, CompositeStrategy))
    strategies.register_algorithm('LEAF', AlgorithmDefinition(
        IndependentStrategy.prepare_source, IndependentStrategy))
    catalog = {
        'root': CompositeConfig('root', (
            CompositeComponent('COMPOSITE', 'branch', Decimal('0.5')),
            CompositeComponent('COMPOSITE', 'branch', Decimal('0.5'))),
                component_drift_limit=Decimal('0.20')),
        'branch': CompositeConfig('branch', (
            CompositeComponent('LEAF', 'quote:uid', Decimal('1')),),
                component_drift_limit=Decimal('0.20')),
    }
    with patch('autorepeater.composite_strategy.select_composite_config',
               side_effect=catalog.__getitem__) as select:
        prepared = strategies.prepare_strategy('COMPOSITE', 'root')
        assert select.call_args_list == [call('root'), call('branch'), call('branch')]
    monkeypatch.setattr(strategies, 'ALGORITHMS', {})
    catalog.clear()
    with patch('builtins.open', side_effect=AssertionError('creation I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('creation I/O')):
        strategy = create_strategy(prepared)
    first, second = strategy.children
    assert first is not second
    assert first.children[0] is not second.children[0]
    assert first.children[0].source == second.children[0].source == IndependentSource('uid')


def test_composite_preparation_errors_propagate_before_factories():
    """A failed child leaves every preceding saved factory uncalled."""
    config = CompositeConfig('root', (
        CompositeComponent('LEAF', 'ok', Decimal('0.5')),
        CompositeComponent('LEAF', 'bad', Decimal('0.5'))), component_drift_limit=Decimal('0.20'))
    factory = Mock()
    context = create_autospec(PreparationContext, instance=True, spec_set=True)
    error = ValueError('bad child source')
    context.prepare.side_effect = [PreparedStrategy(factory, None, 'ok'), error]
    with patch('autorepeater.composite_strategy.select_composite_config', return_value=config):
        with pytest.raises(ValueError) as caught:
            prepare_composite_source('root', context)
    assert caught.value is error
    assert context.prepare.call_args_list == [call('LEAF', 'ok'), call('LEAF', 'bad')]
    factory.assert_not_called()


def test_snapshots_are_fresh_ordered_and_opaque():
    """The composite passes each child's snapshot back without inspecting it."""
    children = [mock_child(), mock_child()]
    strategy = make_composite(children, ['0.4', '0.2'])
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    timeline = Mock()
    for index, child in enumerate(children):
        timeline.attach_mock(child, f'child{index}')
    first, second = strategy.load_snapshot(data), strategy.load_snapshot(data)
    assert first is not second
    assert first.children[0] is not second.children[0]
    assert timeline.mock_calls == [
        call.child0.load_snapshot(data), call.child1.load_snapshot(data)] * 2
    timeline.reset_mock()
    with patch('builtins.open', side_effect=AssertionError('build I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('build I/O')):
        pure_plan_target(strategy, first, Decimal('100'))
    calls = [item for item in timeline.mock_calls if item[0].endswith('build_plan')]
    assert [item.args[0] for item in calls] == list(first.children)
    assert [item.args[1].budget for item in calls] == [Decimal(40), Decimal(20)]
    assert data.mock_calls == []


def test_nested_allocations_and_leaf_reserves_leave_unallocated_cash():
    """Each level multiplies full budgets; reserves are applied once by actual leaves."""
    account, data = contract_case('ACCOUNT')
    index, _ = contract_case('INDEX')
    branch = make_composite([account, index], ['0.4', '0.2'], name='branch')
    leaf = IndependentStrategy(IndependentSource('uid'))
    root = make_composite([branch, leaf], ['0.5', '0.25'], algorithms=['COMPOSITE', 'LEAF'])
    snapshot = root.load_snapshot(data)
    data.reset_mock()
    with patch.object(account, 'build_target', wraps=account.build_target) as account_build, \
            patch.object(index, 'build_target', wraps=index.build_target) as index_build, \
            patch.object(leaf, 'build_target', wraps=leaf.build_target) as leaf_build:
        target = pure_plan_target(root, snapshot, Decimal('100'))
    account_build.assert_called_once_with(snapshot.children[0].children[0], Decimal('20'))
    index_build.assert_called_once_with(snapshot.children[0].children[1], Decimal('10'))
    leaf_build.assert_called_once_with(snapshot.children[1], Decimal('25'))
    assert target == TargetPortfolio({'uid': Decimal('25.15')}, {'uid': Decimal('2')})
    assert target.quantities['uid'] * target.prices['uid'] == Decimal('50.30')
    assert data.mock_calls == []


def test_uid_sums_fractional_zero_and_prices_only_from_contributors():
    """Keep independent contributions and ignore spare estimates in financial plans."""
    targets = [
        TargetPortfolio({'uid': Decimal('1.25'), 'zero': Decimal(0)},
                        {'uid': Decimal(2), 'zero': Decimal(0), 'unused': Decimal(999)}),
        TargetPortfolio({'uid': Decimal('.5')}, {'uid': Decimal(3), 'zero': Decimal(999)}),
    ]
    originals = deepcopy(targets)
    strategy = make_composite([mock_child(target) for target in targets])
    snapshot = strategy.load_snapshot(create_autospec(StrategyData, instance=True, spec_set=True))
    result = pure_plan_target(strategy, snapshot, Decimal(100))
    assert result == TargetPortfolio({'uid': Decimal('1.75'), 'zero': Decimal(0)},
                                     {'uid': Decimal(3), 'zero': Decimal(0)})
    validate_target(result)
    result.quantities['uid'] = Decimal(99)
    assert targets == originals


@pytest.mark.parametrize('budget', [None, True, 1, 1.0, '1', Decimal('NaN'), Decimal('sNaN'),
                                    Decimal('Infinity'), Decimal('-Infinity'),
                                    Decimal('0'), Decimal('-1')])
def test_invalid_composite_budget_never_calls_child_build(budget):
    """Reject invalid full budgets before allocating anything to children."""
    child = mock_child()
    strategy = make_composite([child])
    snapshot = strategy.load_snapshot(create_autospec(StrategyData, instance=True, spec_set=True))
    with pytest.raises(ValueError, match='budget'):
        strategy.build_plan(
            snapshot, StrategyContext((), budget, Decimal(0), False, (), {'uid': Decimal(2)}))
    child.build_plan.assert_not_called()


@pytest.mark.parametrize('empty_index', [0, 1, 2])
@pytest.mark.parametrize('nested', [False, True])
def test_empty_child_skips_whole_target_after_building_every_child(empty_index, nested):
    """Empty leaves at any level abort composition with their full source path."""
    children = [mock_child() for _ in range(3)]
    original = children[empty_index].build_plan.side_effect
    children[empty_index].build_plan.side_effect = lambda snapshot, context: __import__(
        'dataclasses').replace(original(snapshot, context), target=TargetPortfolio({}, {}))
    strategy = make_composite(children, name='branch')
    expected = f'COMPOSITE/branch -> LEAF/{empty_index}: empty target'
    if nested:
        sibling = mock_child()
        strategy = make_composite([sibling, strategy], ['0.5', '0.5'],
                                  algorithms=['LEAF', 'COMPOSITE'], sources=['ok', 'branch'])
        expected = 'COMPOSITE/root -> ' + expected
    snapshot = strategy.load_snapshot(create_autospec(StrategyData, instance=True, spec_set=True))
    with pytest.raises(ValueError, match='positive quantity'):
        pure_plan_target(strategy, snapshot, Decimal('100'))
    assert children[empty_index].build_plan.called


@pytest.mark.parametrize('bad_target, message', [
    (TargetPortfolio({'bad': Decimal('1')}, {}), 'price for UID: bad'),
    (TargetPortfolio({'bad': Decimal('NaN')}, {'bad': Decimal('1')}), 'quantity for UID: bad'),
    (TargetPortfolio({'bad': Decimal('1')}, {'bad': Decimal('Infinity')}), 'price for UID: bad'),
    (TargetPortfolio({}, []), 'prices'),
])
def test_late_invalid_target_is_not_hidden_by_early_empty(bad_target, message):
    """All child target validation precedes the empty-target decision."""
    early = mock_child()
    late = mock_child(bad_target)
    strategy = make_composite([early, late])
    snapshot = strategy.load_snapshot(create_autospec(StrategyData, instance=True, spec_set=True))
    with pytest.raises(ValueError, match=message):
        pure_plan_target(strategy, snapshot, Decimal('100'))
    assert early.build_plan.call_count == late.build_plan.call_count == 1


@pytest.mark.parametrize('phase', ['load_snapshot', 'build_plan'])
@pytest.mark.parametrize('error', [DataAccessError('read failed'), ValueError('bad data'),
                                   RuntimeError('programming error')])
def test_child_errors_propagate_without_empty_fallback(phase, error):
    """Transport, data and programming failures retain identity and cause."""
    early, late = mock_child(), mock_child()
    strategy = make_composite([early, late])
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    snapshot = strategy.load_snapshot(data)
    getattr(late, phase).side_effect = error
    with pytest.raises(type(error)) as caught:
        if phase == 'load_snapshot':
            strategy.load_snapshot(data)
        else:
            pure_plan_target(strategy, snapshot, Decimal('100'))
    assert caught.value is error
    assert data.mock_calls == []


def test_event_accounts_union_order_and_all_child_decisions():
    """Nested children share one ordered subscription; an early True never short circuits."""
    children = [mock_child() for _ in range(3)]
    declarations = [('src1', 'dst'), ('src2', 'dst'), ('src1', 'src3', 'dst')]
    for child, accounts in zip(children, declarations):
        child.event_accounts.return_value = accounts
    children[0].should_rebalance.return_value = True
    branch = make_composite(children[:2], name='branch')
    strategy = make_composite([branch, children[2]])
    event = PositionEvent(False, '', (), (), 'ping')
    with patch('builtins.open', side_effect=AssertionError('event I/O')):
        assert strategy.event_accounts('dst') == ('src1', 'dst', 'src2', 'src3')
        assert strategy.should_rebalance(event, 'dst') is True
    for child in children:
        child.event_accounts.assert_called_once_with('dst')
        child.should_rebalance.assert_called_once_with(event, 'dst')


@pytest.mark.parametrize('accounts', [None, [], ['dst'], (), ('',), ('bad id',),
                                      (1,), (True,), ('dst', 'dst'), ('dst', [])])
def test_invalid_child_accounts_are_checked_before_union(accounts):
    """Duplicates, bad types and empty children cannot disappear through flattening."""
    early, late = mock_child(), mock_child()
    late.event_accounts.return_value = accounts
    strategy = make_composite([early, late])
    with pytest.raises(ValueError, match='event_accounts'):
        strategy.event_accounts('dst')
    early.event_accounts.assert_called_once_with('dst')
    late.event_accounts.assert_called_once_with('dst')


@pytest.mark.parametrize('decision', [None, 0, 1, 'true', [], Decimal('1')])
def test_late_nonbool_decision_is_not_hidden_by_early_true(decision):
    """Validate each child's strict bool even after another child requests a rebalance."""
    children = [mock_child() for _ in range(3)]
    children[0].should_rebalance.return_value = True
    children[1].should_rebalance.return_value = decision
    strategy = make_composite(children)
    event = PositionEvent(False, '', (), (), 'ping')
    with pytest.raises(ValueError, match='should_rebalance.*bool'):
        strategy.should_rebalance(event, 'dst')
    for child in children:
        child.should_rebalance.assert_called_once_with(event, 'dst')
