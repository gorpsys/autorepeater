"""Neutral orchestration of fresh strategy checks and finite execution passes."""
from dataclasses import replace
from decimal import Decimal

from autorepeater.execution import OrderExecutionError, execute_plan
from autorepeater.execution_data import ExecutionDataError
from autorepeater.logging_config import logger
from autorepeater.order_plan import build_order_plan
from autorepeater.purchase_plan import nodes
from autorepeater.strategy_allocation import build_marks, recovered_capital
from autorepeater.strategy_contract import validate_event_accounts, validate_strategy
from autorepeater.strategy_data import DataAccessError, InstrumentType
from autorepeater.strategy_plan import StrategyContext, exact_sum, validate_plan
from autorepeater import reporting


def plan_ownership(plan):
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


def destination_positions(destination):
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

    def __init__(self, strategy, data, execution_data, executor):
        self.strategy = validate_strategy(strategy)
        self.data = data
        self.execution_data = execution_data
        self.executor = executor
        self.debug = False

    def set_debug(self, debug):
        """Debug still validates the whole financial and physical plan."""
        if not isinstance(debug, bool):
            raise TypeError("Debug flag must be boolean")
        self.debug = debug

    def sync_accounts(self, dst_account_id):
        """Validate every main target before reading rules or sending any order."""
        try:
            self.data.begin_snapshot()
            snapshot = self.strategy.load_snapshot(self.data)
            destination = self.execution_data.get_destination(dst_account_id)
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
            plan = replace(plan, unassigned={**plan.unassigned, **unassigned})
            uids = sorted(set(destination.quantities) | {
                uid for node in nodes(plan) for uid in node.target.quantities})
            ownership = plan_ownership(plan)
            orders = build_order_plan(
                plan, destination, ownership, marks,
                self.execution_data.get_trade_rules(dst_account_id, uids))
        except ValueError as error:
            logger.error('Rebalance rejected for destination %s: %s', dst_account_id, error)
            raise
        reporting.print_rebalance_plan(orders, self.debug)
        if self.debug:
            return ()
        return execute_plan(dst_account_id, orders, self.execution_data, self.executor)

    def _local_pass(self, dst):
        try:
            self.sync_accounts(dst)
        except (DataAccessError, ExecutionDataError, OrderExecutionError) as error:
            logger.error('Current pass stopped for destination %s: %s', dst, error)

    def mainflow(self, dst):
        """Initial pass and events only; a submission stop does not end the stream."""
        self._local_pass(dst)
        while True:
            try:
                accounts = validate_event_accounts(self.strategy.event_accounts(dst))
                for event in self.data.position_events(accounts):
                    triggered = self.strategy.should_rebalance(event, dst)
                    if not isinstance(triggered, bool):
                        raise ValueError('strategy should_rebalance must return bool')
                    if triggered:
                        self._local_pass(dst)
                    else:
                        reporting.print_skipped_strategy_event(event)
            except DataAccessError as error:
                logger.error('Position stream stopped for destination %s: %s', dst, error)
