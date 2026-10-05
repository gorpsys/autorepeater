"""Pure physical intents retaining occurrence ownership and monetary bounds."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import logging

from autorepeater.execution_data import TradeRules
from autorepeater.money import format_decimal_map
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import (
    TradeMode, exact_product, exact_sum, finite_decimal, validate_map,
    validate_plan,
)

from autorepeater.purchase_plan import (
    OrderIntent, ZERO, allocate_floor, cash_bound, difference, leaves, nodes,
    purchase_intents, take_pieces, validate_intent,
)

LOGGER = logging.getLogger('tinkoffBot')



@dataclass(frozen=True)
class OrderPlan:  # pylint: disable=too-many-instance-attributes
    """One pass, including fixed ownership and initial spending permissions."""
    strategy: object
    snapshot: object
    ownership: dict
    marks: dict
    rules: dict
    money: dict
    sells: tuple[OrderIntent, ...]
    buys: tuple[OrderIntent, ...]


def ready(snapshot, stage='planning'):
    """Blocked/loading or outstanding orders defer this entire trading phase."""
    if not snapshot.limits_ready or snapshot.active_orders:
        LOGGER.info('Defer execution: active orders or unavailable/blocked limits; '
                    'stage=%s limits_ready=%s active_orders=%d available_cash=%s',
                    stage, snapshot.limits_ready, len(snapshot.active_orders),
                    format_decimal_map(snapshot.available_cash))
        return False
    return True


def validate_rules(rules):
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


def spending_limits(strategy, ownership, marks):
    """Potential monetary gaps are caps, never presumed cash or sale proceeds."""
    result = {}
    for path, node in leaves(strategy).items():
        value = exact_sum(exact_product(quantity, marks[uid])
                          for uid, quantity in ownership[path].items())
        result[path] = max(ZERO, difference(difference(node.budget, node.cash_floor), value))
    return result


def allocate_money(amount, limits):
    """Bounded proportional pool with exact residual conservation."""
    paths = sorted(limits)
    total = exact_sum(limits.values())
    if not total:
        return dict.fromkeys(paths, ZERO)
    parts = proportional_split(min(amount, total), [limits[path] for path in paths],
                               ceilings=[limits[path] for path in paths])
    return dict(zip(paths, parts))


def _validate_inputs(strategy, snapshot, ownership, marks, rules):
    # pylint: disable=too-many-branches
    validate_plan(strategy)
    validate_map(snapshot.quantities, 'destination quantities')
    validate_map(snapshot.available_quantities, 'free quantities')
    validate_map(snapshot.available_cash, 'available cash')
    validate_map(marks, 'marks')
    validate_rules(rules)
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
    for node in nodes(strategy):
        for uid, quantity in node.unassigned.items():
            if ownership[node.path].get(uid, ZERO) < quantity:
                raise ValueError(f'ownership missing unassigned UID {uid}')
        for uid in node.target.quantities:
            if uid not in marks:
                raise ValueError(f'marks missing target UID {uid}')
            if marks[uid] <= 0 < node.target.quantities[uid]:
                raise ValueError(f'positive mark required for target UID {uid}')
    for uid in uids:
        if uid not in marks:
            raise ValueError(f'marks missing destination UID {uid}')
        if marks[uid] <= 0 < snapshot.quantities.get(uid, ZERO):
            raise ValueError(f'positive mark required for destination UID {uid}')


def sale_intents(strategy, snapshot, ownership, rules):  # pylint: disable=too-many-locals
    """Net physical excess, then round once within free and permitted ownership."""
    result = []
    for uid, current in sorted(snapshot.quantities.items()):
        rule = rules.get(uid)
        if (rule is None or rule.currency != 'rub' or not rule.api_trade_available
                or not rule.bestprice_order_available):
            LOGGER.info('Defer SELL UID %s: currency or API/BESTPRICE unavailable', uid)
            continue
        excess = max(ZERO, difference(current, strategy.target.quantities.get(uid, ZERO)))
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
        delta = min(excess, exact_sum(deltas.values()))
        with localcontext() as context:
            context.prec = max(28, len(delta.as_tuple().digits) + 4)
            rounded = int((delta / rule.lot).to_integral_value(rounding=ROUND_HALF_EVEN))
        lots = min(rounded,
                   allocate_floor(snapshot.available_quantities.get(uid, ZERO), Decimal(rule.lot)),
                   allocate_floor(exact_sum(capacities.values()), Decimal(rule.lot)),
                   rule.sell_max_lots)
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
            result.append(intent)
        if delta != Decimal(lots * rule.lot):
            LOGGER.info('Defer SELL residual UID %s: physical lot/ownership/own cap', uid)
    return tuple(result)


def build_order_plan(strategy, snapshot, ownership, marks, rules):
    """Validate every target first; no conditional proceeds enter the BUY pool."""
    ownership = {path: dict(quantities) for path, quantities in ownership.items()}
    for node in nodes(strategy):
        if node.unassigned:
            area = ownership.setdefault(node.path, {})
            for uid, quantity in node.unassigned.items():
                area.setdefault(uid, quantity)
    _validate_inputs(strategy, snapshot, ownership, marks, rules)
    money = allocate_money(cash_bound(strategy, snapshot, rules),
                           spending_limits(strategy, ownership, marks))
    sells, buys = (), ()
    if ready(snapshot):
        sells = sale_intents(strategy, snapshot, ownership, rules)
        buys = purchase_intents(strategy, snapshot, ownership, marks, rules, money)
    return OrderPlan(strategy, snapshot, ownership, dict(marks), dict(rules), money, sells, buys)
