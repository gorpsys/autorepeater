"""Neutral orchestration of fresh strategy checks and finite execution passes."""
from dataclasses import replace
from decimal import Decimal
from time import monotonic, sleep

from autorepeater.execution import (
    ExecutionReceipt, OrderExecutionError, OrderExecutor, execute_plan,
)
from autorepeater.execution_data import (
    AccountReadiness, ExecutionData, ExecutionDataError, ExecutionSnapshot,
)
from autorepeater.execution_market import ExecutionMarket
from autorepeater.logging_config import logger
from autorepeater.order_plan import build_order_plan, ready
from autorepeater.purchase_plan import nodes
from autorepeater.strategy_allocation import build_marks, recovered_capital
from autorepeater.strategy_contract import Strategy, validate_event_accounts, validate_strategy
from autorepeater.strategy_data import DataAccessError, InstrumentType, PortfolioEntry, StrategyData
from autorepeater.strategy_plan import StrategyContext, StrategyPlan, exact_sum, validate_plan
from autorepeater.valuation import Valuation
from autorepeater import reporting

READINESS_TIMEOUT = 30
READINESS_RETRY_INTERVAL = 5


class AccountNotReadyError(Exception):
    """Confirmed account blockers stopped this pass, independently of transport errors."""


def plan_ownership(plan: StrategyPlan) -> dict[tuple[int, ...], dict[str, Decimal]]:
    """Read fixed occurrence holdings, never reconstruct them from target weights."""
    result = {}
    for node in nodes(plan):
        if not node.children:
            result[node.path] = {entry.uid: entry.quantity for entry in node.positions}
        if node.unassigned:
            area = result.setdefault(node.path, {})
            for uid, quantity in node.unassigned.items():
                if uid in area:
                    raise ValueError(f'ownership overlaps unassigned UID {uid}')
                area[uid] = quantity
    return result


def destination_positions(destination: ExecutionSnapshot) -> tuple[PortfolioEntry, ...]:
    """Aggregate compatible duplicate DTOs without changing the adapter's nano budget."""
    positions = {}
    for entry in destination.portfolio.positions:
        if entry.instrument_type == InstrumentType.CURRENCY:
            continue
        if entry.uid in positions:
            previous = positions[entry.uid]
            if (entry.current_price, entry.currency, entry.instrument_type) != (
                    previous.current_price, previous.currency, previous.instrument_type):
                raise ValueError(f'incompatible destination duplicate UID {entry.uid}')
            entry = replace(entry, quantity=exact_sum((previous.quantity, entry.quantity)))
        positions[entry.uid] = entry
    return tuple(positions.values())


class AutoRepeater:
    """Check strategy policy from neutral reads, then execute its bounded plan."""

    def __init__(self, strategy: Strategy, data: StrategyData, execution_data: ExecutionData,
                 executor: OrderExecutor, *, market: ExecutionMarket | None = None) -> None:
        self.strategy = validate_strategy(strategy)
        self.data = data
        self.execution_data = execution_data
        self.executor = executor
        self.market = market
        self.debug = False

    def set_debug(self, debug: bool) -> None:
        """Debug still validates the whole financial and physical plan."""
        if not isinstance(debug, bool):
            raise TypeError("Debug flag must be boolean")
        self.debug = debug

    def _build_strategy_plan(self, snapshot, destination):
        profile = self.strategy.allocation_profile(snapshot)
        positions = destination_positions(destination)
        marks = build_marks(positions, (profile,))
        recognized = tuple(entry for entry in positions
                           if profile.exposures.get(entry.uid, Decimal(0)) > 0)
        unassigned = {entry.uid: entry.quantity for entry in positions
                      if profile.exposures.get(entry.uid, Decimal(0)) == 0}
        context = StrategyContext(
            (), destination.budget, recovered_capital(recognized, profile, marks),
            False, recognized, marks)
        plan = self.strategy.build_plan(snapshot, context)
        validate_plan(plan, context)
        return replace(plan, unassigned={**plan.unassigned, **unassigned}), marks

    def _wait_for_readiness(self, dst_account_id: str) -> None:
        started = monotonic()
        deadline = started + READINESS_TIMEOUT
        next_check = started
        last = None
        while (now := monotonic()) < deadline:
            if now < next_check:
                sleep(min(next_check, deadline) - now)
                continue
            last = self.execution_data.get_readiness(dst_account_id, timeout=deadline - now)
            if not isinstance(last, AccountReadiness):
                raise ValueError('readiness must be AccountReadiness')
            now = monotonic()
            if last.ready and now <= deadline:
                return
            # Skip elapsed slots, including one occupied by a slow read.
            next_check = started + ((now - started) // READINESS_RETRY_INTERVAL + 1) * (
                READINESS_RETRY_INTERVAL)
        if last is None:
            raise ExecutionDataError('readiness read deadline expired') from TimeoutError(
                'readiness read deadline expired')
        raise AccountNotReadyError(
            f'account not ready within {READINESS_TIMEOUT} seconds: '
            f'loading={last.loading} portfolio_blocked={last.portfolio_blocked} '
            f'positions_blocked={last.positions_blocked} '
            f'active_order_count={last.active_order_count}')

    def _load_destination(self, dst_account_id: str,
                          valuation: Valuation | None) -> ExecutionSnapshot:
        destination = (self.execution_data.get_destination(dst_account_id)
                       if valuation is None else self.execution_data.get_destination(
                           dst_account_id, valuation=valuation))
        if valuation is not None and destination.fx_snapshot is not valuation.fx:
            raise ValueError('destination must share the pass FX snapshot')
        if not ready(destination):
            raise AccountNotReadyError('account became not ready after preflight')
        return destination

    def sync_accounts(self, dst_account_id: str, *,
                      valuation: Valuation | None = None) -> tuple[ExecutionReceipt, ...]:
        """Validate every main target before reading rules or sending any order."""
        try:
            self._wait_for_readiness(dst_account_id)
            self.data.begin_snapshot()
            if valuation is None and self.market is not None:
                valuation = self.market.valuation()
            if valuation is not None:
                valuation.begin_pass()
            destination = self._load_destination(dst_account_id, valuation)
            data = self.data if valuation is None else valuation.strategy_data(self.data)
            snapshot = self.strategy.load_snapshot(data)
            if valuation is not None:
                # Source discovery extends the same rates without revaluing the destination.
                destination = replace(destination, fx_snapshot=valuation.fx)
            plan, marks = self._build_strategy_plan(snapshot, destination)
            uids = sorted(set(destination.quantities) | {
                uid for node in nodes(plan) for uid in node.target.quantities})
            ownership = plan_ownership(plan)
            rules = self.execution_data.get_trade_rules(dst_account_id, uids)
            funding = (self.market.funding_inputs(
                dst_account_id, valuation, uids, trading=self.market.needs_trading(
                    plan, destination, ownership, marks, rules))
                       if self.market is not None and valuation is not None else None)
            orders = build_order_plan(
                plan, destination, ownership, marks, rules, funding=funding)
        except ValueError as error:
            logger.error('Rebalance rejected for destination %s: %s', dst_account_id, error)
            raise
        reporting.print_rebalance_plan(orders, self.debug)
        if self.debug:
            return ()
        if self.market is not None and valuation is not None:
            return self.market.execute(dst_account_id, orders, self.execution_data,
                                       self.executor, valuation)
        if valuation is not None:
            valuation.validate_fresh()
            if any(rule.currency.lower() != 'rub' for rule in rules.values()):
                raise ValueError('FX financing is not supported until FX execution is integrated')
        return execute_plan(dst_account_id, orders, self.execution_data, self.executor)

    def _local_pass(self, dst: str) -> None:
        try:
            self.sync_accounts(dst)
        except (AccountNotReadyError, DataAccessError, ExecutionDataError,
                OrderExecutionError) as error:
            logger.error('Current pass stopped for destination %s: %s', dst, error)

    def _handle_position_event(self, event, dst: str) -> None:
        triggered = self.strategy.should_rebalance(event, dst)
        if not isinstance(triggered, bool):
            raise ValueError('strategy should_rebalance must return bool')
        if triggered:
            self._local_pass(dst)
        else:
            reporting.print_skipped_strategy_event(event)

    def mainflow(self, dst: str) -> None:
        """Reconcile after stream loss; healthy subscriptions remain event-driven."""
        self._local_pass(dst)
        delay = 1
        while True:
            accounts = validate_event_accounts(self.strategy.event_accounts(dst))
            try:
                events = iter(self.data.position_events(accounts))
            except DataAccessError:
                logger.error('Position stream stopped for destination %s', dst)
            else:
                subscribed = monotonic()
                while True:
                    try:
                        event = next(events)
                    except StopIteration:
                        logger.warning('Position stream ended for destination %s', dst)
                        break
                    except DataAccessError:
                        logger.error('Position stream stopped for destination %s', dst)
                        break
                    if event.has_position and monotonic() - subscribed >= 60:
                        delay = 1
                    self._handle_position_event(event, dst)
            logger.warning('Position stream reconnect for destination %s in %s seconds', dst, delay)
            sleep(delay)
            self._local_pass(dst)
            delay = min(delay * 2, 30)
