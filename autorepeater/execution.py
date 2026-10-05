"""Neutral receipts and one-pass execution, without SDK dependencies."""
from dataclasses import dataclass
import logging
from typing import Protocol

from autorepeater.execution_data import ExecutionDataError
from autorepeater.order_plan import (
    ZERO, allocate_money, cash_bound, difference, nodes, ready,
    spending_limits, validate_intent, validate_rules,
)
from autorepeater.purchase_plan import purchase_intents
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import exact_product, exact_sum, validate_map, validate_plan

LOGGER = logging.getLogger('tinkoffBot')


class OrderExecutionError(Exception):
    """Stop this pass; a submitted order may still execute later."""


@dataclass(frozen=True)
class ExecutionReceipt:  # pylint: disable=too-many-instance-attributes
    """Confirmed facts only; cash excludes undocumented SDK proceeds fields."""
    uid: str
    side: str
    order_id: str
    status: str
    lots_requested: int
    lots_executed: int
    cash: dict


class OrderExecutor(Protocol):  # pylint: disable=too-few-public-methods
    """Neutral submission boundary, never retried by the orchestrator."""

    def submit_order(self, account_id, intent) -> ExecutionReceipt:
        """Submit exactly once, returning documented execution facts."""


def _submit(account_id, intent, executor):
    try:
        receipt = executor.submit_order(account_id, intent)
    except OrderExecutionError:
        LOGGER.error('Stopping pass: unknown order result UID %s side %s', intent.uid, intent.side)
        raise
    valid = (isinstance(receipt, ExecutionReceipt) and receipt.uid == intent.uid
             and receipt.side == intent.side and isinstance(receipt.order_id, str)
             and bool(receipt.order_id) and receipt.status == 'FILL'
             and isinstance(receipt.lots_requested, int)
             and not isinstance(receipt.lots_requested, bool)
             and isinstance(receipt.lots_executed, int)
             and not isinstance(receipt.lots_executed, bool)
             and receipt.lots_requested == receipt.lots_executed == intent.lots)
    if not valid:
        LOGGER.error('Stopping pass: UID %s side %s order_id %s status %s requested %s executed %s',
                     intent.uid, intent.side, getattr(receipt, 'order_id', None),
                     getattr(receipt, 'status', None), getattr(receipt, 'lots_requested', None),
                     getattr(receipt, 'lots_executed', None))
        raise OrderExecutionError('order not fully confirmed; stopping pass')
    try:
        validate_map(receipt.cash, 'receipt cash')
    except ValueError as error:
        LOGGER.error('Stopping pass: invalid confirmed cash UID %s', intent.uid)
        raise OrderExecutionError('invalid receipt cash; stopping pass') from error
    return receipt


def _fresh(account_id, data, uids):
    try:
        snapshot = data.get_destination(account_id)
        if not ready(snapshot):
            return snapshot, {}
        rules = data.get_trade_rules(account_id, uids)
    except ExecutionDataError as error:
        LOGGER.error('Stopping pass: fresh execution availability failed')
        raise OrderExecutionError('execution refresh failed; stopping pass') from error
    validate_rules(rules)
    validate_map(snapshot.quantities, 'fresh quantities')
    validate_map(snapshot.available_cash, 'fresh cash')
    return snapshot, rules


def _sale_paths(origin, by_path, limits):
    domain = origin
    while domain and by_path[domain[:-1]].decision.redistribution_allowed:
        domain = domain[:-1]
    result = set()
    for path in limits:
        if path[:len(domain)] != domain:
            continue
        crossing = [path[:depth] for depth in range(len(domain), len(path))]
        if path == origin or all(
                by_path[prefix].decision.redistribution_allowed
                or prefix == origin and bool(by_path[origin].unassigned)
                for prefix in crossing):
            result.add(path)
    return result


def _fresh_money(plan, ownership, snapshot, rules, receipts):  # pylint: disable=too-many-locals
    limits = spending_limits(plan.strategy, ownership, plan.marks)
    amount = cash_bound(plan.strategy, snapshot, rules)
    money = {path: min(limit, plan.money[path]) for path, limit in limits.items()}
    initial = allocate_money(min(amount, exact_sum(money.values())), money)
    remaining = difference(amount, exact_sum(initial.values()))
    gaps = {path: difference(limit, initial[path]) for path, limit in limits.items()}
    sale_credits = dict.fromkeys(limits, ZERO)
    by_path = {node.path: node for node in nodes(plan.strategy)}
    shared_pool = bool(plan.sells)
    for intent, receipt in zip(plan.sells, receipts):
        origins = sorted(intent.pieces)
        scopes = {origin: _sale_paths(origin, by_path, limits) for origin in origins}
        common_scope = all(scope == set(limits) for scope in scopes.values())
        shared_pool = shared_pool and common_scope
        if 'rub' not in receipt.cash:
            # Account availability cannot identify proceeds of isolated sales.
            # It can fund a common pool only when every seller may fund every leaf.
            if not common_scope:
                LOGGER.info('Defer scoped purchases: sale cash attribution unavailable UID %s',
                            intent.uid)
            continue
        parts = proportional_split(receipt.cash['rub'],
                                   [intent.pieces[origin] for origin in origins])
        for origin, part in zip(origins, parts):
            scoped = {path: max(ZERO, difference(gap, sale_credits[path]))
                      if path in scopes[origin] else ZERO for path, gap in gaps.items()}
            for path, credit in allocate_money(part, scoped).items():
                sale_credits[path] = exact_sum((sale_credits[path], credit))
    extra = allocate_money(remaining, sale_credits)
    if shared_pool and any('rub' not in receipt.cash for receipt in receipts):
        residual = allocate_money(difference(remaining, exact_sum(extra.values())), {
            path: difference(gap, extra[path]) for path, gap in gaps.items()})
        extra = {path: exact_sum((extra[path], residual[path])) for path in limits}
    if receipts and exact_sum(extra.values()) == 0:
        LOGGER.info('Defer scoped purchases: fresh availability has no permitted funded lot')
    return {path: exact_sum((initial[path], extra[path])) for path in limits}


def _positions_match(snapshot, ownership):
    uids = set(snapshot.quantities) | {uid for values in ownership.values() for uid in values}
    if any(snapshot.quantities.get(uid, ZERO) != exact_sum(
            values.get(uid, ZERO) for values in ownership.values()) for uid in uids):
        LOGGER.info('Defer purchases: fresh positions differ from fixed pass ownership')
        return False
    return True


def execute_plan(account_id, plan, data, executor):  # pylint: disable=too-many-locals
    """Sales confirmed individually; fresh positions/caps constrain a new BUY plan."""
    validate_plan(plan.strategy)
    for intent in (*plan.sells, *plan.buys):
        validate_intent(intent)
    if not ready(plan.snapshot):
        return ()
    receipts = []
    ownership = {path: dict(values) for path, values in plan.ownership.items()}
    for intent in plan.sells:
        receipts.append(_submit(account_id, intent, executor))
        for path, quantity in intent.pieces.items():
            ownership[path][intent.uid] = difference(ownership[path][intent.uid], quantity)
    uids = sorted(set(plan.rules) | set(plan.strategy.target.quantities))
    snapshot, rules = ((plan.snapshot, plan.rules) if not plan.sells
                       else _fresh(account_id, data, uids))
    if not ready(snapshot) or not _positions_match(snapshot, ownership):
        return tuple(receipts)
    money = _fresh_money(plan, ownership, snapshot, rules, receipts)
    buys = purchase_intents(plan.strategy, snapshot, ownership, plan.marks, rules, money)
    submitted = set()
    while buys:
        intent = buys[0]
        receipts.append(_submit(account_id, intent, executor))
        submitted.add(intent.uid)
        for path, quantity in intent.pieces.items():
            ownership[path][intent.uid] = exact_sum((
                ownership[path].get(intent.uid, ZERO), quantity))
            money[path] = difference(
                money[path], exact_product(quantity, plan.marks[intent.uid]))
        if len(buys) == 1:
            break
        snapshot, rules = _fresh(account_id, data, uids)
        if not ready(snapshot) or not _positions_match(snapshot, ownership):
            break
        rules = {uid: rule for uid, rule in rules.items() if uid not in submitted}
        buys = purchase_intents(plan.strategy, snapshot, ownership, plan.marks, rules, money)
    return tuple(receipts)
