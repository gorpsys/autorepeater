"""Neutral receipts and one-pass execution, without SDK dependencies."""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import logging
from time import monotonic, sleep
from typing import Protocol

from autorepeater.execution_data import (
    ExecutionData, ExecutionDataError, ExecutionSnapshot, TradeRules,
)
from autorepeater.money import format_decimal, format_decimal_map
from autorepeater.order_plan import (
    OrderIntent, OrderPlan, ZERO, allocate_money, cash_bound, difference, nodes, ready,
    spending_limits, validate_intent, validate_rules,
)
from autorepeater.purchase_plan import purchase_intents
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import (
    StrategyPlan, exact_product, exact_sum, validate_map, validate_plan,
)

LOGGER = logging.getLogger('tinkoffBot')
SETTLEMENT_TIMEOUT = 30
SETTLEMENT_RETRY_INTERVAL = 2


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
    cash: dict[str, Decimal]


class OrderExecutor(Protocol):  # pylint: disable=too-few-public-methods
    """Neutral submission boundary, never retried by the orchestrator."""

    def submit_order(self, account_id: str, intent: OrderIntent) -> ExecutionReceipt:
        """Submit one intent, returning documented execution facts."""


    def get_order_state(self, account_id: str, order_id: str) -> ExecutionReceipt:
        """Read an already submitted order; never submits or cancels it."""


def _receipt_matches(receipt: object, intent: OrderIntent) -> bool:
    return (isinstance(receipt, ExecutionReceipt) and receipt.uid == intent.uid
            and receipt.side == intent.side and isinstance(receipt.order_id, str)
            and bool(receipt.order_id)
            and isinstance(receipt.lots_requested, int)
            and not isinstance(receipt.lots_requested, bool)
            and receipt.lots_requested == intent.lots
            and isinstance(receipt.lots_executed, int)
            and not isinstance(receipt.lots_executed, bool)
            and 0 <= receipt.lots_executed <= receipt.lots_requested)


def _wait_for_order(account_id: str, intent: OrderIntent, executor: OrderExecutor,
                    receipt: ExecutionReceipt) -> ExecutionReceipt:
    deadline = monotonic() + SETTLEMENT_TIMEOUT
    order_id = receipt.order_id
    while _receipt_matches(receipt, intent) and receipt.status in ('NEW', 'PARTIALLYFILL'):
        remaining = deadline - monotonic()
        if remaining <= 0:
            LOGGER.error('Sale execution timeout: uid=%s order_id=%s status=%s '
                         'requested=%d executed=%d timeout=%s', receipt.uid, order_id,
                         receipt.status, receipt.lots_requested, receipt.lots_executed,
                         SETTLEMENT_TIMEOUT)
            break
        delay = min(SETTLEMENT_RETRY_INTERVAL, remaining)
        LOGGER.info('Waiting for sale execution: uid=%s order_id=%s status=%s '
                    'requested=%d executed=%d retry_in=%s', receipt.uid, order_id,
                    receipt.status, receipt.lots_requested, receipt.lots_executed, delay)
        sleep(delay)
        if monotonic() >= deadline:
            continue
        try:
            receipt = executor.get_order_state(account_id, order_id)
        except OrderExecutionError:
            LOGGER.error('Stopping pass: sale status read failed uid=%s order_id=%s',
                         intent.uid, order_id)
            raise
        if getattr(receipt, 'order_id', None) != order_id:
            LOGGER.error('Stopping pass: sale order ID mismatch expected=%s actual=%s',
                         order_id, getattr(receipt, 'order_id', None))
            raise OrderExecutionError('sale order ID mismatch; stopping pass')
    return receipt


def _submit(account_id: str, intent: OrderIntent, executor: OrderExecutor) -> ExecutionReceipt:
    LOGGER.info('Submitting order: account=%s uid=%s side=%s lots=%d',
                account_id, intent.uid, intent.side, intent.lots)
    try:
        receipt = executor.submit_order(account_id, intent)
    except OrderExecutionError:
        LOGGER.error('Stopping pass: unknown order result UID %s side %s', intent.uid, intent.side)
        raise
    if intent.side == 'SELL' and _receipt_matches(receipt, intent):
        receipt = _wait_for_order(account_id, intent, executor, receipt)
    valid = (_receipt_matches(receipt, intent) and receipt.status == 'FILL'
             and receipt.lots_executed == intent.lots)
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
    LOGGER.info('Confirmed order: uid=%s side=%s order_id=%s status=%s requested=%d executed=%d',
                receipt.uid, receipt.side, receipt.order_id, receipt.status,
                receipt.lots_requested, receipt.lots_executed)
    return receipt


def _fresh(account_id: str, data: ExecutionData, uids: Sequence[str]
           ) -> tuple[ExecutionSnapshot, dict[str, TradeRules]]:
    try:
        snapshot = data.get_destination(account_id)
        LOGGER.info('Execution refresh: account=%s budget=%s available_cash=%s '
                    'limits_ready=%s active_orders=%d', account_id, format_decimal(snapshot.budget),
                    format_decimal_map(snapshot.available_cash), snapshot.limits_ready,
                    len(snapshot.active_orders))
        if not ready(snapshot, stage='refresh'):
            return snapshot, {}
        rules = data.get_trade_rules(account_id, uids)
    except ExecutionDataError as error:
        LOGGER.error('Stopping pass: fresh execution availability failed')
        raise OrderExecutionError('execution refresh failed; stopping pass') from error
    validate_rules(rules)
    validate_map(snapshot.quantities, 'fresh quantities')
    validate_map(snapshot.available_cash, 'fresh cash')
    return snapshot, rules


def _sale_paths(origin: tuple[int, ...], by_path: dict[tuple[int, ...], StrategyPlan],
                limits: dict[tuple[int, ...], Decimal]) -> set[tuple[int, ...]]:
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


def _fresh_money(plan: OrderPlan, ownership: dict[tuple[int, ...], dict[str, Decimal]],
                 snapshot: ExecutionSnapshot, rules: dict[str, TradeRules],
                 receipts: Sequence[ExecutionReceipt]) -> dict[tuple[int, ...], Decimal]:
    # pylint: disable=too-many-locals
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


def _positions_match(snapshot: ExecutionSnapshot,
                     ownership: dict[tuple[int, ...], dict[str, Decimal]]) -> bool:
    uids = set(snapshot.quantities) | {uid for values in ownership.values() for uid in values}
    mismatches = {}
    for uid in sorted(uids):
        actual = snapshot.quantities.get(uid, ZERO)
        expected = exact_sum(values.get(uid, ZERO) for values in ownership.values())
        if actual != expected:
            mismatches[uid] = {'expected': expected, 'actual': actual}
    if mismatches:
        LOGGER.info('Defer purchases: fresh positions differ from fixed pass ownership; '
                    'mismatched_instruments=%d', len(mismatches))
        for uid, values in mismatches.items():
            LOGGER.info('Execution position mismatch: uid=%s expected=%s actual=%s',
                        uid, format_decimal(values['expected']), format_decimal(values['actual']))
        return False
    return True


def _wait_for_settlement(account_id: str, data: ExecutionData, uids: Sequence[str],
                         ownership: dict[tuple[int, ...], dict[str, Decimal]], stage: str
                         ) -> tuple[ExecutionSnapshot, dict[str, TradeRules]]:
    """Poll only reads after FILL; never resubmit orders or infer settled money."""
    deadline = monotonic() + SETTLEMENT_TIMEOUT
    snapshot, rules = _fresh(account_id, data, uids)
    while not ready(snapshot, stage=stage) or not _positions_match(snapshot, ownership):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        delay = min(SETTLEMENT_RETRY_INTERVAL, remaining)
        LOGGER.info('Waiting for execution settlement: account=%s stage=%s '
                    'retry_in=%s remaining=%s', account_id, stage, delay, remaining)
        sleep(delay)
        if monotonic() >= deadline:
            break
        snapshot, rules = _fresh(account_id, data, uids)
    else:
        LOGGER.info('Execution settlement confirmed: account=%s stage=%s available_cash=%s',
                    account_id, stage, format_decimal_map(snapshot.available_cash))
        return snapshot, rules
    LOGGER.warning('Execution settlement timeout: account=%s stage=%s timeout=%s; '
                   'purchases deferred', account_id, stage, SETTLEMENT_TIMEOUT)
    return snapshot, rules


def _execution_result(plan: OrderPlan, receipts: Sequence[ExecutionReceipt], stage: str
                      ) -> tuple[ExecutionReceipt, ...]:
    sells = sum(receipt.side == 'SELL' for receipt in receipts)
    LOGGER.info('Execution result: stage=%s confirmed_sells=%d confirmed_buys=%d '
                'planned_sells=%d planned_buys=%d', stage, sells, len(receipts) - sells,
                len(plan.sells), len(plan.buys))
    return tuple(receipts)


def execute_plan(account_id: str, plan: OrderPlan, data: ExecutionData,
                  executor: OrderExecutor) -> tuple[ExecutionReceipt, ...]:
    # pylint: disable=too-many-locals
    """Sales confirmed individually; fresh positions/caps constrain a new BUY plan."""
    validate_plan(plan.strategy)
    for intent in (*plan.sells, *plan.buys):
        validate_intent(intent)
    if not ready(plan.snapshot, stage='before_sales'):
        return _execution_result(plan, (), 'before_sales')
    receipts = []
    ownership = {path: dict(values) for path, values in plan.ownership.items()}
    for intent in plan.sells:
        receipts.append(_submit(account_id, intent, executor))
        for path, quantity in intent.pieces.items():
            ownership[path][intent.uid] = difference(ownership[path][intent.uid], quantity)
    uids = sorted(set(plan.rules) | set(plan.strategy.target.quantities))
    snapshot, rules = ((plan.snapshot, plan.rules) if not plan.sells
                       else _wait_for_settlement(account_id, data, uids, ownership, 'after_sales'))
    if not ready(snapshot, stage='before_buys') or not _positions_match(snapshot, ownership):
        return _execution_result(plan, receipts, 'before_buys')
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
        snapshot, rules = _wait_for_settlement(account_id, data, uids, ownership, 'after_buy')
        if not ready(snapshot, stage='between_buys') or not _positions_match(snapshot, ownership):
            return _execution_result(plan, receipts, 'between_buys')
        rules = {uid: rule for uid, rule in rules.items() if uid not in submitted}
        buys = purchase_intents(plan.strategy, snapshot, ownership, plan.marks, rules, money)
    return _execution_result(plan, receipts, 'finished')
