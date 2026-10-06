"""Pure financial permissions and budgets, isolated from the working runtime."""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_allocation import (
    PositionAttribution, attribute_positions, component_weights,
    proportional_split, recovered_capital,
)
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, StrategyDecision, TradeMode, exact_product, exact_sum,
    finite_decimal, validate_context, validate_map, validate_positions,
)
from autorepeater.strategy_data import PortfolioEntry


@dataclass(frozen=True)
class ComponentAllocation:
    """Ordered child inputs and the parent's separate redistribution permission."""
    children: tuple[StrategyContext, ...]
    decision: StrategyDecision
    unassigned: dict[str, Decimal]
    unallocated_budget: Decimal


def _limit(value: Decimal) -> None:
    finite_decimal(value, 'policy limit')
    if value >= 1:
        raise ValueError('policy limit must be less than 1')


def _normalized(values: Sequence[Decimal]) -> tuple[Decimal, ...] | None:
    total = exact_sum(values)
    return proportional_split(Decimal(1), values) if total > 0 else None


def allocation_drift(positions: tuple[PortfolioEntry, ...], control: TargetPortfolio,
                     marks: dict[str, Decimal]) -> Decimal | None:
    """Half-L1 security weights, or None when invested/control value is zero."""
    validate_positions(positions, marks)
    if not isinstance(control, TargetPortfolio):
        raise ValueError('control: expected TargetPortfolio')
    validate_target(control)
    validate_map(control.quantities, 'control quantities')
    current = {entry.uid: entry.quantity for entry in positions}
    uids = sorted(current.keys() | control.quantities.keys())
    for uid in uids:
        if uid not in marks:
            raise ValueError(f'marks: missing UID {uid}')
        finite_decimal(marks[uid], f'marks[{uid}]',
                       positive=current.get(uid, Decimal(0)) > 0
                       or control.quantities.get(uid, Decimal(0)) > 0)
    actual = _normalized([exact_product(current.get(uid, Decimal(0)), marks[uid]) for uid in uids])
    target = _normalized([exact_product(control.quantities.get(uid, Decimal(0)), marks[uid])
                          for uid in uids])
    if actual is None or target is None:
        return None
    differences = [exact_sum((left, right.copy_negate())).copy_abs()
                   for left, right in zip(actual, target)]
    return exact_product(exact_sum(differences), Decimal('.5'))


def leaf_decision(context: StrategyContext, control: TargetPortfolio,
                  limit: Decimal) -> StrategyDecision:
    """The explicit capital obligation precedes the independent internal criterion."""
    validate_context(context)
    _limit(limit)
    metric = allocation_drift(context.positions, control, context.marks)
    if context.budget_reduction_requires_rebalance:
        mode, reason = TradeMode.REBALANCE, 'capital_reduction'
    elif metric is not None and metric > limit:
        mode, reason = TradeMode.REBALANCE, 'allocation_drift'
    else:
        mode = TradeMode.BUY_ONLY
        reason = 'control_unavailable' if metric is None else 'within_allocation_limit'
    return StrategyDecision(mode, False, reason, metric, limit)


def component_drift(capitals: tuple[Decimal, ...], weights: tuple[Decimal, ...]) -> Decimal | None:
    """Maximum relative share error; zero current capital has no share metric."""
    if (not isinstance(capitals, tuple) or not isinstance(weights, tuple)
            or not capitals or len(capitals) != len(weights)):
        raise ValueError('component metric requires matching nonempty tuples')
    for capital in capitals:
        finite_decimal(capital, 'component capital')
    for weight in weights:
        finite_decimal(weight, 'component weight', positive=True)
    if sum(weights, Decimal(0)) > 1:
        raise ValueError('component weight sum must not exceed 1')
    actual = _normalized(capitals)
    target = _normalized(weights)
    if actual is None:
        return None
    return max(exact_sum((left, right.copy_negate())).copy_abs() / right
               for left, right in zip(actual, target))


def _target_budgets(budget: Decimal, weights: tuple[Decimal, ...],
                    pool: Decimal) -> tuple[Decimal, ...]:
    """Correct only the residual of budget * weight products in the target branch."""
    receiver = max(range(len(weights)), key=weights.__getitem__)
    precision = 28
    while True:
        with localcontext() as decimal_context:
            decimal_context.prec = precision
            parts = [budget * weight for weight in weights]
        others = exact_sum(part for index, part in enumerate(parts) if index != receiver)
        parts[receiver] = exact_sum((pool, others.copy_negate()))
        if all(0 <= part <= pool for part in parts):
            return tuple(parts)
        precision *= 2


def _budgets(budget: Decimal, weights: tuple[Decimal, ...], capitals: tuple[Decimal, ...],
              above_limit: bool) -> tuple[tuple[Decimal, ...], Decimal, str]:
    """Apply the agreed priority and conserve the exact child pool after correction."""
    pool = exact_product(budget, exact_sum(weights))
    targets = _target_budgets(budget, weights, pool)
    total = exact_sum(capitals)
    if total == 0 or above_limit:
        return targets, pool, 'initial_allocation' if total == 0 else 'component_drift'
    if pool < total:
        return proportional_split(pool, capitals), pool, 'pool_reduction'
    surplus = exact_sum((pool, total.copy_negate()))
    if surplus == 0:
        return capitals, pool, 'preserve_capital'
    deficits = tuple(max(Decimal(0), exact_sum((target, capital.copy_negate())))
                     for target, capital in zip(targets, capitals))
    additions = proportional_split(surplus, deficits, ceilings=deficits)
    budgets = tuple(exact_sum((capital, addition))
                    for capital, addition in zip(capitals, additions))
    return budgets, pool, 'fund_deficits'


def _capital_shortfall(pool: Decimal, capitals: tuple[Decimal, ...],
                        profiles: tuple[AllocationProfile, ...]) -> bool:
    """Only leaf reserves may explain a reserve-only shortage of the pool."""
    reserves = exact_sum(exact_product(capital, profile.reserve_fraction)
                         for capital, profile in zip(capitals, profiles))
    capital_without_reserve = exact_sum((exact_sum(capitals), reserves.copy_negate()))
    return pool < capital_without_reserve


def _child_contexts(context: StrategyContext, attributed: PositionAttribution,
                     capitals: tuple[Decimal, ...], budgets: tuple[Decimal, ...],
                     force: bool) -> tuple[StrategyContext, ...]:
    children = tuple(StrategyContext(context.path + (index,), budget, capital,
                                     budget < capital and force, positions, context.marks)
                     for index, (budget, capital, positions)
                     in enumerate(zip(budgets, capitals, attributed.children)))
    for child in children:
        validate_context(child)
    return children


def allocate_component_budgets(context: StrategyContext, profiles: tuple[AllocationProfile, ...],
                               weights: tuple[Decimal, ...], limit: Decimal) -> ComponentAllocation:
    """Attribute holdings, restore capital and propagate only justified reductions."""
    validate_context(context)
    _limit(limit)
    weights = component_weights(profiles, weights)
    attributed = attribute_positions(context.positions, profiles, weights,
                                     context.marks, context.path)
    capitals = tuple(recovered_capital(positions, profile, context.marks)
                     for positions, profile in zip(attributed.children, profiles))
    metric = component_drift(capitals, weights)
    above_limit = metric is not None and metric > limit
    budgets, pool, reason = _budgets(context.budget, weights, capitals, above_limit)
    capital_shortfall = _capital_shortfall(pool, capitals, profiles)
    force = context.budget_reduction_requires_rebalance or above_limit or capital_shortfall
    children = _child_contexts(context, attributed, capitals, budgets, force)
    if exact_sum(budgets) != pool:
        raise ValueError('component budgets must conserve the child pool')
    if context.budget_reduction_requires_rebalance or capital_shortfall:
        reason = 'capital_reduction'
    elif reason == 'pool_reduction':
        reason = 'reserve_shortfall'
    mode = TradeMode.REBALANCE if force else TradeMode.BUY_ONLY
    return ComponentAllocation(children, StrategyDecision(mode, above_limit, reason, metric, limit),
                               attributed.unassigned,
                               exact_sum((context.budget, pool.copy_negate())))
