"""Shared, effect-free preparation of a working or replay planning pass."""
from dataclasses import replace
from decimal import Decimal

from autorepeater.execution_data import ExecutionSnapshot
from autorepeater.strategy_allocation import build_marks, recovered_capital
from autorepeater.strategy_data import InstrumentType, PortfolioEntry
from autorepeater.strategy_plan import (
    ZERO, AllocationProfile, StrategyContext, StrategyPlan, exact_sum, nodes, validate_plan,
)


def _destination_positions(destination: ExecutionSnapshot) -> tuple[PortfolioEntry, ...]:
    """Aggregate compatible duplicates without changing the adapter's nano budget."""
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


def prepare_strategy_context(profile: AllocationProfile, destination: ExecutionSnapshot
                             ) -> tuple[StrategyContext, dict[str, Decimal]]:
    """Build the root context and separate holdings without a strategy exposure."""
    positions = _destination_positions(destination)
    marks = build_marks(positions, (profile,))
    recognized = tuple(entry for entry in positions
                       if profile.exposures.get(entry.uid, ZERO) > 0)
    unassigned = {entry.uid: entry.quantity for entry in positions
                  if profile.exposures.get(entry.uid, ZERO) == 0}
    context = StrategyContext(
        (), destination.budget, recovered_capital(recognized, profile, marks),
        False, recognized, marks)
    return context, unassigned


def _plan_ownership(plan: StrategyPlan) -> dict[tuple[int, ...], dict[str, Decimal]]:
    """Read fixed occurrence holdings directly from the validated tree."""
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


def finalize_strategy_plan(plan: StrategyPlan, context: StrategyContext,
                           unassigned: dict[str, Decimal], destination: ExecutionSnapshot
                           ) -> tuple[StrategyPlan, dict[tuple[int, ...], dict[str, Decimal]],
                                      list[str]]:
    """Validate the original strategy result before preparing physical planning inputs."""
    validate_plan(plan, context)
    plan = replace(plan, unassigned={**plan.unassigned, **unassigned})
    ownership = _plan_ownership(plan)
    uids = sorted(set(destination.quantities) | {
        uid for node in nodes(plan) for uid in node.target.quantities})
    return plan, ownership, uids
