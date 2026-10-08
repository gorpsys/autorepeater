"""Generic recursive preparation independent of concrete algorithms."""
import inspect
import json
from unittest.mock import Mock, call, create_autospec, patch

import pytest

from autorepeater import strategies, strategy_contract
from autorepeater import index_config
from autorepeater.account_strategy import prepare_account_source
from autorepeater.composite_strategy import CompositeStrategy, prepare_composite_source
from autorepeater.index_strategy import prepare_index_source
from autorepeater.strategy_contract import (
    AlgorithmDefinition, PreparedStrategy, Strategy, UnsupportedSourceError,
)


class TreePreparation:  # pylint: disable=too-few-public-methods
    """A test-only preparer depending solely on the neutral context interface."""

    def __init__(self, nodes):
        self.nodes = nodes

    def prepare(self, src, context: 'strategy_contract.PreparationContext'):
        """Save ordered children without constructing any strategies."""
        if src not in self.nodes:
            raise UnsupportedSourceError(f'unknown tree source: {src}')
        return tuple(context.prepare(algoritm, source)
                     for algoritm, source in self.nodes[src])


def create_tree(children):
    """Construct children using only saved definitions and the neutral helper."""
    strategy = Mock(spec_set=[
        'children', 'load_snapshot', 'allocation_profile', 'build_plan',
        'event_accounts', 'should_rebalance'])
    strategy.children = tuple(strategy_contract.create_strategy(child) for child in children)
    return strategy


@pytest.fixture(name='tree')
def fixture_tree(monkeypatch):
    """A private registry with an arbitrary leaf and independently prepared tree."""
    monkeypatch.setattr(strategies, 'ALGORITHMS', dict(strategies.ALGORITHMS))
    nodes = {}
    prepare_tree = Mock(wraps=TreePreparation(nodes).prepare)
    factory_tree = Mock(side_effect=create_tree)
    prepare_leaf = Mock(side_effect=lambda src, context: {'source': src})
    factory_leaf = Mock(side_effect=lambda source: create_autospec(
        Strategy, instance=True, spec_set=True))
    strategies.register_algorithm('TREE', AlgorithmDefinition(prepare_tree, factory_tree))
    strategies.register_algorithm('LEAF', AlgorithmDefinition(prepare_leaf, factory_leaf))
    return nodes, prepare_tree, factory_tree, prepare_leaf, factory_leaf


def test_preparation_signatures_are_neutral_and_explicit():
    """All preparers receive context; the application keeps its two arguments."""
    assert issubclass(strategy_contract.PreparationContext, strategy_contract.Protocol)
    assert list(inspect.signature(strategy_contract.PreparationContext.prepare).parameters) == [
        'self', 'algoritm', 'src']
    assert list(inspect.signature(strategies.prepare_strategy).parameters) == ['algoritm', 'src']
    for prepare in (prepare_account_source, prepare_index_source, prepare_composite_source):
        assert list(inspect.signature(prepare).parameters) == ['src', 'context']
    assert strategies.ALGORITHMS['COMPOSITE'] == AlgorithmDefinition(
        prepare_composite_source, CompositeStrategy)


def test_public_create_strategy_reexports_neutral_helper():
    """Public imports expose the same factory helper used by nested strategies."""
    assert strategies.create_strategy is strategy_contract.create_strategy


def test_nested_sibling_reuse_retains_every_factory(tree, monkeypatch):
    """Each occurrence is prepared and created separately, with stable factory selection."""
    # The shared source is opaque; its tuple shape is checked below at runtime.
    # pylint: disable=unsubscriptable-object
    nodes, prepare_tree, factory_tree, prepare_leaf, factory_leaf = tree
    nodes.update(root=(('TREE', 'branch'), ('TREE', 'branch'), ('LEAF', 'root')),
                 branch=(('LEAF', '00123'), ('LEAF', '00123')))
    prepared = strategies.prepare_strategy('TREE', 'root')
    context = prepare_tree.call_args_list[0].args[1]
    assert prepare_tree.call_args_list == [
        call('root', context), call('branch', context), call('branch', context)]
    assert prepare_leaf.call_args_list == [call('00123', context)] * 4 + [call('root', context)]
    factory_tree.assert_not_called()
    factory_leaf.assert_not_called()
    branches = prepared.prepared_source
    assert isinstance(branches, tuple)
    assert branches[0] is not branches[1]
    assert branches[0].prepared_source[0] is not branches[0].prepared_source[1]
    monkeypatch.setattr(strategies, 'ALGORITHMS', {})
    with patch('builtins.open', side_effect=AssertionError('creation I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('creation I/O')):
        strategy = strategies.create_strategy(prepared)
    assert factory_tree.call_args_list == [
        call(branches), call(branches[0].prepared_source), call(branches[1].prepared_source)]
    assert factory_leaf.call_args_list == (
        [call({'source': '00123'})] * 4 + [call({'source': 'root'})])
    leaves = [child for branch in strategy.children[:2] for child in branch.children]
    assert len({id(child) for child in leaves + [strategy.children[2]]}) == 5
    assert prepare_tree.call_count == 3
    assert prepare_leaf.call_count == 5


@pytest.mark.parametrize('nodes, chain', [
    ({'root': (('TREE', 'root'),)}, 'TREE/root -> TREE/root'),
    ({'root': (('TREE', 'branch'),), 'branch': (('TREE', 'root'),)},
     'TREE/root -> TREE/branch -> TREE/root'),
    ({'root': (('TREE', 'branch'),), 'branch': (('OTHER', 'leaf'),)},
     'TREE/root -> TREE/branch -> OTHER/leaf -> TREE/branch'),
])
def test_cycles_report_full_active_path_before_construction(tree, monkeypatch, nodes, chain):
    """Direct and indirect cycles fail before any factory or Client."""
    from autorepeater import runner  # pylint: disable=import-outside-toplevel

    configured, _, factory_tree, _, factory_leaf = tree
    configured.update(nodes)
    factory_other = Mock()
    monkeypatch.setitem(strategies.ALGORITHMS, 'OTHER', AlgorithmDefinition(
        lambda src, context: context.prepare('TREE', 'branch'), factory_other))
    with patch.object(runner, 'Client') as client:
        with pytest.raises(ValueError) as error:
            runner.Runner('synthetic-token', strategies.prepare_strategy('TREE', 'root'), 'dst')
    assert 'cycle' in str(error.value)
    assert chain in str(error.value)
    factory_tree.assert_not_called()
    factory_leaf.assert_not_called()
    factory_other.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize('child, message', [
    (('UNKNOWN', 'leaf'), 'unsupported algoritm: UNKNOWN'),
    (('', 'leaf'), 'algoritm is required'),
    (('LEAF', ''), 'src is required'),
    (('TREE', 'missing'), 'unknown tree source: missing'),
])
def test_bad_nested_source_aborts_before_any_factory(tree, child, message):
    """Even a valid preceding sibling stays unconstructed when preparation fails."""
    from autorepeater import runner  # pylint: disable=import-outside-toplevel

    nodes, _, factory_tree, _, factory_leaf = tree
    nodes['root'] = (('LEAF', 'good'), child)
    with patch.object(runner, 'Client') as client:
        with pytest.raises(ValueError) as error:
            runner.Runner('synthetic-token', strategies.prepare_strategy('TREE', 'root'), 'dst')
    assert str(error.value) == message
    factory_tree.assert_not_called()
    factory_leaf.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize('failure', ['cycle', 'source'])
def test_context_pops_failed_nested_paths_in_finally(tree, failure):
    """A preparer can recover and reuse the same path after a child failure."""
    # pylint: disable=no-member
    nodes, prepare_tree, factory_tree, _, factory_leaf = tree
    nodes['branch'] = (('TREE', 'branch' if failure == 'cycle' else 'missing'),)

    def recover(src, context):
        with pytest.raises(ValueError):
            context.prepare('TREE', src)
        nodes['branch'] = (('LEAF', 'recovered'),)
        return context.prepare('TREE', src)

    strategies.register_algorithm('RECOVER', AlgorithmDefinition(recover, Mock()))
    prepared = strategies.prepare_strategy('RECOVER', 'branch')
    branch = prepared.prepared_source
    assert isinstance(branch, PreparedStrategy)
    children = branch.prepared_source
    assert isinstance(children, tuple)
    assert children[0].prepared_source == {'source': 'recovered'}
    context = prepare_tree.call_args_list[0].args[1]
    assert prepare_tree.call_args_list == ([call('branch', context)] + (
        [call('missing', context)] if failure == 'source' else []) + [call('branch', context)])
    factory_tree.assert_not_called()
    factory_leaf.assert_not_called()


def test_each_public_preparation_has_its_own_context(tree):
    """A failed launch cannot retain active paths or share a context with the next one."""
    nodes, prepare_tree, _, _, _ = tree
    nodes['root'] = (('TREE', 'root'),)
    with pytest.raises(ValueError, match='cycle'):
        strategies.prepare_strategy('TREE', 'root')
    first_context = prepare_tree.call_args_list[0].args[1]
    nodes['root'] = ()
    strategies.prepare_strategy('TREE', 'root')
    assert prepare_tree.call_args_list[-1].args[1] is not first_context


@pytest.mark.parametrize('invalid', [None, 'source', object()])
def test_neutral_creation_requires_prepared_strategy(invalid):
    """Neutral and assembly creation share the explicit prepared-input contract."""
    with pytest.raises(TypeError, match='create_strategy requires PreparedStrategy'):
        strategy_contract.create_strategy(invalid)


def test_neutral_creation_validates_every_nested_factory_before_client(tree, monkeypatch):
    """The shared helper validates child results before Runner can open Client."""
    from autorepeater import runner  # pylint: disable=import-outside-toplevel

    nodes, _, factory_tree, _, _ = tree
    nodes['root'] = (('BROKEN', 'child'),)
    factory = Mock(return_value=object())
    monkeypatch.setitem(strategies.ALGORITHMS, 'BROKEN', AlgorithmDefinition(
        lambda src, context: src, factory))
    prepared = strategies.prepare_strategy('TREE', 'root')
    factory_tree.assert_not_called()
    factory.assert_not_called()
    with patch.object(runner, 'Client') as client:
        with pytest.raises(TypeError, match='strategy load_snapshot must be callable'):
            runner.Runner('synthetic-token', prepared, 'dst')
    factory.assert_called_once_with('child')
    client.assert_not_called()


def test_nested_index_reuse_reads_each_occurrence_only_during_preparation(tree, tmp_path,
                                                                          monkeypatch):
    """INDEX keeps a read pass per occurrence; creation retains files and factories."""
    nodes, _, factory_tree, _, _ = tree
    nodes['root'] = (('INDEX', 'ONLY'), ('INDEX', 'ONLY'))
    path = tmp_path / 'only.json'
    payload = {
        'allocation_drift_limits': [
            {'budget_from': '0', 'budget_to': None,
             'upper_inclusive': False, 'limit': '0'}],
        'name': 'ONLY', 'max_lot_weight_error': '0.05', 'reserve': '0.01',
        'instruments': [{
            'ticker': 'ONE', 'effective_quantity': '1', 'free_float': '1',
            'weight_limit': '1', 'reference_price': '10',
            'reference_weight': '100', 'reference_index_capitalization': '10',
        }],
    }
    path.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))
    monkeypatch.delenv('IMOEX_CONFIG_PATH', raising=False)
    definition = strategies.ALGORITHMS['INDEX']
    factory_index = Mock(wraps=definition.create)
    monkeypatch.setitem(strategies.ALGORITHMS, 'INDEX', AlgorithmDefinition(
        definition.prepare_source, factory_index))
    with patch.object(index_config, 'read_index_document',
                      wraps=index_config.read_index_document) as reads:
        prepared = strategies.prepare_strategy('TREE', 'root')
    assert reads.call_args_list == [call(path)] * 2
    factory_tree.assert_not_called()
    factory_index.assert_not_called()
    path.write_text('{', encoding='utf-8')
    monkeypatch.delitem(strategies.ALGORITHMS, 'INDEX')
    with patch('builtins.open', side_effect=AssertionError('creation I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('creation I/O')):
        strategy = strategy_contract.create_strategy(prepared)
    first, second = strategy.children
    assert first is not second
    assert first.config == second.config
    assert first.config is not second.config
    assert factory_index.call_args_list == [call(first.config), call(second.config)]
