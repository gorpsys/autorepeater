"""Neutral financial models and validation, independent from the working engine."""
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_data import InstrumentType, PortfolioEntry


class TradeMode(StrEnum):
    """Permission to reduce positions in a future financial plan."""
    BUY_ONLY = 'BUY_ONLY'
    REBALANCE = 'REBALANCE'


@dataclass(frozen=True)
class AllocationProfile:
    """Ideal security exposures before lot rounding or tail exclusion."""
    exposures: dict[str, Decimal]
    prices: dict[str, Decimal]
    reserve_fraction: Decimal


@dataclass(frozen=True)
class StrategyContext:
    """One occurrence's full budget and parent-attributed positions."""
    path: tuple[int, ...]
    budget: Decimal
    previous_budget: Decimal
    budget_reduction_requires_rebalance: bool
    positions: tuple[PortfolioEntry, ...]
    marks: dict[str, Decimal]


@dataclass(frozen=True)
class StrategyDecision:
    """Explicit permissions, with a diagnostic reason and optional comparison."""
    mode: TradeMode
    redistribution_allowed: bool
    reason: str
    metric: Decimal | None
    limit: Decimal | None


@dataclass(frozen=True)
class StrategyPlan:  # pylint: disable=too-many-instance-attributes
    """One occurrence's target and ordered child financial plans."""
    path: tuple[int, ...]
    budget: Decimal
    target: TargetPortfolio
    cash_floor: Decimal
    decision: StrategyDecision
    children: tuple['StrategyPlan', ...]
    unassigned: dict[str, Decimal]
    positions: tuple[PortfolioEntry, ...] = ()


def finite_decimal(value: object, field: str, minimum: Decimal = Decimal(0),
                   positive: bool = False) -> Decimal:
    """Validate without coercion or epsilon at a financial boundary."""
    if (not isinstance(value, Decimal) or not value.is_finite()
            or value < minimum or (positive and value == minimum)):
        qualifier = 'positive' if positive else 'nonnegative'
        raise ValueError(f'{field}: expected a {qualifier} finite Decimal')
    return value


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum finite decimal operands without losing small residuals."""
    values = tuple(values)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError('exact_sum requires finite Decimal operands')
    if not values:
        return Decimal(0)
    precision = (max(value.adjusted() for value in values)
                 - min(value.as_tuple().exponent for value in values)
                 + len(str(len(values))) + 2)
    with localcontext() as context:
        context.prec = max(28, precision)
        context.Emin = min(context.Emin, min(value.adjusted() for value in values))
        context.Emax = max(context.Emax, max(value.adjusted() for value in values))
        return sum(values, Decimal(0))


def exact_product(left: Decimal, right: Decimal) -> Decimal:
    """Retain the complete finite product used for virtual position valuation."""
    with localcontext() as context:
        context.prec = max(28, len(left.as_tuple().digits) + len(right.as_tuple().digits))
        return left * right


def validate_path(path: object) -> None:
    """Occurrence identity is an ordered tuple of nonnegative component indices."""
    if (not isinstance(path, tuple)
            or any(isinstance(index, bool) or not isinstance(index, int) or index < 0
                   for index in path)):
        raise ValueError('path: expected a tuple of nonnegative indices')


def validate_map(values: object, field: str, positive: bool = False) -> None:
    """Validate a UID map without silently ignoring malformed entries."""
    if not isinstance(values, dict):
        raise ValueError(f'{field}: expected a dict')
    for uid, value in values.items():
        if not isinstance(uid, str) or not uid:
            raise ValueError(f'{field}: invalid UID {uid}')
        finite_decimal(value, f'{field}[{uid}]', positive=positive)


def validate_profile(profile: object) -> None:
    """DTOs may have zero invested fraction; capital recovery checks positivity."""
    if not isinstance(profile, AllocationProfile):
        raise ValueError('profile: expected AllocationProfile')
    validate_map(profile.exposures, 'exposures')
    validate_map(profile.prices, 'prices')
    for uid, exposure in profile.exposures.items():
        if uid not in profile.prices:
            raise ValueError(f'profile prices: missing UID {uid}')
        if profile.prices[uid] <= 0 < exposure:
            raise ValueError(
                f'profile prices: positive exposure requires a positive price for {uid}')
    finite_decimal(profile.reserve_fraction, 'reserve_fraction')
    if exact_sum((*profile.exposures.values(), profile.reserve_fraction)) > 1:
        raise ValueError('profile sum(exposures) + reserve_fraction must not exceed 1')


def validate_positions(positions: object, marks: dict[str, Decimal] | None = None) -> None:
    """Only long securities, consistently valued by one shared mark per UID."""
    if not isinstance(positions, tuple):
        raise ValueError('positions: expected a tuple')
    if marks is not None:
        validate_map(marks, 'marks')
    seen = set()
    for entry in positions:
        if not isinstance(entry, PortfolioEntry):
            raise ValueError('positions: expected PortfolioEntry')
        if not isinstance(entry.uid, str) or not entry.uid or entry.uid in seen:
            raise ValueError(f'positions: invalid or duplicate UID {entry.uid}')
        seen.add(entry.uid)
        if (not isinstance(entry.instrument_type, InstrumentType)
                or entry.instrument_type == InstrumentType.CURRENCY):
            raise ValueError(f'positions[{entry.uid}]: expected a security type')
        finite_decimal(entry.quantity, f'positions[{entry.uid}].quantity')
        finite_decimal(entry.current_price, f'positions[{entry.uid}].current_price',
                       positive=entry.quantity > 0)
        if marks is not None and (entry.uid not in marks
                                  or marks[entry.uid] != entry.current_price):
            raise ValueError(f'positions[{entry.uid}]: price/marks mismatch')


def validate_context(context: object) -> None:
    """Reject invalid budgets and an inconsistent forced-reduction flag."""
    if not isinstance(context, StrategyContext):
        raise ValueError('context: expected StrategyContext')
    validate_path(context.path)
    finite_decimal(context.budget, 'budget', positive=True)
    finite_decimal(context.previous_budget, 'previous_budget')
    if not isinstance(context.budget_reduction_requires_rebalance, bool):
        raise ValueError('budget_reduction_requires_rebalance: expected bool')
    if context.budget_reduction_requires_rebalance and context.budget >= context.previous_budget:
        raise ValueError('budget_reduction_requires_rebalance requires budget < previous_budget')
    validate_positions(context.positions, context.marks)


def validate_decision(decision: object) -> None:
    """Validate explicit permission without interpreting its diagnostic reason."""
    if not isinstance(decision, StrategyDecision):
        raise ValueError('decision: expected StrategyDecision')
    if not isinstance(decision.mode, TradeMode):
        raise ValueError('decision mode: expected TradeMode')
    if not isinstance(decision.redistribution_allowed, bool):
        raise ValueError('decision redistribution_allowed: expected bool')
    if decision.redistribution_allowed and decision.mode != TradeMode.REBALANCE:
        raise ValueError('decision redistribution_allowed requires REBALANCE')
    if not isinstance(decision.reason, str) or not decision.reason:
        raise ValueError('decision reason: expected a nonempty string')
    for field in ('metric', 'limit'):
        value = getattr(decision, field)
        if value is not None:
            finite_decimal(value, f'decision {field}')


def _validate_main_target(target: object) -> None:
    if not isinstance(target, TargetPortfolio):
        raise ValueError('plan target: expected TargetPortfolio')
    validate_target(target)
    validate_map(target.quantities, 'plan target quantities')
    if not any(quantity > 0 for quantity in target.quantities.values()):
        raise ValueError('plan target must contain a positive quantity')
    for uid, quantity in target.quantities.items():
        if target.prices[uid] <= 0 < quantity:
            raise ValueError(f'plan target price must be positive for UID {uid}')


def _validate_plan_context(plan: StrategyPlan, context: StrategyContext) -> None:
    """Check the occurrence, assigned budget and mandatory reduction decision together."""
    validate_context(context)
    if plan.path != context.path or plan.budget != context.budget:
        raise ValueError('plan path/budget must match context')
    if plan.positions != context.positions:
        raise ValueError('plan positions must match context')
    if (context.budget_reduction_requires_rebalance
            and plan.decision.mode != TradeMode.REBALANCE):
        raise ValueError('budget reduction requires REBALANCE')


def validate_plan(plan: object, context: StrategyContext | None = None) -> None:
    """Validate the entire future financial tree before any execution."""
    if not isinstance(plan, StrategyPlan):
        raise ValueError('plan: expected StrategyPlan')
    validate_path(plan.path)
    finite_decimal(plan.budget, 'plan budget', positive=True)
    finite_decimal(plan.cash_floor, 'cash_floor')
    if plan.cash_floor > plan.budget:
        raise ValueError('cash_floor must not exceed budget')
    validate_decision(plan.decision)
    if context is not None:
        _validate_plan_context(plan, context)
    _validate_main_target(plan.target)
    validate_map(plan.unassigned, 'unassigned')
    validate_positions(plan.positions)
    if not isinstance(plan.children, tuple):
        raise ValueError('plan children: expected an ordered tuple')
    for index, child in enumerate(plan.children):
        validate_plan(child)
        if child.path != plan.path + (index,):
            raise ValueError('child path must match its occurrence index')
    if exact_sum(child.budget for child in plan.children) > plan.budget:
        raise ValueError('children budget sum exceeds parent budget')
