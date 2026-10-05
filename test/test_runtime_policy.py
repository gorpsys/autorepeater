"""Offline integration of the final neutral strategy and execution boundaries."""
from dataclasses import replace
from decimal import Decimal
import logging
from unittest.mock import Mock, create_autospec
from contextlib import nullcontext

import pytest

from autorepeater.execution_data import ExecutionData, ExecutionSnapshot, TradeRules
from autorepeater.execution import ExecutionReceipt, OrderExecutionError
from autorepeater.portfolio import TargetPortfolio
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_contract import Strategy, validate_strategy
from autorepeater.strategy_data import (
    InstrumentType, MoneyBlocking, PortfolioEntry, PortfolioSnapshot, PositionEvent,
    SecurityBlocking, StrategyData,
)
from autorepeater.strategy_plan import AllocationProfile, StrategyDecision, StrategyPlan, TradeMode
from autorepeater.triggers import check_triggers
from autorepeater import runner as runner_module, serverless
import main as cli


@pytest.fixture(name='runtime')
def fixture_runtime():
    """Neutral ports have no SDK attributes or display lookup fallback."""
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = Mock(spec_set=['submit_order'])
    snapshot = ExecutionSnapshot(PortfolioSnapshot(()), Decimal(100), {}, {},
                                 {'rub': Decimal(100)}, {}, (), True)
    execution.get_destination.return_value = snapshot
    execution.get_trade_rules.return_value = {
        'stock': TradeRules(1, 'rub', True, True, Decimal(100), 100, 100)}
    strategy.allocation_profile.return_value = AllocationProfile(
        {'stock': Decimal(1)}, {'stock': Decimal(10)}, Decimal(0))
    def build_plan(_snapshot, context):
        return StrategyPlan(context.path, context.budget,
                            TargetPortfolio({'stock': Decimal(10)}, {'stock': Decimal(10)}),
                            Decimal(0), StrategyDecision(TradeMode.BUY_ONLY, False,
                                                        'initial', None, Decimal(0)), (), {},
                            context.positions)
    strategy.build_plan.side_effect = build_plan
    executor.submit_order.side_effect = lambda _account, intent: ExecutionReceipt(
        intent.uid, intent.side, 'offline', 'FILL', intent.lots, intent.lots, {})
    return strategy, data, execution, executor


@pytest.mark.parametrize('debug', [False, True])
def test_final_api_and_neutral_execution_order(runtime, debug, caplog):
    """One snapshot/profile/plan precedes own limits and submission, also in debug."""
    strategy, data, execution, executor = runtime
    timeline = Mock()
    for name, obj in [('strategy', strategy), ('data', data), ('execution', execution),
                      ('executor', executor)]:
        timeline.attach_mock(obj, name)
    engine = AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(debug)
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        engine.sync_accounts('dst')
    names = [item[0] for item in timeline.mock_calls]
    assert names[:5] == ['strategy.load_snapshot', 'execution.get_destination',
                         'strategy.allocation_profile', 'strategy.build_plan',
                         'execution.get_trade_rules']
    assert executor.submit_order.call_count == (0 if debug else 1)
    data.find_instruments.assert_not_called()
    data.get_instrument.assert_not_called()
    assert any('cash_floor' in message for message in caplog.messages)


@pytest.mark.parametrize('quantities', [{}, {'stock': Decimal(0)}])
@pytest.mark.parametrize('debug', [False, True])
def test_empty_main_is_error_before_rules_or_orders(runtime, quantities, debug, caplog):
    """Empty and all-zero main targets fail even when a surplus could be sold."""
    strategy, data, execution, executor = runtime
    build = strategy.build_plan.side_effect
    strategy.build_plan.side_effect = lambda snapshot, context: replace(
        build(snapshot, context), target=TargetPortfolio(quantities, {'stock': Decimal(10)}))
    engine = AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(debug)
    with caplog.at_level(logging.INFO, logger='tinkoffBot'), pytest.raises(ValueError):
        engine.sync_accounts('dst')
    execution.get_trade_rules.assert_not_called()
    executor.submit_order.assert_not_called()
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.parametrize('account,securities,money,expected', [
    ('dst', (), (), False),
    ('dst', (0,), (), True),
    ('dst', (0, 1), (), False),
    ('dst', (0,), (0, 1), False),
    ('dst', (0, 0), (0, 0), True),
    ('dst', (), (0, 1), False),
    ('src', (), (0,), False),
    ('src', (0,), (1,), True),
    ('src', (0, 1), (0,), False),
    ('other', (0,), (0,), False),
])
def test_account_events_preserve_source_and_check_all_destination_blockers(
        account, securities, money, expected):
    """Destination activity only initiates checking; source's security condition is unchanged."""
    event = PositionEvent(True, account, tuple(SecurityBlocking(value) for value in securities),
                          tuple(MoneyBlocking(Decimal(value)) for value in money), 'offline')
    assert check_triggers(event, 'src', 'dst') is expected
    assert check_triggers(replace(event, has_position=False), 'src', 'dst') is False


def test_reached_main_skips_without_empty_error(runtime, caplog):
    """A positive target with no delta is an ordinary INFO skip."""
    strategy, data, execution, executor = runtime
    entry = PortfolioEntry('stock', InstrumentType.SHARE, 'rub', Decimal(10), Decimal(10), '')
    execution.get_destination.return_value = replace(
        execution.get_destination.return_value, portfolio=PortfolioSnapshot((entry,)),
        quantities={'stock': Decimal(10)}, marks={'stock': Decimal(10)},
        available_cash={'rub': Decimal(0)}, available_quantities={'stock': Decimal(10)})
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    executor.submit_order.assert_not_called()
    assert any('SKIP' in message for message in caplog.messages)
    assert all(record.levelno < logging.ERROR for record in caplog.records)


def test_final_callable_contract_rejects_missing_profile_without_invocation():
    """Preparation rejects incomplete implementations before any Client opens."""
    strategy = Mock(spec_set=['load_snapshot', 'build_plan', 'event_accounts', 'should_rebalance'])
    with pytest.raises(TypeError, match='allocation_profile'):
        validate_strategy(strategy)
    assert not strategy.mock_calls


def test_unknown_execution_stops_only_local_pass(runtime):
    """An unknown result is not retried; subsequent events still initiate fresh passes."""
    strategy, data, execution, executor = runtime
    strategy.event_accounts.return_value = ('dst',)
    strategy.should_rebalance.return_value = True
    data.position_events.side_effect = [iter([object()]), RuntimeError('end offline stream')]
    executor.submit_order.side_effect = [OrderExecutionError('unknown'),
                                       ExecutionReceipt('stock', 'BUY', 'next', 'FILL', 10, 10, {})]
    with pytest.raises(RuntimeError, match='end offline stream'):
        AutoRepeater(strategy, data, execution, executor).mainflow('dst')
    assert executor.submit_order.call_count == 2
    assert execution.get_destination.call_count == 2


@pytest.mark.parametrize('flag', ['-t', '--threshold'])
def test_cli_rejects_removed_common_threshold(monkeypatch, flag):
    """Argparse rejects the removed trading override before preparation or token lookup."""
    monkeypatch.setattr('sys.argv', ['main.py', '--algoritm', 'INDEX', '-s', 'IMOEX', flag, '.01'])
    prepare = Mock()
    monkeypatch.setattr(cli, 'prepare_strategy', prepare)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    prepare.assert_not_called()


def test_cloud_does_not_return_success_after_stopped_execution(monkeypatch):
    """The real entrypoint propagates a neutral stop from the common runner."""
    runner = Mock()
    runner.return_value.run_sync.side_effect = OrderExecutionError('offline stop')
    monkeypatch.setattr(serverless, 'Runner', runner)
    monkeypatch.setattr(serverless, 'configure_yc_logging', Mock())
    with pytest.raises(OrderExecutionError, match='offline stop'):
        serverless.handler({'queryStringParameters': {'token': 'offline', 'dst': 'dst'}}, None)


@pytest.mark.parametrize('method', ['allocation_profile', 'build_plan'])
def test_factory_validation_precedes_client(monkeypatch, method):
    """Neither loading nor policy evaluation is called by constructor validation."""
    from autorepeater.strategy_contract import PreparedStrategy  # pylint: disable=import-outside-toplevel
    strategy = Mock(spec_set=['load_snapshot', 'allocation_profile', 'build_plan',
                              'event_accounts', 'should_rebalance'])
    setattr(strategy, method, None)
    client = Mock()
    monkeypatch.setattr(runner_module, 'Client', client)
    with pytest.raises(TypeError, match=method):
        runner_module.Runner('offline', PreparedStrategy(lambda _: strategy, None, 'test'), 'dst')
    client.assert_not_called()
    assert not strategy.mock_calls


@pytest.mark.parametrize('status', ['NEW', 'PARTIALLYFILL', 'REJECTED', 'CANCELLED', 'UNSPECIFIED'])
def test_failed_first_sale_blocks_all_remaining_orders(runtime, status):
    """Main validates before unassigned liquidation; every sale needs complete confirmation."""
    strategy, data, execution, executor = runtime
    entries = tuple(PortfolioEntry(uid, InstrumentType.SHARE, 'rub', Decimal(10), Decimal(5), '')
                    for uid in ('old', 'other'))
    execution.get_destination.return_value = replace(
        execution.get_destination.return_value, portfolio=PortfolioSnapshot(entries),
        quantities={uid: Decimal(5) for uid in ('old', 'other')},
        marks={uid: Decimal(10) for uid in ('old', 'other')},
        available_quantities={uid: Decimal(5) for uid in ('old', 'other')},
        available_cash={'rub': Decimal(0)})
    rule = execution.get_trade_rules.return_value['stock']
    execution.get_trade_rules.return_value.update(dict.fromkeys(('old', 'other'), rule))
    executor.submit_order.side_effect = lambda _account, intent: ExecutionReceipt(
        intent.uid, intent.side, 'pending', status, intent.lots, 0, {})
    with pytest.raises(OrderExecutionError):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    assert executor.submit_order.call_count == 1
    assert executor.submit_order.call_args.args[1].side == 'SELL'
    execution.get_destination.assert_called_once_with('dst')


def test_confirmed_sale_refreshes_money_before_buy_without_reload(runtime):
    """The source/target stay fixed while availability reflects the actual confirmed sale."""
    strategy, data, execution, executor = runtime
    entry = PortfolioEntry('old', InstrumentType.SHARE, 'rub', Decimal(10), Decimal(5), '')
    initial = replace(execution.get_destination.return_value, portfolio=PortfolioSnapshot((entry,)),
                      quantities={'old': Decimal(5)}, marks={'old': Decimal(10)},
                      available_quantities={'old': Decimal(5)}, available_cash={'rub': Decimal(50)})
    fresh = replace(initial, portfolio=PortfolioSnapshot(()), quantities={}, marks={},
                    available_quantities={}, available_cash={'rub': Decimal(100)})
    execution.get_destination.side_effect = [initial, fresh]
    execution.get_trade_rules.return_value['old'] = execution.get_trade_rules.return_value['stock']
    timeline = Mock()
    timeline.attach_mock(execution, 'reads')
    timeline.attach_mock(executor, 'orders')
    AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    calls = timeline.mock_calls
    sell, buy = [item for item in calls if item[0] == 'orders.submit_order']
    assert (sell.args[1].side, buy.args[1].side) == ('SELL', 'BUY')
    assert calls.index(sell) < 3 < calls.index(buy)
    assert calls[3][0] == 'reads.get_destination'
    strategy.load_snapshot.assert_called_once_with(data)
    assert strategy.build_plan.call_count == strategy.allocation_profile.call_count == 1


@pytest.mark.parametrize('entrypoint', ['cli', 'cloud'])
@pytest.mark.parametrize('empty', [False, True])
def test_entrypoints_use_same_real_engine(runtime, monkeypatch, entrypoint, empty):
    """Real Runner lifecycle passes debug and propagates main errors through both entrypoints."""
    from unittest.mock import patch  # pylint: disable=import-outside-toplevel
    from autorepeater.strategy_contract import PreparedStrategy  # pylint: disable=import-outside-toplevel
    strategy, data, execution, executor = runtime
    if empty:
        original = strategy.build_plan.side_effect
        strategy.build_plan.side_effect = lambda snapshot, context: replace(
            original(snapshot, context), target=TargetPortfolio({}, {}))
    prepared = PreparedStrategy(lambda _: strategy, None, 'offline')
    monkeypatch.setenv('INVEST_TOKEN', 'offline')
    monkeypatch.setattr(cli, 'prepare_strategy', lambda *_: prepared)
    monkeypatch.setattr(serverless, 'prepare_strategy', lambda *_: prepared)
    monkeypatch.setattr(serverless, 'configure_yc_logging', Mock())
    monkeypatch.setattr(runner_module, 'configure_local_logging', Mock())
    monkeypatch.setattr(runner_module, 'print_all_portfolio', Mock())
    engine = AutoRepeater(strategy, data, execution, executor)
    strategy.event_accounts.return_value = ('dst',)
    data.position_events.side_effect = RuntimeError('offline stream end')
    monkeypatch.setattr(runner_module.Runner, '_create_repeater',
                        lambda self, _client: (engine.set_debug(self.params.debug), engine)[1])
    monkeypatch.setattr('sys.argv', ['main.py', '--algoritm', 'OFFLINE', '-s', 'source',
                                   '-d', 'dst', '--debug'])
    expected = (pytest.raises(ValueError) if empty else
                pytest.raises(RuntimeError, match='offline stream end') if entrypoint == 'cli'
                else nullcontext())
    with (expected, patch.object(runner_module, 'Client', autospec=True) as client):
        if entrypoint == 'cli':
            cli.main()
        else:
            assert serverless.handler(
                {'queryStringParameters': {'dst': 'dst'}}, None
            )['body'] == 'Success sync, offline dst!'
    client.return_value.__exit__.assert_called_once()
    assert executor.submit_order.call_count == (0 if empty or entrypoint == 'cli' else 1)


@pytest.mark.parametrize('bad_child', [0, 1])
def test_every_child_main_is_validated_before_unassigned_sale(runtime, bad_child):
    """Invalid returned trees cannot liquidate an otherwise legitimate unrelated holding."""
    strategy, data, execution, executor = runtime
    original = strategy.build_plan.side_effect
    def build(snapshot, context):
        base = original(snapshot, context)
        children = tuple(replace(base, path=(index,), budget=Decimal(50),
                                 target=TargetPortfolio(
                                     {} if index == bad_child else {'stock': Decimal(5)},
                                     {'stock': Decimal(10)}))
                         for index in range(2))
        return replace(base, children=children)
    strategy.build_plan.side_effect = build
    with pytest.raises(ValueError, match='positive quantity'):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    execution.get_trade_rules.assert_not_called()
    executor.submit_order.assert_not_called()


@pytest.mark.parametrize('conflict', [None, 'current_price', 'currency', 'instrument_type'])
def test_duplicate_destination_entries_preserve_budget_or_fail_before_rules(runtime, conflict):
    """Compatible duplicate rows are one holding; conflicting metadata is not guessed."""
    strategy, data, execution, executor = runtime
    entry = PortfolioEntry('stock', InstrumentType.SHARE, 'rub', Decimal(10), Decimal(2), '')
    duplicate = replace(entry, quantity=Decimal(3))
    if conflict:
        duplicate = replace(duplicate, **{conflict: {
            'current_price': Decimal(11), 'currency': 'usd',
            'instrument_type': InstrumentType.ETF}[conflict]})
    execution.get_destination.return_value = replace(
        execution.get_destination.return_value, portfolio=PortfolioSnapshot((entry, duplicate)),
        quantities={'stock': Decimal(5)}, marks={'stock': Decimal(10)})
    engine = AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(True)
    if conflict:
        with pytest.raises(ValueError, match='incompatible destination duplicate UID stock'):
            engine.sync_accounts('dst')
        execution.get_trade_rules.assert_not_called()
        strategy.build_plan.assert_not_called()
    else:
        engine.sync_accounts('dst')
        context = strategy.build_plan.call_args.args[1]
        assert context.positions == (replace(entry, quantity=Decimal(5)),)
        assert context.budget == Decimal(100)
    executor.submit_order.assert_not_called()


@pytest.mark.parametrize('damage', ['overlap', 'empty_reason'])
def test_invalid_plan_ownership_and_empty_reason_fail_before_rules(runtime, damage):
    """Neither inconsistent ownership nor empty diagnostics can enter a physical plan."""
    strategy, data, execution, executor = runtime
    entry = PortfolioEntry('stock', InstrumentType.SHARE, 'rub', Decimal(10), Decimal(2), '')
    execution.get_destination.return_value = replace(
        execution.get_destination.return_value, portfolio=PortfolioSnapshot((entry,)),
        quantities={'stock': Decimal(2)}, marks={'stock': Decimal(10)})
    original = strategy.build_plan.side_effect
    def build(snapshot, context):
        plan = original(snapshot, context)
        if damage == 'overlap':
            return replace(plan, unassigned={'stock': Decimal(1)})
        return replace(plan, target=replace(plan.target, empty_reason='not actually empty'))
    strategy.build_plan.side_effect = build
    with pytest.raises(ValueError, match='overlaps unassigned|empty_reason'):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    execution.get_trade_rules.assert_not_called()
    executor.submit_order.assert_not_called()


def test_strategy_cannot_drop_received_ownership_before_rules(runtime):
    """The fixed holdings in a returned plan must match the received root context."""
    strategy, data, execution, executor = runtime
    entry = PortfolioEntry('stock', InstrumentType.SHARE, 'rub', Decimal(10), Decimal(2), '')
    execution.get_destination.return_value = replace(
        execution.get_destination.return_value, portfolio=PortfolioSnapshot((entry,)),
        quantities={'stock': Decimal(2)}, marks={'stock': Decimal(10)})
    original = strategy.build_plan.side_effect
    strategy.build_plan.side_effect = lambda snapshot, context: replace(
        original(snapshot, context), positions=())
    with pytest.raises(ValueError, match='positions must match context'):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    execution.get_trade_rules.assert_not_called()
    executor.submit_order.assert_not_called()
