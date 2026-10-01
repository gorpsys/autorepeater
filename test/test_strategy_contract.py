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
from unittest.mock import Mock, call, create_autospec, patch

import pytest

from autorepeater.account_strategy import AccountStrategy
from autorepeater.account_strategy import PreparedAccountSource
from autorepeater.account_config import AccountConfig
from autorepeater.index_config import IndexConfig, IndexInstrument
from autorepeater.index_strategy import IndexStrategy
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_budget import available_budget
from autorepeater.strategy_contract import AlgorithmDefinition, Strategy, validate_strategy
from autorepeater.strategy_data import (
    DataAccessError, InstrumentInfo, InstrumentMatch, InstrumentType, MoneyBlocking,
    PortfolioEntry, PortfolioSnapshot, PositionEvent, PriceQuote, StrategyData,
)


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
    def prepare_source(src):
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
        'uid', 'ONE', 'One', InstrumentType.SHARE, 'TQBR', 3, 'RUB')
    data.get_last_prices.return_value = [PriceQuote('uid', Decimal('2'), None)]
    data.position_events.return_value = iter((
        PositionEvent(False, '', (), (), 'ping'),
        PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('0')),), 'ready'),
        PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('1')),), 'blocked'),
    ))
    if algoritm == 'ACCOUNT':
        strategy = AccountStrategy(PreparedAccountSource('00123', AccountConfig(Decimal('0.01'))))
    elif algoritm == 'INDEX':
        config = IndexConfig('CONTRACT', Decimal('0.05'), [IndexInstrument(
            'ONE', Decimal('1'), Decimal('1'), Decimal('1'), Decimal('2'),
            Decimal('100'), Decimal('12'))], reserve=Decimal('0.03'))
        strategy = IndexStrategy(config)
    else:
        strategy = IndependentStrategy(IndependentStrategy.prepare_source('quote:uid'))
    return strategy, data


@pytest.fixture(name='case', params=['ACCOUNT', 'INDEX', 'INDEPENDENT'])
def fixture_case(request):
    """Every contract test runs unchanged against all three implementations."""
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
    for method in ('load_snapshot', 'build_target', 'event_accounts', 'should_rebalance'):
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
            patch.object(strategy, 'build_target', wraps=strategy.build_target) as build, \
            patch.object(strategy, 'event_accounts', wraps=strategy.event_accounts) as accounts, \
            patch.object(strategy, 'should_rebalance', wraps=strategy.should_rebalance) as decision:
        assert validate_strategy(strategy) is strategy
        load.assert_not_called()
        build.assert_not_called()
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
        'ACCOUNT': [call.get_portfolio('00123'), call.find_instruments('uid')],
        'INDEX': [call.find_instruments('ONE'), call.get_instrument('uid'),
                  call.get_last_prices(['uid'])],
        'INDEPENDENT': [call.get_last_prices(['uid'])],
    }
    assert data.mock_calls == expected_reads[algoritm] * 2
    data.reset_mock()
    target = strategy.build_target(snapshot, Decimal('60'))
    assert isinstance(target, TargetPortfolio)
    validate_target(target)
    assert target.quantities == {'uid': {
        'ACCOUNT': Decimal('29.70'), 'INDEX': Decimal('27'),
        'INDEPENDENT': Decimal('29.40'),
    }[algoritm]}
    assert target.prices == {'uid': Decimal('2')}
    assert_own_values(target)
    assert data.mock_calls == []


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
    ((MoneyBlocking(Decimal('0')), MoneyBlocking(Decimal('1'))), True, False),
    ((MoneyBlocking(Decimal('1')), MoneyBlocking(Decimal('0'))), False, False),
    ((MoneyBlocking(Decimal('0')), MoneyBlocking(Decimal('0'))), True, True),
])
def test_destination_money_predicates_remain_distinct(money, account_decision, index_decision):
    """ACCOUNT uses the first money blocking; INDEX requires every money to be clear."""
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

    client = Mock(spec_set=['instruments', 'operations', 'orders', 'users'])
    for name, service in [('instruments', services.InstrumentsService),
                          ('operations', services.OperationsService),
                          ('orders', services.OrdersService), ('users', services.UsersService)]:
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(instrument_uid='cash', instrument_type='currency',
                                 current_price=invest.MoneyValue(currency='RUB', units=1, nano=0),
                                 quantity=invest.Quotation(units=100, nano=0))])
    client.instruments.get_instrument_by.return_value = invest.InstrumentResponse(
        instrument=invest.Instrument(
        uid='uid', ticker='ONE', name='One', lot=1,
        trading_status=invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
    client.users.get_accounts.return_value = invest.GetAccountsResponse(accounts=[])
    client.orders.post_order.return_value = invest.PostOrderResponse(order_id='test-order')
    _, data = contract_case('INDEPENDENT')
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
    prepare.assert_called_once_with('quote:uid')
    create.assert_called_once_with(IndependentSource('uid'))
    sdk_client.assert_called_once_with(token='test-token', target=runner_module.INVEST_GRPC_API)
    sdk_client.return_value.__enter__.assert_called_once_with()
    sdk_client.return_value.__exit__.assert_called_once()
    adapter.assert_called_once_with(client)
    reads = [call.get_last_prices(['uid'])]
    assert data.mock_calls == (reads + [call.position_events(('dst',))] + reads
                               + [call.position_events(('dst',))] if streaming else reads)
    count = 2 if streaming else 1
    assert client.operations.get_portfolio.call_args_list == [call(account_id='dst')] * count
    assert client.users.get_accounts.call_args_list == ([call()] if streaming else [])
    assert client.instruments.get_instrument_by.call_args_list == [call(
        id_type=invest.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')] * count
    client.instruments.find_instrument.assert_not_called()
    assert client.orders.post_order.call_args_list == [call(
        instrument_id='uid', quantity=49, direction=invest.OrderDirection.ORDER_DIRECTION_BUY,
        account_id='dst', order_type=invest.OrderType.ORDER_TYPE_BESTPRICE)] * count


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
from test.test_strategy_contract import contract_case, assert_own_values
from autorepeater.strategy_contract import validate_strategy
from autorepeater.portfolio import validate_target
from autorepeater.strategy_data import DataAccessError
from decimal import Decimal

for algorithm in ('ACCOUNT', 'INDEX', 'INDEPENDENT'):
    strategy, data = contract_case(algorithm)
    validate_strategy(strategy)
    assert data.mock_calls == []
    snapshot = strategy.load_snapshot(data)
    assert_own_values(snapshot)
    data.reset_mock()
    target = strategy.build_target(snapshot, Decimal('60'))
    validate_target(target)
    assert target.quantities == {'uid': {
        'ACCOUNT': Decimal('29.70'), 'INDEX': Decimal('27'),
        'INDEPENDENT': Decimal('29.40'),
    }[algorithm]}
    assert target.prices == {'uid': Decimal('2')}
    assert data.mock_calls == []
    from unittest.mock import patch
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
assert reporting.strategy_position_to_string(data, position) == 'One(ONE) - 6.0 - RUB - 12.0'
data.get_portfolio.assert_not_called()
assert not any(name.split('.')[0] in ('t_tech', 'grpc') for name in sys.modules)
print('SDK-independent strategies and reporting ok')
'''
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'SDK-independent strategies and reporting ok\n'
