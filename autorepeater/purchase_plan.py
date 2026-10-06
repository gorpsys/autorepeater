"""SDK-free greedy monetary underweight reduction with equivalent batching."""
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, localcontext
import logging

from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.strategy_plan import (
    StrategyPlan, exact_product, exact_sum, finite_decimal, validate_path,
)

LOGGER = logging.getLogger('tinkoffBot')


ZERO = Decimal(0)


def difference(left: Decimal, right: Decimal) -> Decimal:
    """Subtract finite values without losing a previously corrected residual."""
    return exact_sum((left, right.copy_negate()))


@dataclass(frozen=True)
class OrderIntent:
    """Only the aggregate is integral; occurrence contributions are pieces."""
    uid: str
    side: str
    lots: int
    lot_size: int
    pieces: dict[tuple[int, ...], Decimal]


def validate_intent(intent: object) -> None:
    """Reject malformed physical orders before crossing the SDK boundary."""
    if not isinstance(intent, OrderIntent) or not isinstance(intent.uid, str) or not intent.uid:
        raise ValueError('intent requires a UID')
    if intent.side not in ('BUY', 'SELL'):
        raise ValueError('intent side must be BUY or SELL')
    for value in (intent.lots, intent.lot_size):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError('intent lots and lot_size must be positive integers')
    if not isinstance(intent.pieces, dict) or not intent.pieces:
        raise ValueError('intent pieces must contain ownership')
    for path, quantity in intent.pieces.items():
        validate_path(path)
        finite_decimal(quantity, 'intent pieces', positive=True)
    if exact_sum(intent.pieces.values()) != Decimal(intent.lots * intent.lot_size):
        raise ValueError('intent ownership sum must equal lots * lot_size')


def nodes(plan: StrategyPlan) -> Iterator[StrategyPlan]:
    """Preorder occurrences, including unassigned money at internal nodes."""
    yield plan
    for child in plan.children:
        yield from nodes(child)


def leaves(plan: StrategyPlan) -> dict[tuple[int, ...], StrategyPlan]:
    """Terminal targets retain their own spending limits."""
    return {node.path: node for node in nodes(plan) if not node.children}


def cash_bound(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
               rules: dict[str, TradeRules]) -> Decimal:
    """Intersect documented own money with currency upper bound, reserve once."""
    rub = snapshot.available_cash.get('rub', ZERO)
    bounds = [rule.buy_money_amount for rule in rules.values()
              if rule.currency == 'rub' and rule.api_trade_available
              and rule.bestprice_order_available and rule.buy_max_lots > 0]
    # Different instrument bounds are never summed. Each candidate checks its
    # own documented bound, reduced by all earlier simulated spending.
    amount = min(rub, max(bounds)) if bounds else ZERO
    return max(ZERO, difference(amount, strategy.cash_floor))


def take_pieces(amount: Decimal, capacities: dict[tuple[int, ...], Decimal]
                ) -> dict[tuple[int, ...], Decimal]:
    """Fill an aggregate in path order, never exceeding an owner's cap."""
    result = {}
    for path in sorted(capacities):
        quantity = min(amount, capacities[path])
        if quantity:
            result[path] = quantity
            amount = difference(amount, quantity)
    if amount:
        raise ValueError('ownership cannot cover physical lot')
    return result


def allocate_floor(amount: Decimal, divisor: Decimal) -> int:
    """Integral ratio without precision-28 rounding up a monetary/piece cap."""
    numerator, denominator = amount.as_integer_ratio()
    div_numerator, div_denominator = divisor.as_integer_ratio()
    return (numerator * div_denominator) // (denominator * div_numerator)


def _candidate(uid: str, state: tuple[
        StrategyPlan, dict[tuple[int, ...], dict[str, Decimal]], dict[str, Decimal],
        dict[str, TradeRules], dict[tuple[int, ...], Decimal], dict[str, Decimal],
        dict[str, int], Decimal, Decimal,
]) -> tuple[Decimal, str, int, dict[tuple[int, ...], Decimal]] | None:
    # pylint: disable=too-many-locals
    strategy, ownership, marks, rules, money, quantities, used, remaining, spent = state
    rule = rules.get(uid)
    if (strategy.target.quantities[uid] == 0 or rule is None
            or rule.currency != 'rub' or not rule.api_trade_available
            or not rule.bestprice_order_available):
        return None
    price = marks[uid]
    cost = exact_product(price, Decimal(rule.lot))
    aggregate = max(ZERO, difference(strategy.target.quantities[uid], quantities.get(uid, ZERO)))
    capacities = {}
    for path, node in leaves(strategy).items():
        gap = max(ZERO, difference(node.target.quantities.get(uid, ZERO),
                                   ownership[path].get(uid, ZERO)))
        with localcontext() as context:
            context.rounding = ROUND_FLOOR
            context.prec = max(28, len(money[path].as_tuple().digits)
                               + len(price.as_tuple().digits) + 4)
            capacities[path] = min(gap, money[path] / price)
    max_pieces = min(aggregate, exact_sum(capacities.values()))
    max_lots = min(allocate_floor(max_pieces, Decimal(rule.lot)),
                   rule.buy_max_lots - used.get(uid, 0),
                   allocate_floor(remaining, cost),
                   allocate_floor(max(ZERO, difference(
                       difference(rule.buy_money_amount, strategy.cash_floor), spent)), cost))
    if max_lots <= 0:
        return None
    # Every permitted full lot reduces absolute underweight by exactly its cost.
    # Marks are fixed and purchases cannot cross targets. Thus a winning UID
    # remains the winner until a cap is exhausted: batch all those lots.
    pieces = take_pieces(Decimal(max_lots * rule.lot), capacities)
    return cost, uid, max_lots, pieces


def purchase_intents(strategy: StrategyPlan, snapshot: ExecutionSnapshot,
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
    initial = cash_bound(strategy, snapshot, rules)
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
    if not result:
        LOGGER.info('Defer purchases: no funded executable underweight lot')
    return tuple(result)
