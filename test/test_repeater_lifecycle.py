"""Readiness precedes every financial read and ends with one fresh destination."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from test.test_fx_runtime import fixture_sdk_runtime, launch, money
from test.test_order_plan import destination, leaf, rules
from unittest.mock import Mock, call, create_autospec, patch

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError, schemas as sdk

import handler as cloud
from autorepeater import grpc_deadline, repeater, runner, serverless, tinvest_requests
from autorepeater.execution import OrderExecutor
from autorepeater.execution_data import (
    AccountReadiness, ActiveOrder, ExecutionData, ExecutionDataError,
)
from autorepeater.execution_market import ExecutionMarket
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_contract import Strategy
from autorepeater.strategy_data import PositionEvent, StrategyData
from autorepeater.strategy_plan import AllocationProfile
from autorepeater.tinvest_execution_data import TInvestExecutionData
from scripts.sandbox_retry_report import transport_facts

__all__ = ['fixture_sdk_runtime']

READY = AccountReadiness(False, False, False, 0)
BLOCKED = AccountReadiness(True, False, False, 0)


class FakeClock:
    """Only lifecycle clocks advance; no real sleep or global test hooks."""

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        """Use a nonzero epoch to exercise relative scheduling."""
        return self.now

    def sleep(self, seconds):
        """Record every positive wait, including the final deadline remainder."""
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(name='clock')
def fixture_clock(monkeypatch):
    """Patch only the module under test, including on the initial RED run."""
    clock = FakeClock()
    monkeypatch.setattr(repeater, 'monotonic', clock.monotonic, raising=False)
    monkeypatch.setattr(repeater, 'sleep', clock.sleep, raising=False)
    return clock


@pytest.fixture(name='case')
def fixture_case():
    """Strict ports return explicit ready facts and a completely valid buy plan."""
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    strategy.load_snapshot.return_value = object()
    strategy.allocation_profile.return_value = AllocationProfile({'X': D(1)}, {'X': D(1)}, D(0))
    strategy.build_plan.return_value = leaf()
    execution.get_readiness.return_value = READY
    execution.get_destination.return_value = destination()
    execution.get_trade_rules.return_value = rules('X')
    engine = repeater.AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(True)
    return SimpleNamespace(engine=engine, strategy=strategy, data=data,
                           execution=execution, executor=executor)


def assert_no_planning(case):
    """Probe failures cannot create cache boundaries, budgets, FX or effects."""
    assert not case.data.mock_calls
    assert not case.strategy.mock_calls
    case.execution.get_destination.assert_not_called()
    case.execution.get_trade_rules.assert_not_called()
    assert not case.executor.mock_calls


def test_ready_immediately_reads_destination_before_source(case, clock):
    """The ready path has no delay and retains complete debug plan validation."""
    calls = Mock()
    for name, port in [('execution', case.execution), ('data', case.data),
                       ('strategy', case.strategy)]:
        calls.attach_mock(port, name)
    assert case.engine.sync_accounts('dst') == ()
    assert clock.sleeps == []
    assert calls.mock_calls == [
        call.execution.get_readiness('dst', timeout=30.0),
        call.data.begin_snapshot(), call.execution.get_destination('dst'),
        call.strategy.load_snapshot(case.data),
        call.strategy.allocation_profile(case.strategy.load_snapshot.return_value),
        call.strategy.build_plan(case.strategy.load_snapshot.return_value,
                                 case.strategy.build_plan.call_args.args[1]),
        call.execution.get_trade_rules('dst', ['X']),
    ]
    assert not case.executor.mock_calls


@pytest.mark.parametrize('blocker', [
    BLOCKED, AccountReadiness(False, True, False, 0),
    AccountReadiness(False, False, True, 0), AccountReadiness(False, False, False, 2),
])
def test_wait_does_not_create_valuation_or_plan(case, clock, blocker):
    """All four blockers use the same bounded window, before any market read."""
    market = create_autospec(ExecutionMarket, instance=True, spec_set=True)
    case.engine.market = market
    failure = RuntimeError('stop at first financial read')

    def probe(*_args, timeout):
        assert_no_planning(case)
        assert not market.mock_calls
        assert timeout == 30 - (clock.now - 100)
        return READY if clock.now == 110 else blocker

    case.execution.get_readiness.side_effect = probe
    market.valuation.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        case.engine.sync_accounts('dst')
    assert caught.value is failure
    assert clock.sleeps == [5, 5]
    assert case.execution.get_readiness.call_args_list == [
        call('dst', timeout=value) for value in [30, 25, 20]]
    market.valuation.assert_called_once_with()
    assert not case.strategy.mock_calls


def test_timeout_has_only_validated_blocker_flags(case, clock):
    """Not-ready is a failure, with no invented transport cause or retry status."""
    case.execution.get_readiness.return_value = AccountReadiness(True, True, True, 3)
    with pytest.raises(repeater.AccountNotReadyError) as caught:
        case.engine.sync_accounts('dst')
    assert str(caught.value) == (
        'account not ready within 30 seconds: loading=True portfolio_blocked=True '
        'positions_blocked=True active_order_count=3')
    assert caught.value.__cause__ is None
    assert transport_facts(caught.value)[1] is None
    assert clock.now == 130
    assert clock.sleeps == [5] * 6
    assert case.execution.get_readiness.call_args_list == [
        call('dst', timeout=value) for value in [30, 25, 20, 15, 10, 5]]
    assert_no_planning(case)


@pytest.mark.parametrize('duration,schedule', [
    (7, ([100, 110, 120], [30, 20, 10], [3, 3, 3])),
    (5, ([100, 110, 120], [30, 20, 10], [5, 5, 5])),
    (29, ([100], [30], [1])),
])
def test_slow_probes_skip_missed_slots(case, clock, duration, schedule):
    """A slow RPC never triggers a catch-up queue or a request at the deadline."""
    observed = []
    starts, timeouts, sleeps = schedule

    def probe(*_args, timeout):
        observed.append(clock.now)
        assert timeout > 0
        clock.now += duration
        return BLOCKED

    case.execution.get_readiness.side_effect = probe
    with pytest.raises(repeater.AccountNotReadyError):
        case.engine.sync_accounts('dst')
    assert observed == starts
    assert case.execution.get_readiness.call_args_list == [
        call('dst', timeout=value) for value in timeouts]
    assert clock.sleeps == sleeps
    assert clock.now == 130
    assert_no_planning(case)


@pytest.mark.parametrize('duration,accepted', [(30, True), (30.001, False)])
def test_ready_response_must_arrive_by_deadline(case, clock, duration, accepted):
    """An in-flight response exactly on the boundary is allowed; a late one is not."""
    def probe(*_args, **_kwargs):
        clock.now += duration
        return READY

    case.execution.get_readiness.side_effect = probe
    if accepted:
        assert case.engine.sync_accounts('dst') == ()
    else:
        with pytest.raises(repeater.AccountNotReadyError):
            case.engine.sync_accounts('dst')
        assert_no_planning(case)
    case.execution.get_readiness.assert_called_once_with('dst', timeout=30)
    assert not clock.sleeps


def test_oversleep_does_not_start_another_read(case, clock, monkeypatch):
    """Scheduler delays cannot extend the budget or submit one last RPC."""
    case.execution.get_readiness.return_value = BLOCKED
    monkeypatch.setattr(repeater, 'sleep', lambda _seconds: setattr(clock, 'now', 131))
    with pytest.raises(repeater.AccountNotReadyError):
        case.engine.sync_accounts('dst')
    case.execution.get_readiness.assert_called_once_with('dst', timeout=30)
    assert_no_planning(case)


def test_delay_before_first_read_is_not_a_confirmed_blocker(case, clock, monkeypatch):
    """Descheduling before the first RPC cannot fabricate not-ready broker facts."""
    times = iter((100, 131))
    monkeypatch.setattr(repeater, 'monotonic', lambda: next(times))
    with pytest.raises(ExecutionDataError) as caught:
        case.engine.sync_accounts('dst')
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert transport_facts(caught.value)[1] is None
    case.execution.get_readiness.assert_not_called()
    assert not clock.sleeps
    assert_no_planning(case)


def test_malformed_port_result_is_not_truthy_readiness(case, clock):
    """An arbitrary object is never accepted as a ready DTO."""
    del clock
    case.execution.get_readiness.return_value = object()
    with pytest.raises(ValueError, match='readiness must be AccountReadiness'):
        case.engine.sync_accounts('dst')
    assert_no_planning(case)


@pytest.mark.parametrize('error', [ExecutionDataError('read failed'), ValueError('invalid flags')])
def test_read_errors_are_not_readiness_timeouts(case, clock, error):
    """Transport and malformed responses preserve their error, without another probe."""
    case.execution.get_readiness.side_effect = error
    with pytest.raises(type(error)) as caught:
        case.engine.sync_accounts('dst')
    assert caught.value is error
    assert not clock.sleeps
    assert_no_planning(case)


@pytest.mark.parametrize('change', [
    {'limits_ready': False},
    {'active_orders': (ActiveOrder('old', 'X', 'NEW', 1, 0),)},
])
def test_full_snapshot_loses_readiness_without_wait_or_source(case, clock, change):
    """A successful probe cannot authorize planning from an unready financial snapshot."""
    case.execution.get_destination.return_value = replace(destination(), **change)
    with pytest.raises(repeater.AccountNotReadyError) as caught:
        case.engine.sync_accounts('dst')
    assert str(caught.value) == 'account became not ready after preflight'
    assert caught.value.__cause__ is None
    assert transport_facts(caught.value)[1] is None
    assert case.execution.mock_calls == [
        call.get_readiness('dst', timeout=30), call.get_destination('dst')]
    assert case.data.mock_calls == [call.begin_snapshot()]
    assert not case.strategy.mock_calls
    assert not case.executor.mock_calls
    assert not clock.sleeps


def test_financial_read_after_ready_has_no_readiness_budget(case, clock):
    """Full reads keep their existing timeouts even when the probe used most of 30s."""
    case.execution.get_readiness.side_effect = [BLOCKED] * 5 + [READY]

    def fresh(_account_id):
        assert clock.now == 125
        clock.now += 40
        return destination()

    case.execution.get_destination.side_effect = fresh
    assert case.engine.sync_accounts('dst') == ()
    assert clock.now == 165
    case.execution.get_destination.assert_called_once_with('dst')
    case.strategy.load_snapshot.assert_called_once_with(case.data)


@pytest.mark.parametrize('invalid', ['empty', 'cash_floor'])
def test_ready_still_validates_complete_plan_before_rules_or_effects(case, clock, invalid):
    """Debug cannot turn an empty target or invalid money into a successful pass."""
    del clock
    plan = case.strategy.build_plan.return_value
    case.strategy.build_plan.return_value = (
        replace(plan, target=TargetPortfolio({}, {}, 'empty')) if invalid == 'empty'
        else replace(plan, cash_floor=D(101)))
    with pytest.raises(ValueError):
        case.engine.sync_accounts('dst')
    case.execution.get_trade_rules.assert_not_called()
    assert not case.executor.mock_calls


@pytest.mark.parametrize('failure', ['timeout', 'lost', 'local_deadline'])
def test_cli_stops_only_current_pass_and_next_event_rechecks(case, clock, failure):
    """No stream recovery change: a fresh event can start a new independent pass."""
    local = ExecutionDataError('readiness read deadline expired')
    local.__cause__ = TimeoutError('read deadline expired')
    if failure == 'timeout':
        case.execution.get_readiness.side_effect = [BLOCKED] * 6 + [READY]
    elif failure == 'lost':
        case.execution.get_destination.side_effect = [
            replace(destination(), limits_ready=False), destination()]
    else:
        case.execution.get_readiness.side_effect = [local, READY]
        assert transport_facts(local)[1] is None
    case.strategy.event_accounts.return_value = ('dst',)
    case.strategy.should_rebalance.return_value = True
    stop = RuntimeError('end finite test stream')
    case.data.position_events.side_effect = [iter((PositionEvent(False, '', (), (), ''),)), stop]
    with pytest.raises(RuntimeError) as caught:
        case.engine.mainflow('dst')
    assert caught.value is stop
    case.strategy.load_snapshot.assert_called_once_with(case.data)
    assert case.execution.get_readiness.call_count == (7 if failure == 'timeout' else 2)
    assert clock.sleeps == ([5] * 6 if failure == 'timeout' else [])


@pytest.mark.parametrize('entrypoint', ['run_sync', 'handler'])
@pytest.mark.parametrize('failure', ['timeout', 'lost', 'local_deadline'])
def test_finite_entrypoints_propagate_readiness_failures(case, clock, entrypoint, failure):
    """The actual Runner and cloud handler cannot return Success after a failed pass."""
    del clock
    expected = repeater.AccountNotReadyError
    if failure == 'timeout':
        case.execution.get_readiness.return_value = BLOCKED
    elif failure == 'lost':
        case.execution.get_destination.return_value = replace(destination(), limits_ready=False)
    else:
        expected = ExecutionDataError
        error = ExecutionDataError('readiness read deadline expired')
        error.__cause__ = TimeoutError('read deadline expired')
        case.execution.get_readiness.side_effect = error
    prepared = object()
    with patch.object(runner.Runner, '_create_client', autospec=True, spec_set=True), \
            patch.object(runner.Runner, '_create_repeater', autospec=True, spec_set=True,
                         return_value=case.engine), \
            patch.object(runner, 'create_strategy', autospec=True, spec_set=True,
                         return_value=case.strategy), \
            patch.object(serverless, 'prepare_strategy', autospec=True, spec_set=True,
                         return_value=prepared), \
            patch.object(serverless, 'print_launch', autospec=True, spec_set=True):
        with pytest.raises(expected):
            if entrypoint == 'run_sync':
                runner.Runner('synthetic', prepared, 'dst', runner.RunnerParams(False)).run_sync()
            else:
                cloud.handler({'queryStringParameters': {
                    'algoritm': 'ACCOUNT', 'src': 'synthetic', 'dst': 'dst',
                    'token': 'synthetic'}}, None)
    assert not case.strategy.load_snapshot.mock_calls
    assert not case.executor.mock_calls


@pytest.mark.parametrize('destination_currency', ['rub', 'usd'])
def test_destination_first_finalizes_source_discovered_fx(
        sdk_runtime, clock, monkeypatch, destination_currency):
    """Extend the destination's FX reference while retaining every observed quote exactly."""
    del clock
    client, state = sdk_runtime
    if destination_currency == 'usd':
        state.cash = {'rub': D(0), 'cny': D(0), 'usd': D(100)}
        client.instruments.currencies.return_value.instruments.append(replace(
            client.instruments.currencies.return_value.instruments[0],
            uid='usd-rub', iso_currency_name='USD', nominal=money('usd', 1)))
        client.instruments.currency_by.side_effect = lambda **kwargs: sdk.CurrencyResponse(
            instrument=sdk.Currency(uid=kwargs['id'], iso_currency_name=(
                kwargs['id'].removeprefix('cash-').upper())))
    observed = []
    read = TInvestExecutionData.get_destination

    def destination_read(self, account_id, *, valuation=None):
        snapshot = read(self, account_id, valuation=valuation)
        assert snapshot.fx_snapshot is valuation.fx
        observed.append((snapshot, valuation))
        return snapshot

    monkeypatch.setattr(TInvestExecutionData, 'get_destination', destination_read)
    plan = launch(client, debug=True)
    original, value = observed[0]
    assert len(observed) == 1
    assert plan.snapshot.fx_snapshot is value.fx
    assert set(original.fx_snapshot.quotes) == (set() if destination_currency == 'rub' else {'usd'})
    assert set(plan.snapshot.fx_snapshot.quotes) == (
        {'cny'} if destination_currency == 'rub' else {'usd', 'cny'})
    for currency, quote in original.fx_snapshot.quotes.items():
        assert plan.snapshot.fx_snapshot.quotes[currency] is quote
    assert plan.snapshot.budget == (D(1000) if destination_currency == 'rub' else D(1200))
    assert plan.marks == {'C': D(720)}
    assert state.events[:3] == [('portfolio', 'dst'), ('portfolio', 'dst'), ('portfolio', '0004')]
    assert client.operations.get_positions.call_count == 2
    assert client.operations.get_portfolio.call_count == 3
    books = [item.kwargs['instrument_id'] for item in
             client.market_data.get_order_book.call_args_list]
    assert books.count('C') == books.count('cny-rub') == 1
    assert books.count('usd-rub') == int(destination_currency == 'usd')
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('quota_clears', [True, False])
def test_real_adapter_quota_shares_polling_budget_and_scope_ends_on_ready(
        sdk_runtime, clock, monkeypatch, quota_clears):
    """A quota sleep consumes the same 30s and normal financial reads have no scope."""
    client, _state = sdk_runtime
    monkeypatch.setattr(grpc_deadline, 'monotonic', clock.monotonic)
    monkeypatch.setattr(tinvest_requests.time, 'sleep', clock.sleep)
    positions = client.operations.get_positions.side_effect
    quota = RequestError(StatusCode.RESOURCE_EXHAUSTED, 'private details', None)
    scopes = []

    def probe(**kwargs):
        remaining = grpc_deadline.remaining_read_timeout()
        scopes.append(remaining)
        if remaining is not None:
            if (quota_clears and clock.now == 100) or (not quota_clears and clock.now == 125):
                raise quota
            return replace(positions(**kwargs), limits_loading_in_progress=not quota_clears)
        return positions(**kwargs)

    client.operations.get_positions.side_effect = probe
    if quota_clears:
        plan = launch(client, debug=True)
        assert plan.snapshot.limits_ready
        assert scopes == [30, 20, None]
        assert clock.sleeps == [10]
    else:
        with pytest.raises(ExecutionDataError) as caught:
            launch(client, debug=True)
        assert isinstance(caught.value.__cause__, TimeoutError)
        assert caught.value.__cause__.__cause__ is quota
        assert scopes == [30, 25, 20, 15, 10, 5]
        assert clock.sleeps == [5] * 6
        assert clock.now == 130
        assert not client.instruments.mock_calls
        assert not client.market_data.mock_calls
        client.orders.get_max_lots.assert_not_called()
    assert grpc_deadline.remaining_read_timeout() is None
    client.orders.post_order.assert_not_called()
