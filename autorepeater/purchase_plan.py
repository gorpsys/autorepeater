"""SDK-free greedy monetary underweight reduction with equivalent batching."""
from decimal import Decimal, ROUND_FLOOR, localcontext

from autorepeater import reporting
from autorepeater.cash_pools import cash_bound
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.order_intent import OrderIntent, take_pieces, validate_intent
from autorepeater.strategy_plan import (
    ZERO, StrategyPlan, allocate_floor, difference, exact_product, exact_sum, leaves,
)


def _buy_allowed(rule: TradeRules, *, native: bool) -> bool:
    return ((native or rule.currency == 'rub')
            and rule.api_trade_available and rule.bestprice_order_available)


def _quantity_gap(target: Decimal, current: Decimal) -> Decimal:
    return max(ZERO, difference(target, current))


def has_buy_delta(strategy: StrategyPlan, ownership: dict[tuple[int, ...], dict[str, Decimal]],
                  rules: dict[str, TradeRules], *, native: bool) -> bool:
    """Use current ownership and permissions before money, lot and numeric limits."""
    owners = leaves(strategy)
    for uid, target in strategy.target.quantities.items():
        rule = rules.get(uid)
        if rule is None or not _buy_allowed(rule, native=native):
            continue
        total = exact_sum(positions.get(uid, ZERO) for positions in ownership.values())
        if _quantity_gap(target, total) > ZERO and any(
                _quantity_gap(node.target.quantities.get(uid, ZERO),
                              ownership[path].get(uid, ZERO)) > ZERO
                for path, node in owners.items()):
            return True
    return False


def _candidate(uid: str, state: tuple[
        StrategyPlan, dict[tuple[int, ...], dict[str, Decimal]], dict[str, Decimal],
        dict[str, TradeRules], dict[tuple[int, ...], Decimal], dict[str, Decimal],
        dict[str, int], Decimal, Decimal,
], *, conditional: bool = False
) -> tuple[Decimal, str, int, dict[tuple[int, ...], Decimal]] | None:
    # pylint: disable=too-many-locals
    strategy, ownership, marks, rules, money, quantities, used, remaining, spent = state
    rule = rules.get(uid)
    if (rule is None or not _buy_allowed(rule, native=conditional)
            or strategy.target.quantities[uid] == ZERO):
        return None
    price = marks[uid]
    cost = exact_product(price, Decimal(rule.lot))
    aggregate = _quantity_gap(strategy.target.quantities[uid], quantities.get(uid, ZERO))
    capacities = {}
    for path, node in leaves(strategy).items():
        gap = _quantity_gap(node.target.quantities.get(uid, ZERO),
                            ownership[path].get(uid, ZERO))
        with localcontext() as context:
            context.rounding = ROUND_FLOOR
            context.prec = max(28, len(money[path].as_tuple().digits)
                               + len(price.as_tuple().digits) + 4)
            capacities[path] = min(gap, money[path] / price)
    max_pieces = min(aggregate, exact_sum(capacities.values()))
    max_lots = min(allocate_floor(max_pieces, Decimal(rule.lot)), allocate_floor(remaining, cost))
    if not conditional:
        max_lots = min(max_lots, rule.buy_max_lots - used.get(uid, 0),
                       allocate_floor(max(ZERO, difference(rule.buy_money_amount, spent)), cost))
    if max_lots <= 0:
        return None
    # Every permitted full lot reduces absolute underweight by exactly its cost.
    # Marks are fixed and purchases cannot cross targets. Thus a winning UID
    # remains the winner until a cap is exhausted: batch all those lots.
    pieces = take_pieces(Decimal(max_lots * rule.lot), capacities)
    return cost, uid, max_lots, pieces


def funding_candidates(strategy: StrategyPlan, ownership: dict[tuple[int, ...], dict[str, Decimal]],
                       quantities: dict[str, Decimal], marks: dict[str, Decimal],
                       rules: dict[str, TradeRules], money: dict[tuple[int, ...], Decimal]
                       ) -> tuple[OrderIntent, ...]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Conditional target/budget lots in the same absolute monetary priority queue.

    Current broker cash/lot caps cannot predict caps after FX. These demands are
    never orders authorized for submission; the funding planner checks current
    caps for immediate purchases and requires a refresh for dependent ones.
    """
    state = (strategy, ownership, marks, rules, money, quantities, {},
             exact_sum(money.values()), ZERO)
    candidates = [item for uid in sorted(strategy.target.quantities)
                  if (item := _candidate(uid, state, conditional=True)) is not None]
    return tuple(OrderIntent(uid, 'BUY', lots, rules[uid].lot, pieces)
                 for _, uid, lots, pieces in sorted(candidates,
                                                    key=lambda item: (-item[0], item[1])))


def calculate_purchase_intents(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                               ownership: dict[tuple[int, ...], dict[str, Decimal]],
                               marks: dict[str, Decimal], rules: dict[str, TradeRules],
                               money: dict[tuple[int, ...], Decimal]) -> tuple[OrderIntent, ...]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Highest absolute reduction first, UID then path; no per-lot million loop."""
    ownership = {path: dict(values) for path, values in ownership.items()}
    money = dict(money)
    quantities = dict(snapshot.quantities)
    used = {}
    result = []
    initial = cash_bound(snapshot, rules)
    remaining = initial
    while remaining:
        state = (strategy, ownership, marks, rules, money, quantities, used,
                 remaining, difference(initial, remaining))
        candidates = [item for uid in sorted(strategy.target.quantities)
                      if (item := _candidate(uid, state)) is not None]
        if not candidates:
            break
        cost, uid, lots, pieces = min(candidates, key=lambda item: (-item[0], item[1]))
        rule = rules[uid]
        for path, quantity in pieces.items():
            spent = exact_product(quantity, marks[uid])
            money[path] = difference(money[path], spent)
            ownership[path][uid] = exact_sum((ownership[path].get(uid, ZERO), quantity))
        quantities[uid] = exact_sum((quantities.get(uid, ZERO), Decimal(lots * rule.lot)))
        used[uid] = used.get(uid, 0) + lots
        remaining = difference(remaining, exact_product(cost, Decimal(lots)))
        intent = OrderIntent(uid, 'BUY', lots, rule.lot, pieces)
        validate_intent(intent)
        result.append(intent)
    return tuple(result)


def purchase_intents(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
                     ownership: dict[tuple[int, ...], dict[str, Decimal]],
                     marks: dict[str, Decimal], rules: dict[str, TradeRules],
                     money: dict[tuple[int, ...], Decimal]) -> tuple[OrderIntent, ...]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Report an empty executable BUY queue after its calculation completes."""
    result = calculate_purchase_intents(strategy, snapshot, ownership, marks, rules, money)
    if not result:
        reporting.print_purchase_deferral()
    return result
