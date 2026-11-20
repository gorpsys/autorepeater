"""Physical intents and diagnostics for an already loaded execution snapshot."""
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal

from autorepeater import reporting
from autorepeater.cash_pools import allocate_money, cash_bound, spending_limits
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.fx_funding import FundingInputs, FundingPlan, build_funding_plan
from autorepeater.order_intent import OrderIntent, take_pieces, validate_intent
from autorepeater.purchase_plan import purchase_intents
from autorepeater.strategy_plan import (
    ZERO, StrategyPlan, TradeMode, allocate_floor, difference,
    exact_sum, finite_decimal, nodes, validate_map, validate_plan,
)

PreparedOrderPreview = tuple[
    dict[tuple[int, ...], dict[str, Decimal]], dict[tuple[int, ...], Decimal]]


@dataclass(frozen=True)
class OrderPlan:  # pylint: disable=too-many-instance-attributes
    """One pass, including fixed ownership and initial spending permissions."""
    strategy: StrategyPlan
    snapshot: ExecutionSnapshot
    ownership: dict[tuple[int, ...], dict[str, Decimal]]
    marks: dict[str, Decimal]
    rules: dict[str, TradeRules]
    money: dict[tuple[int, ...], Decimal]
    sells: tuple[OrderIntent, ...]
    buys: tuple[OrderIntent, ...]
    funding: FundingPlan | None = None


def order_snapshot_ready(snapshot: ExecutionSnapshot) -> bool:
    """Decide readiness from the loaded limits and outstanding orders."""
    return bool(snapshot.limits_ready) and not snapshot.active_orders


def ready(snapshot: ExecutionSnapshot, stage: str = 'planning') -> bool:
    """Report the blockers that defer this trading phase."""
    result = order_snapshot_ready(snapshot)
    if not result:
        reporting.print_order_snapshot_deferred(snapshot, stage)
    return result


def validate_rules(rules: dict[str, TradeRules]) -> None:
    """Own limits are native currency amounts and integral nonmargin lot caps."""
    for uid, rule in rules.items():
        if not isinstance(rule, TradeRules):
            raise ValueError(f'trade rules[{uid}]: expected TradeRules')
        if isinstance(rule.lot, bool) or not isinstance(rule.lot, int) or rule.lot <= 0:
            raise ValueError(f'trade rules[{uid}]: invalid lot')
        if not isinstance(rule.currency, str) or not rule.currency:
            raise ValueError(f'trade rules[{uid}]: invalid currency')
        if (not isinstance(rule.api_trade_available, bool)
                or not isinstance(rule.bestprice_order_available, bool)):
            raise ValueError(f'trade rules[{uid}]: invalid permission')
        finite_decimal(rule.buy_money_amount, f'trade rules[{uid}].buy_money_amount')
        for cap in (rule.buy_max_lots, rule.sell_max_lots):
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
                raise ValueError(f'trade rules[{uid}]: invalid own cap')


def _validate_ownership(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                        ownership: dict[tuple[int, ...], dict[str, Decimal]]) -> set[str]:
    """Every physical quantity must belong to exactly the declared occurrence pool."""
    paths = {node.path for node in nodes(strategy) if node.unassigned or not node.children}
    if set(ownership) != paths:
        raise ValueError('ownership paths must match leaves/unassigned occurrences')
    for path, quantities in ownership.items():
        validate_map(quantities, f'ownership {path}')
    uids = set(snapshot.quantities) | {
        uid for quantities in ownership.values() for uid in quantities}
    for uid in uids:
        owned = exact_sum(quantities.get(uid, ZERO) for quantities in ownership.values())
        if owned != snapshot.quantities.get(uid, ZERO):
            raise ValueError(f'ownership does not conserve destination UID {uid}')
    return uids


def _validate_target_marks(strategy: StrategyPlan,
                           ownership: dict[tuple[int, ...], dict[str, Decimal]],
                           marks: dict[str, Decimal]) -> None:
    """Check unassigned ownership and valuation for every node before any intent."""
    for node in nodes(strategy):
        for uid, quantity in node.unassigned.items():
            if ownership[node.path].get(uid, ZERO) < quantity:
                raise ValueError(f'ownership missing unassigned UID {uid}')
        for uid in node.target.quantities:
            if uid not in marks:
                raise ValueError(f'marks missing target UID {uid}')
            if marks[uid] <= 0 < node.target.quantities[uid]:
                raise ValueError(f'positive mark required for target UID {uid}')


def _validate_inputs(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                     ownership: dict[tuple[int, ...], dict[str, Decimal]],
                     marks: dict[str, Decimal], rules: dict[str, TradeRules]) -> None:
    validate_plan(strategy)
    validate_map(snapshot.quantities, 'destination quantities')
    validate_map(snapshot.available_quantities, 'free quantities')
    validate_map(snapshot.available_cash, 'available cash')
    validate_map(marks, 'marks')
    validate_rules(rules)
    uids = _validate_ownership(strategy, snapshot, ownership)
    _validate_target_marks(strategy, ownership, marks)
    for uid in uids:
        if uid not in marks:
            raise ValueError(f'marks missing destination UID {uid}')
        if marks[uid] <= 0 < snapshot.quantities.get(uid, ZERO):
            raise ValueError(f'positive mark required for destination UID {uid}')


def _sale_capacities(strategy: StrategyPlan,
                     ownership: dict[tuple[int, ...], dict[str, Decimal]], uid: str
                     ) -> tuple[dict[tuple[int, ...], Decimal], dict[tuple[int, ...], Decimal]]:
    """Only unassigned holdings and rebalance-authorized leaf excess may be sold."""
    capacities = {}
    deltas = {}
    for node in nodes(strategy):
        held = ownership.get(node.path, {}).get(uid, ZERO)
        unassigned = node.unassigned.get(uid, ZERO)
        if unassigned:
            capacities[node.path] = unassigned
            deltas[node.path] = unassigned
        if not node.children and node.decision.mode == TradeMode.REBALANCE:
            delta = max(ZERO, difference(held, node.target.quantities.get(uid, ZERO)))
            if delta:
                capacities[node.path] = held
                deltas[node.path] = delta
    return capacities, deltas


def iter_sale_steps(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                    ownership: dict[tuple[int, ...], dict[str, Decimal]],
                    rules: dict[str, TradeRules], *, native: bool = False
                    ) -> Iterator[tuple[str, OrderIntent | None,
                                        Literal['unavailable', 'residual'] | None]]:
    # pylint: disable=too-many-locals
    """Net and round each UID in order, yielding its intent and any deferral."""
    for uid, current in sorted(snapshot.quantities.items()):
        rule = rules.get(uid)
        if (rule is None or rule.currency != 'rub' and not native or not rule.api_trade_available
                or not rule.bestprice_order_available):
            yield uid, None, 'unavailable'
            continue
        excess = max(ZERO, difference(current, strategy.target.quantities.get(uid, ZERO)))
        capacities, deltas = _sale_capacities(strategy, ownership, uid)
        delta = min(excess, exact_sum(deltas.values()))
        with localcontext() as context:
            context.prec = max(28, len(delta.as_tuple().digits) + 4)
            rounded = int((delta / rule.lot).to_integral_value(rounding=ROUND_HALF_EVEN))
        lots = min(rounded,
                   allocate_floor(snapshot.available_quantities.get(uid, ZERO), Decimal(rule.lot)),
                   allocate_floor(exact_sum(capacities.values()), Decimal(rule.lot)),
                   rule.sell_max_lots)
        intent = None
        if lots:
            amount = Decimal(lots * rule.lot)
            pieces = take_pieces(min(amount, exact_sum(deltas.values())), deltas)
            remainder = difference(amount, exact_sum(pieces.values()))
            if remainder:
                extra = take_pieces(remainder, {
                    path: difference(held, pieces.get(path, ZERO))
                    for path, held in capacities.items()})
                for path, quantity in extra.items():
                    pieces[path] = exact_sum((pieces.get(path, ZERO), quantity))
            intent = OrderIntent(uid, 'SELL', lots, rule.lot, pieces)
            validate_intent(intent)
        yield uid, intent, 'residual' if delta != Decimal(lots * rule.lot) else None


def sale_intents(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                 ownership: dict[tuple[int, ...], dict[str, Decimal]],
                 rules: dict[str, TradeRules], *, native: bool = False) -> tuple[OrderIntent, ...]:
    """Report each UID's deferral before calculating the next UID."""
    result = []
    for uid, intent, reason in iter_sale_steps(strategy, snapshot, ownership, rules, native=native):
        if reason is not None:
            reporting.print_sale_deferral(uid, reason)
        if intent is not None:
            result.append(intent)
    return tuple(result)


def prepare_order_preview(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                          ownership: dict[tuple[int, ...], dict[str, Decimal]],
                          marks: dict[str, Decimal],
                          rules: dict[str, TradeRules]) -> PreparedOrderPreview:
    """Normalize ownership and validate every target before calculating any intent."""
    ownership = {path: dict(quantities) for path, quantities in ownership.items()}
    for node in nodes(strategy):
        if node.unassigned:
            area = ownership.setdefault(node.path, {})
            for uid, quantity in node.unassigned.items():
                area.setdefault(uid, quantity)
    _validate_inputs(strategy, snapshot, ownership, marks, rules)
    money = allocate_money(cash_bound(snapshot, rules),
                           spending_limits(strategy, ownership, marks))
    return ownership, money


def finish_order_plan(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                      marks: dict[str, Decimal], rules: dict[str, TradeRules],
                      preview: tuple[dict[tuple[int, ...], dict[str, Decimal]],
                                     dict[tuple[int, ...], Decimal],
                                     tuple[OrderIntent, ...], tuple[OrderIntent, ...]], *,
                      funding: FundingInputs | None = None) -> OrderPlan:
    # pylint: disable=too-many-arguments
    """Attach optional conditional funding after the executable preview."""
    ownership, money, sells, buys = preview
    funding_plan = None
    if funding is not None:
        validate_rules(dict(funding.fx_rules))
        funding_plan = build_funding_plan(strategy, snapshot, ownership, marks, rules,
                                          spending_limits(strategy, ownership, marks), funding)
    return OrderPlan(strategy, snapshot, ownership, dict(marks), dict(rules), money,
                     sells, buys, funding_plan)


def build_order_plan(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                     ownership: dict[tuple[int, ...], dict[str, Decimal]],
                     marks: dict[str, Decimal], rules: dict[str, TradeRules], *,
                     funding: FundingInputs | None = None) -> OrderPlan:
    # pylint: disable=too-many-arguments
    """Build and report the physical preview before calculating any conditional funding."""
    prepared = prepare_order_preview(strategy, snapshot, ownership, marks, rules)
    return build_prepared_order_plan(strategy, snapshot, marks, rules, prepared, funding=funding)


def build_prepared_order_plan(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                              marks: dict[str, Decimal], rules: dict[str, TradeRules],
                              prepared: PreparedOrderPreview, *,
                              funding: FundingInputs | None = None) -> OrderPlan:
    # pylint: disable=too-many-arguments
    """Build and report intents from the result of prepare_order_preview."""
    ownership, money = prepared
    sells, buys = (), ()
    if ready(snapshot, stage='planning'):
        sells = sale_intents(strategy, snapshot, ownership, rules, native=funding is not None)
        buys = purchase_intents(strategy, snapshot, ownership, marks, rules, money)
    return finish_order_plan(strategy, snapshot, marks, rules, (ownership, money, sells, buys),
                             funding=funding)
