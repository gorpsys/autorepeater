# pylint: disable=import-outside-toplevel
"""Shared contracts over own data, plus independent algorithm launch coverage."""
import inspect
import subprocess
import sys
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, create_autospec, patch

import pytest

from autorepeater import strategy_contract
from autorepeater.index_config import AllocationDriftRange
from autorepeater.account_strategy import AccountStrategy
from autorepeater.account_strategy import PreparedAccountSource
from autorepeater.account_config import AccountConfig
from autorepeater.composite_strategy import (
    CompositeStrategy, PreparedCompositeComponent, PreparedCompositeSource,
)
from autorepeater.index_config import IndexConfig, IndexInstrument
from autorepeater.index_strategy import IndexStrategy
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_budget import available_budget
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, StrategyPlan, StrategyDecision, TradeMode,
)
from autorepeater.strategy_contract import (
    AlgorithmDefinition, PreparationContext, PreparedStrategy, Strategy, create_strategy,
    validate_strategy,
)
from autorepeater.strategy_data import (
    DataAccessError, InstrumentInfo, InstrumentMatch, InstrumentType, MoneyBlocking,
    PortfolioEntry, PortfolioSnapshot, PositionEvent, PriceQuote, StrategyData,
)


@pytest.mark.parametrize('accounts, message', [
    (None, 'must return a nonempty tuple'),
    ('dst', 'must return a nonempty tuple'),
    (['dst'], 'must return a nonempty tuple'),
    ((), 'must return a nonempty tuple'),
    (('',), 'must contain account strings'),
    ((' ',), 'must contain account strings'),
    (('dst ',), 'must contain account strings'),
    (('a\tb',), 'must contain account strings'),
    (('a\nb',), 'must contain account strings'),
    (('a\u00a0b',), 'must contain account strings'),
    ((None,), 'must contain account strings'),
    ((5,), 'must contain account strings'),
    ((True,), 'must contain account strings'),
    (('dst', []), 'must contain account strings'),
    (('dst', 'dst'), 'must not contain duplicates'),
])
def test_validate_event_accounts_preserves_exact_errors(accounts, message):
    """The neutral validator retains declaration, ID and duplicate diagnostics."""
    with pytest.raises(ValueError) as error:
        strategy_contract.validate_event_accounts(accounts)
    assert str(error.value) == 'strategy event_accounts ' + message


@pytest.mark.parametrize('accounts', [('dst',), ('00123', 'dst', 'other')])
def test_validate_event_accounts_preserves_order_and_identity(accounts):
    """Validation keeps opaque IDs and returns the original ordered declaration."""
    assert strategy_contract.validate_event_accounts(accounts) is accounts


@dataclass(frozen=True)
class IndependentSource:
    """The third algorithm interprets its source as a quote UID, not an account."""
    uid: str


@dataclass(frozen=True)
class IndependentSnapshot:
    """Private snapshot unrelated to account positions or index constituents."""
    uid: str
    unit_price: Decimal


class IndependentStrategy:
    """A test algorithm with its own preparation, snapshot and target calculation."""

    def __init__(self, source):
        self.source = source
        self.reserve = Decimal('0.02')

    @staticmethod
    def prepare_source(src, context):  # pylint: disable=unused-argument
        """Only the algorithm defines the quote: source syntax."""
        if not isinstance(src, str) or not src.startswith('quote:') or not src[6:]:
            raise ValueError('expected quote:<uid>')
        return IndependentSource(src[6:])

    def load_snapshot(self, data):
        """Read only the requested quote through the common port."""
        quote, = data.get_last_prices([self.source.uid])
        return IndependentSnapshot(quote.uid, quote.price)

    def build_target(self, snapshot, budget):
        """Keep fractional units without any external data lookup."""
        budget = available_budget(budget, self.reserve)
        return TargetPortfolio({snapshot.uid: budget / snapshot.unit_price},
                               {snapshot.uid: snapshot.unit_price})

    def allocation_profile(self, snapshot):
        """Pure ideal composition from the same quote."""
        return AllocationProfile({snapshot.uid: Decimal(1) - self.reserve},
                                 {snapshot.uid: snapshot.unit_price}, self.reserve)

    def build_plan(self, snapshot, context):
        """Explicit financial permissions without requiring engine special cases."""
        return StrategyPlan(context.path, context.budget,
                            self.build_target(snapshot, context.budget),
                            context.budget * self.reserve,
                            StrategyDecision(TradeMode.BUY_ONLY, False, 'independent', None, None),
                            (), {}, context.positions)

    def event_accounts(self, dst_account_id):
        """Declare the destination without reading data."""
        return (dst_account_id,)

    def should_rebalance(self, event, dst_account_id):
        """Decide only from the supplied event."""
        return (event.has_position and event.account_id == dst_account_id
                and bool(event.money)
                and all(item.blocked_value == 0 for item in event.money))


def contract_case(algoritm):
    """Build real strategies with a strict own-port autospec and explicit own DTOs."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((
        PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('2'),
                       Decimal('6'), 'share'),
        PortfolioEntry('cash', InstrumentType.CURRENCY, 'RUB', Decimal('1'),
                       Decimal('100'), 'cash'),
    ))
    data.find_instruments.return_value = [
        InstrumentMatch('uid', 'ONE', 'One', InstrumentType.SHARE, 'TQBR')]
    data.get_instrument.return_value = InstrumentInfo(
        'uid', 'ONE', 'One', InstrumentType.SHARE, 'TQBR', 3, 'RUB', True)
    data.get_last_prices.return_value = [PriceQuote('uid', Decimal('2'), None)]
    data.position_events.return_value = iter((
        PositionEvent(False, '', (), (), 'ping'),
        PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('0')),), 'ready'),
        PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('1')),), 'blocked'),
    ))
    if algoritm == 'ACCOUNT':
        strategy = AccountStrategy(PreparedAccountSource('00123', AccountConfig(
            '00123', '00123', Decimal('0.01'), Decimal('0.0092'))))
    elif algoritm == 'INDEX':
        config = IndexConfig('CONTRACT', Decimal('0.05'), [IndexInstrument(
            'ONE', Decimal('1'), Decimal('1'), Decimal('1'), Decimal('2'),
            Decimal('100'), Decimal('12'))], reserve=Decimal('0.03'),
                allocation_drift_limits=(
                    AllocationDriftRange(Decimal('0'), None, False, Decimal('0')),))
        strategy = IndexStrategy(config)
    elif algoritm == 'COMPOSITE':
        source = PreparedCompositeSource('CONTRACT', tuple(
            PreparedCompositeComponent('INDEPENDENT', 'quote:uid', Decimal('0.5'),
                                       PreparedStrategy(IndependentStrategy,
                                                        IndependentSource('uid'), 'quote:uid'))
            for _ in range(2)), component_drift_limit=Decimal('0.20'))
        strategy = CompositeStrategy(source)
    else:
        context = create_autospec(PreparationContext, instance=True, spec_set=True)
        source = IndependentStrategy.prepare_source('quote:uid', context)
        context.prepare.assert_not_called()
        strategy = create_strategy(PreparedStrategy(IndependentStrategy, source, 'quote:uid'))
    return strategy, data


@pytest.fixture(name='case', params=['ACCOUNT', 'INDEX', 'INDEPENDENT', 'COMPOSITE'])
def fixture_case(request):
    """Every contract test runs unchanged against all implementations."""
    return request.param, *contract_case(request.param)


def assert_own_values(value):
    """Recursively reject opaque objects, SDK DTOs/enums and hidden clients."""
    if is_dataclass(value):
        assert type(value).__module__.startswith(('autorepeater.', 'test.'))
        for field in fields(value):
            assert_own_values(getattr(value, field.name))
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_own_values(key)
            assert_own_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_own_values(item)
    elif isinstance(value, Enum):
        assert isinstance(value, InstrumentType)
    else:
        assert type(value) in (str, int, bool, Decimal, datetime, type(None))


def test_signatures_and_runtime_surface(case):
    """All implementations match Protocol parameter names, kinds and defaults."""
    _, strategy, _ = case
    for method in ('load_snapshot', 'allocation_profile', 'build_plan',
                   'event_accounts', 'should_rebalance'):
        actual = inspect.signature(getattr(type(strategy), method)).parameters.values()
        expected = inspect.signature(getattr(Strategy, method)).parameters.values()
        assert [(item.name, item.kind, item.default) for item in actual] == [
            (item.name, item.kind, item.default) for item in expected]
    assert not hasattr(strategy, 'default_reserve')
    assert not hasattr(strategy, 'events')
    assert validate_strategy(strategy) is strategy


def test_validation_never_invokes_strategy_methods(case):
    """Contract validation neither loads a snapshot nor opens or consumes a stream."""
    _, strategy, data = case
    with patch.object(strategy, 'load_snapshot', wraps=strategy.load_snapshot) as load, \
            patch.object(strategy, 'build_plan', wraps=strategy.build_plan) as build, \
            patch.object(strategy, 'allocation_profile',
                         wraps=strategy.allocation_profile) as profile, \
            patch.object(strategy, 'event_accounts', wraps=strategy.event_accounts) as accounts, \
            patch.object(strategy, 'should_rebalance', wraps=strategy.should_rebalance) as decision:
        assert validate_strategy(strategy) is strategy
        load.assert_not_called()
        build.assert_not_called()
        profile.assert_not_called()
        accounts.assert_not_called()
        decision.assert_not_called()
    assert data.mock_calls == []


def test_reserve_is_private_to_strategy(case):
    """The structural contract does not require a public financial setting."""
    _, strategy, data = case
    assert validate_strategy(strategy) is strategy
    assert data.mock_calls == []


def test_snapshots_and_pure_targets_use_only_own_data(case):
    """Snapshots are fresh and opaque; target construction does not read the port."""
    algoritm, strategy, data = case
    snapshot = strategy.load_snapshot(data)
    next_snapshot = strategy.load_snapshot(data)
    assert snapshot is not next_snapshot
    assert_own_values(snapshot)
    expected_reads = {
        'ACCOUNT': [call.get_portfolio('00123')],
        'INDEX': [call.find_instruments('ONE'), call.get_instrument('uid'),
                  call.get_last_prices(['uid'])],
        'INDEPENDENT': [call.get_last_prices(['uid'])],
        'COMPOSITE': [call.get_last_prices(['uid'])] * 2,
    }
    assert data.mock_calls == expected_reads[algoritm] * 2
    data.reset_mock()
    target = pure_plan_target(strategy, snapshot, Decimal('60'))
    assert isinstance(target, TargetPortfolio)
    validate_target(target)
    assert target.quantities == {'uid': {
        'ACCOUNT': Decimal('29.70'), 'INDEX': Decimal('27'),
        'INDEPENDENT': Decimal('29.40'),
        'COMPOSITE': Decimal('29.40'),
    }[algoritm]}
    assert target.prices == {'uid': Decimal('2')}
    assert_own_values(target)
    assert data.mock_calls == []


def pure_plan_target(strategy, snapshot, budget):
    """A first-fill context uses only the profile's already loaded marks."""
    profile = strategy.allocation_profile(snapshot)
    return strategy.build_plan(snapshot, StrategyContext(
        (), budget, Decimal(0), False, (), profile.prices)).target


def test_event_methods_are_pure_and_return_exact_accounts_and_bools(case, caplog):
    """Declarations and predicates never read data, open streams or log skips."""
    algoritm, strategy, data = case
    caplog.set_level(20, logger=LOGGER_NAME)
    source_events = tuple(data.position_events.return_value)
    with patch('builtins.open', side_effect=AssertionError('event I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('event I/O')):
        assert strategy.event_accounts('dst') == (
            ('00123', 'dst') if algoritm == 'ACCOUNT' else ('dst',))
        assert strategy.event_accounts('00123') == ('00123',)
        for event, expected in zip(source_events, (False, True, False)):
            assert strategy.should_rebalance(event, 'dst') is expected
    assert data.mock_calls == []
    assert caplog.records == []


@pytest.mark.parametrize('money, account_decision, index_decision', [
    ((MoneyBlocking(Decimal('0')), MoneyBlocking(Decimal('1'))), False, False),
    ((MoneyBlocking(Decimal('1')), MoneyBlocking(Decimal('0'))), False, False),
    ((MoneyBlocking(Decimal('0')), MoneyBlocking(Decimal('0'))), True, True),
])
def test_destination_money_predicates_remain_distinct(money, account_decision, index_decision):
    """Both algorithms require every destination money blocking to be clear."""
    account, account_data = contract_case('ACCOUNT')
    index, index_data = contract_case('INDEX')
    event = PositionEvent(True, 'dst', (), money, 'money')
    assert account.should_rebalance(event, 'dst') is account_decision
    assert index.should_rebalance(event, 'dst') is index_decision
    assert account_data.mock_calls == index_data.mock_calls == []


def test_port_failures_remain_own_errors(case):
    """No SDK exception is required for snapshot transport failures."""
    algoritm, strategy, data = case
    error = DataAccessError('port unavailable')
    read = data.get_portfolio if algoritm == 'ACCOUNT' else data.get_last_prices
    read.side_effect = error
    with pytest.raises(DataAccessError) as caught:
        strategy.load_snapshot(data)
    assert caught.value is error
    assert type(caught.value).__module__ == 'autorepeater.strategy_data'
    assert caught.value.args == ('port unavailable',)
    assert caught.value.__cause__ is None


class EndOfTestStream(Exception):
    """Terminate only the finite test scenario, never a production subscription."""


@pytest.fixture(name='launch')
def fixture_launch(monkeypatch):
    """Use the actual engine with autospec SDK execution and a separate own data port."""
    from t_tech import invest
    from t_tech.invest import services
    from autorepeater import runner as runner_module, strategies

    client = Mock(spec_set=['instruments', 'operations', 'orders', 'users', 'market_data'])
    for name, service in [('instruments', services.InstrumentsService),
                          ('operations', services.OperationsService),
                          ('orders', services.OrdersService), ('users', services.UsersService),
                          ('market_data', services.MarketDataService)]:
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(instrument_uid='cash', instrument_type='currency',
                                 current_price=invest.MoneyValue(currency='rub', units=1, nano=0),
                                 quantity=invest.Quotation(units=100, nano=0),
                                 blocked=False, blocked_lots=invest.Quotation(0, 0))])
    client.instruments.get_instrument_by.return_value = invest.InstrumentResponse(
        instrument=invest.Instrument(
        uid='uid', ticker='ONE', name='One', lot=1, currency='RUB',
        api_trade_available_flag=True,
        trading_status=invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
    client.users.get_accounts.return_value = invest.GetAccountsResponse(accounts=[])
    client.orders.post_order.return_value = invest.PostOrderResponse(order_id='test-order')
    data = contract_case('INDEPENDENT')[1]
    configure_strategy_port(client, data)
    configure_filled_sdk(client)
    data.position_events.side_effect = [
        iter((PositionEvent(False, '', (), (), 'ping'),
              PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('0')),), 'ready'))),
        EndOfTestStream(),
    ]
    monkeypatch.setattr(strategies, 'ALGORITHMS', dict(strategies.ALGORITHMS))
    prepare = Mock(wraps=IndependentStrategy.prepare_source)
    create = Mock(wraps=IndependentStrategy)
    strategies.register_algorithm('INDEPENDENT', AlgorithmDefinition(prepare, create))
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'TInvestStrategyData', return_value=data) as adapter, \
            patch.object(runner_module, 'configure_local_logging'), \
            patch('autorepeater.serverless.configure_yc_logging'):
        sdk_client.return_value.__enter__.return_value = client
        yield client, data, prepare, create, sdk_client, adapter


def configure_strategy_port(client, data):
    """Source fixtures stay independent; destination reads use the real DTO adapter."""
    from autorepeater.tinvest_strategy_data import TInvestStrategyData

    source_portfolio = data.get_portfolio.return_value
    destination_data = TInvestStrategyData(client)
    data.get_portfolio.side_effect = lambda account_id: (
        destination_data.get_portfolio(account_id) if account_id == 'dst' else source_portfolio)
    data.get_instrument.side_effect = lambda uid: InstrumentInfo(
        uid, 'ONE', 'One', InstrumentType.SHARE, 'TQBR', 3 if uid == 'uid' else 1, 'rub', True)


def configure_filled_sdk(client):
    """Offline SDK holdings and cash change only after a completely filled order."""
    from t_tech import invest

    def cash_position():
        return next(item for item in client.operations.get_portfolio.return_value.positions
                    if item.instrument_type == 'currency')
    def cash():
        item = cash_position()
        return item.quantity.units + Decimal(item.quantity.nano) / 1_000_000_000
    client.operations.get_positions.side_effect = lambda **_: invest.PositionsResponse(
        money=[invest.MoneyValue('rub', int(cash()), int((cash() % 1) * 1_000_000_000))],
        blocked=[], securities=[], futures=[], options=[], limits_loading_in_progress=False)
    client.orders.get_orders.return_value = invest.GetOrdersResponse(orders=[])
    client.market_data.get_trading_status.side_effect = lambda instrument_id: (
        invest.GetTradingStatusResponse(instrument_uid=instrument_id,
                                       api_trade_available_flag=True,
                                       bestprice_order_available_flag=True))
    client.orders.get_max_lots.side_effect = lambda request: invest.GetMaxLotsResponse(
        currency='rub', buy_limits=SimpleNamespace(
            buy_money_amount=invest.Quotation(int(cash()), int((cash() % 1) * 1_000_000_000)),
            buy_max_lots=1000000), sell_limits=SimpleNamespace(sell_max_lots=1000000))
    def fill(**params):
        uid = params['instrument_id']
        lots = params['quantity']
        amount = lots * (3 if uid == 'uid' else 1)
        price = 10 if uid.startswith('uid-') else 2
        direction = 1 if params['direction'] == invest.OrderDirection.ORDER_DIRECTION_BUY else -1
        positions = client.operations.get_portfolio.return_value.positions
        held = next((item for item in positions if item.instrument_uid == uid), None)
        if held is None:
            held = invest.PortfolioPosition(instrument_uid=uid, instrument_type='share',
                                           current_price=invest.MoneyValue('rub', price, 0),
                                           quantity=invest.Quotation(0, 0), blocked=False,
                                           blocked_lots=invest.Quotation(0, 0))
            positions.append(held)
        held.quantity.units += direction * amount
        cash_position().quantity.units -= direction * amount * price
        return invest.PostOrderResponse(instrument_uid=uid, direction=params['direction'],
                                       order_id='offline', lots_requested=lots, lots_executed=lots,
                                       execution_report_status=invest.OrderExecutionReportStatus.
                                       EXECUTION_REPORT_STATUS_FILL)
    client.orders.post_order.side_effect = fill


@pytest.mark.parametrize('invalid_config', ['missing', 'malformed', 'conflicting_paths'])
@pytest.mark.parametrize('entrypoint', ['run_sync', 'run', 'cli', 'query', 'environment'])
def test_independent_algorithm_launches(entrypoint, invalid_config, launch, monkeypatch, tmp_path):
    """An independently registered algorithm uses every real launch path despite INDEX errors."""
    from autorepeater import runner as runner_module, strategies
    import handler as cloud
    import main as cli

    monkeypatch.delenv('IMOEX_CONFIG_PATH', raising=False)
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path / 'missing' if invalid_config == 'missing'
                                           else tmp_path))
    if invalid_config == 'malformed':
        (tmp_path / 'broken.json').write_text('{broken', encoding='utf-8')
    elif invalid_config == 'conflicting_paths':
        monkeypatch.setenv('IMOEX_CONFIG_PATH', str(tmp_path / 'missing.json'))
    for name, value in [('INVEST_TOKEN', 'test-token'), ('DST_ACCOUNT', 'dst'),
                        ('ALGORITM', 'INDEPENDENT'), ('SRC_ACCOUNT', 'quote:uid')]:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', 'INDEPENDENT',
                                    '-s', 'quote:uid', '-d', 'dst'])
    streaming = entrypoint in ('run', 'cli')

    def invoke():
        if entrypoint == 'cli':
            return cli.main()
        if entrypoint in ('run', 'run_sync'):
            runner = runner_module.Runner(
                'test-token', strategies.prepare_strategy('INDEPENDENT', 'quote:uid'), 'dst')
            return getattr(runner, entrypoint)()
        query = ({'algoritm': 'INDEPENDENT', 'src': 'quote:uid',
                  'dst': 'dst', 'token': 'test-token'}
                 if entrypoint == 'query' else {})
        return cloud.handler({'queryStringParameters': query}, None)

    with patch('autorepeater.index_config._index_paths', side_effect=AssertionError(
            'independent algorithms must not inspect INDEX paths')) as paths:
        if streaming:
            with pytest.raises(EndOfTestStream):
                invoke()
        else:
            result = invoke()
            if entrypoint in ('query', 'environment'):
                assert result == {
                    'statusCode': 200, 'headers': {'Content-Type': 'text/plain'},
                    'isBase64Encoded': False, 'body': 'Success sync, quote:uid dst!',
                }
        paths.assert_not_called()
    assert_launch_execution(launch, streaming)


def assert_launch_execution(launch, streaming):
    """Verify the unchanged Runner and engine respect the independent strategy's contract."""
    from autorepeater import runner as runner_module
    from t_tech import invest

    client, data, prepare, create, sdk_client, adapter = launch
    prepare.assert_called_once_with('quote:uid', ANY)
    create.assert_called_once_with(IndependentSource('uid'))
    sdk_client.assert_called_once_with(token='test-token', target=runner_module.INVEST_GRPC_API,
                                       interceptors=[ANY])
    sdk_client.return_value.__enter__.assert_called_once_with()
    sdk_client.return_value.__exit__.assert_called_once()
    adapter.assert_called_once_with(client)
    count = 2 if streaming else 1
    assert data.get_last_prices.call_args_list == [call(['uid'])] * count
    assert data.position_events.call_args_list == ([call(('dst',))] * 2 if streaming else [])
    assert client.users.get_accounts.call_args_list == ([call()] if streaming else [])
    client.instruments.find_instrument.assert_not_called()
    client.orders.post_order.assert_called_once_with(
        instrument_id='uid', quantity=16, direction=invest.OrderDirection.ORDER_DIRECTION_BUY,
        account_id='dst', order_type=invest.OrderType.ORDER_TYPE_BESTPRICE, order_id=ANY)
    assert client.operations.get_positions.call_count >= count
    assert client.orders.get_orders.call_count == client.operations.get_positions.call_count
    assert all(item.kwargs['request'].account_id == 'dst'
               for item in client.orders.get_max_lots.call_args_list)


def test_strategies_execute_in_sdk_blocked_subprocess():
    """Execute snapshots, targets, events and reporting with all SDK imports blocked."""
    script = r'''
import importlib.abc
import sys

class NoSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('t_tech', 'grpc'):
            raise AssertionError('SDK import blocked: ' + fullname)
        return None

assert not any(name.split('.')[0] in ('t_tech', 'grpc') for name in sys.modules)
sys.meta_path.insert(0, NoSDK())
from autorepeater import reporting
from test.test_strategy_contract import contract_case, assert_own_values, pure_plan_target
from autorepeater.strategy_contract import validate_strategy
from autorepeater.portfolio import validate_target
from autorepeater.strategy_data import DataAccessError
from decimal import Decimal
from autorepeater import strategies
assert set(strategies.ALGORITHMS) == {'ACCOUNT', 'INDEX', 'COMPOSITE'}
from autorepeater.strategy_contract import AlgorithmDefinition, create_strategy
from test.test_strategy_contract import IndependentStrategy
from test.test_strategy_preparation import TreePreparation, create_tree
from unittest.mock import patch

nodes = {'root': (('TREE', 'branch'), ('TREE', 'branch')),
         'branch': (('INDEPENDENT', 'quote:uid'),)}
strategies.register_algorithm('INDEPENDENT', AlgorithmDefinition(
    IndependentStrategy.prepare_source, IndependentStrategy))
strategies.register_algorithm('TREE', AlgorithmDefinition(TreePreparation(nodes).prepare, create_tree))
with patch('builtins.open', side_effect=AssertionError('tree I/O')), \
        patch('pathlib.Path.open', side_effect=AssertionError('tree I/O')):
    prepared = strategies.prepare_strategy('TREE', 'root')
    tree = create_strategy(prepared)
    first, second = tree.children
    assert first is not second
    assert first.children[0] is not second.children[0]
    assert first.children[0].source.uid == second.children[0].source.uid == 'uid'
    nodes['branch'] = (('TREE', 'root'),)
    try:
        strategies.prepare_strategy('TREE', 'root')
    except ValueError as error:
        assert 'TREE/root -> TREE/branch -> TREE/root' in str(error)
    else:
        raise AssertionError('missing cycle failure')

for algorithm in ('ACCOUNT', 'INDEX', 'INDEPENDENT', 'COMPOSITE'):
    strategy, data = contract_case(algorithm)
    validate_strategy(strategy)
    assert data.mock_calls == []
    snapshot = strategy.load_snapshot(data)
    assert_own_values(snapshot)
    data.reset_mock()
    target = pure_plan_target(strategy, snapshot, Decimal('60'))
    validate_target(target)
    assert target.quantities == {'uid': {
        'ACCOUNT': Decimal('29.70'), 'INDEX': Decimal('27'),
        'INDEPENDENT': Decimal('29.40'),
        'COMPOSITE': Decimal('29.40'),
    }[algorithm]}
    assert target.prices == {'uid': Decimal('2')}
    assert data.mock_calls == []
    events = tuple(data.position_events.return_value)
    with patch('builtins.open', side_effect=AssertionError('event I/O')), \
            patch('pathlib.Path.open', side_effect=AssertionError('event I/O')):
        assert strategy.event_accounts('dst') == (
            ('00123', 'dst') if algorithm == 'ACCOUNT' else ('dst',))
        assert [strategy.should_rebalance(event, 'dst') for event in events] == [False, True, False]
    assert data.mock_calls == []
    data.get_portfolio.side_effect = DataAccessError('read failed')
    data.get_last_prices.side_effect = DataAccessError('read failed')
    try:
        strategy.load_snapshot(data)
    except DataAccessError as error:
        assert type(error) is DataAccessError
        assert error.args == ('read failed',)
    else:
        raise AssertionError('missing read failure')

_, data = contract_case('ACCOUNT')
position = data.get_portfolio.return_value.positions[0]
assert reporting.strategy_position_to_string(position) == 'uid - 6.0 - RUB - 12.0'
data.get_portfolio.assert_not_called()
assert not any(name.split('.')[0] in ('t_tech', 'grpc') for name in sys.modules)
print('SDK-independent strategies and reporting ok')
'''
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'SDK-independent strategies and reporting ok\n'
