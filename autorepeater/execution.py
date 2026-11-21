"""Neutral receipts and one-pass execution, without SDK dependencies."""
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import logging
from time import monotonic, sleep
from typing import Protocol

from autorepeater.cash_facts import OrderAmounts
from autorepeater.cash_pools import allocate_rub_after_sales, cash_bound
from autorepeater.execution_data import (
    ExecutionData, ExecutionDataError, ExecutionSnapshot, TradeRules,
)
from autorepeater.money import format_decimal, format_decimal_map
from autorepeater.order_intent import OrderIntent, validate_intent
from autorepeater.order_plan import OrderPlan, ready, validate_rules
from autorepeater.purchase_plan import has_buy_delta, purchase_intents
from autorepeater.reporting import print_order_money
from autorepeater.strategy_plan import (
    ZERO, difference, exact_product, exact_sum, validate_map, validate_plan,
)

LOGGER = logging.getLogger('tinkoffBot')
SETTLEMENT_TIMEOUT = 30
SETTLEMENT_RETRY_INTERVAL = 2


class OrderExecutionError(Exception):
    """Stop this pass; a submitted order may still execute later."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.confirmed_receipts: tuple[ExecutionReceipt, ...] | None = None


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
    amounts: OrderAmounts | None = field(default=None, compare=False)


def confirmed_receipts_from_error(error: BaseException) -> tuple[ExecutionReceipt, ...] | None:
    """Read a typed snapshot of accepted fills from a supported pass error."""
    if not isinstance(error, (OrderExecutionError, TimeoutError)):
        return None
    receipts = error.__dict__.get('confirmed_receipts')
    if isinstance(receipts, tuple) and all(isinstance(item, ExecutionReceipt) for item in receipts):
        return receipts
    return None


@dataclass
class _PassReceipts:
    """Accepted fills and the raw timeout, if any, from this pass's submission."""

    accepted: list[ExecutionReceipt] = field(default_factory=list)
    submit_timeout: TimeoutError | None = None


@contextmanager
def _record_confirmed_receipts() -> Iterator[_PassReceipts]:
    receipts = _PassReceipts()
    try:
        yield receipts
    except OrderExecutionError as error:
        error.confirmed_receipts = tuple(receipts.accepted) or None
        raise
    except TimeoutError as error:
        if error is receipts.submit_timeout:
            error.__dict__['confirmed_receipts'] = tuple(receipts.accepted) or None
        elif 'confirmed_receipts' in error.__dict__:
            error.__dict__['confirmed_receipts'] = None
        raise


class ExecutionState(StrEnum):
    """Observable state of a completed execution call."""

    NOOP = 'NOOP'
    DEFERRED = 'DEFERRED'
    FINISHED = 'FINISHED'
    PARTIAL = 'PARTIAL'


@dataclass(frozen=True)
class ExecutionOutcome:
    """Safe terminal facts and accepted fills in execution order."""

    state: ExecutionState
    stage: str
    reason: str
    receipts: tuple[ExecutionReceipt, ...] = ()

    @classmethod
    def stopped(cls, stage: str, reason: str,
                receipts: Sequence[ExecutionReceipt] = ()) -> 'ExecutionOutcome':
        """A guard stops an empty pass or one that already had confirmed effects."""
        state = ExecutionState.PARTIAL if receipts else ExecutionState.DEFERRED
        return cls(state, stage, reason, tuple(receipts))


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


def _submit(account_id: str, intent: OrderIntent, executor: OrderExecutor, *,
            wait: bool = False, pass_receipts: _PassReceipts | None = None) -> ExecutionReceipt:
    LOGGER.info('Submitting order: account=%s uid=%s side=%s lots=%d',
                account_id, intent.uid, intent.side, intent.lots)
    try:
        receipt = executor.submit_order(account_id, intent)
    except OrderExecutionError:
        LOGGER.error('Stopping pass: unknown order result UID %s side %s', intent.uid, intent.side)
        raise
    except TimeoutError as error:
        if pass_receipts is not None:
            pass_receipts.submit_timeout = error
        raise
    if (wait or intent.side == 'SELL') and _receipt_matches(receipt, intent):
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
    print_order_money(receipt)
    return receipt


def _fresh(account_id: str, data: ExecutionData, uids: Sequence[str], *, valuation=None
           ) -> tuple[ExecutionSnapshot, dict[str, TradeRules]]:
    try:
        snapshot = (data.get_destination(account_id) if valuation is None
                    else data.get_destination(account_id, valuation=valuation))
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


def _fresh_money(plan: OrderPlan, ownership: dict[tuple[int, ...], dict[str, Decimal]],
                 snapshot: ExecutionSnapshot, rules: dict[str, TradeRules],
                 receipts: Sequence[ExecutionReceipt]) -> dict[tuple[int, ...], Decimal]:
    sales = tuple((intent.pieces, receipt.cash.get('rub'))
                  for intent, receipt in zip(plan.sells, receipts))
    money, restricted, no_credit = allocate_rub_after_sales(
        plan.strategy, ownership, plan.marks, plan.money,
        cash_bound(snapshot, rules), sales)
    for index in restricted:
        LOGGER.info('Defer scoped purchases: sale cash attribution unavailable UID %s',
                    plan.sells[index].uid)
    if no_credit:
        LOGGER.info('Defer scoped purchases: fresh availability has no permitted funded lot')
    return money


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


def _finished_outcome(plan: OrderPlan, ownership: dict[tuple[int, ...], dict[str, Decimal]],
                      rules: dict[str, TradeRules], receipts: Sequence[ExecutionReceipt], *,
                      native: bool = False) -> ExecutionOutcome:
    # On a normal exit, accepted security sales precede all other fills.
    sold = {intent.uid: Decimal(receipt.lots_executed * intent.lot_size)
            for intent, receipt in zip(plan.sells, receipts)}
    sell_deltas = plan.initial_sell_deltas(native=native)
    if (any(delta > sold.get(uid, ZERO) for uid, delta in sell_deltas.items())
            or has_buy_delta(plan.strategy, ownership, rules, native=native)):
        return ExecutionOutcome(ExecutionState.PARTIAL, 'finished', 'remaining_trade_delta',
                                tuple(receipts))
    if receipts:
        return ExecutionOutcome(ExecutionState.FINISHED, 'finished', 'queue_completed',
                                tuple(receipts))
    return ExecutionOutcome(ExecutionState.NOOP, 'finished', 'no_execution')


def _execution_result(plan: OrderPlan, outcome: ExecutionOutcome, *,
                      log_stage: str | None = None) -> ExecutionOutcome:
    sells = sum(receipt.side == 'SELL' for receipt in outcome.receipts)
    LOGGER.info('Execution result: stage=%s confirmed_sells=%d confirmed_buys=%d '
                'planned_sells=%d planned_buys=%d', log_stage or outcome.stage, sells,
                len(outcome.receipts) - sells, len(plan.sells), len(plan.buys))
    return outcome


def execute_plan(account_id: str, plan: OrderPlan, data: ExecutionData,
                 executor: OrderExecutor) -> ExecutionOutcome:
    """Sales confirmed individually; fresh positions/caps constrain a new BUY plan."""
    with _record_confirmed_receipts() as pass_receipts:
        return _execute_plan(account_id, plan, data, executor, pass_receipts)


def _execute_plan(account_id: str, plan: OrderPlan, data: ExecutionData,
                  executor: OrderExecutor, pass_receipts: _PassReceipts) -> ExecutionOutcome:
    # pylint: disable=too-many-locals
    validate_plan(plan.strategy)
    for intent in (*plan.sells, *plan.buys):
        validate_intent(intent)
    if not ready(plan.snapshot, stage='before_sales'):
        outcome = ExecutionOutcome.stopped('before_sales', 'snapshot_not_ready')
        return _execution_result(plan, outcome)
    receipts = pass_receipts.accepted
    ownership = {path: dict(values) for path, values in plan.ownership.items()}
    for intent in plan.sells:
        receipts.append(_submit(account_id, intent, executor, pass_receipts=pass_receipts))
        for path, quantity in intent.pieces.items():
            ownership[path][intent.uid] = difference(ownership[path][intent.uid], quantity)
    uids = sorted(set(plan.rules) | set(plan.strategy.target.quantities))
    snapshot, rules = ((plan.snapshot, plan.rules) if not plan.sells
                       else _wait_for_settlement(account_id, data, uids, ownership, 'after_sales'))
    reason = _buy_snapshot_blocker(snapshot, ownership)
    if reason is not None:
        outcome = (ExecutionOutcome.stopped('after_sales', 'settlement_timeout', receipts)
                   if plan.sells else ExecutionOutcome.stopped('before_buys', reason))
        return _execution_result(plan, outcome, log_stage='before_buys')
    money = _fresh_money(plan, ownership, snapshot, rules, receipts)
    buys = purchase_intents(plan.strategy, snapshot, ownership, plan.marks, rules, money)
    submitted = set()
    while buys:
        intent = buys[0]
        receipts.append(_submit(account_id, intent, executor, pass_receipts=pass_receipts))
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
            return _execution_result(
                plan, ExecutionOutcome.stopped('after_buy', 'settlement_timeout', receipts),
                log_stage='between_buys')
        remaining_rules = {uid: rule for uid, rule in rules.items() if uid not in submitted}
        buys = purchase_intents(
            plan.strategy, snapshot, ownership, plan.marks, remaining_rules, money)
    return _execution_result(plan, _finished_outcome(plan, ownership, rules, receipts))


def _buy_snapshot_blocker(snapshot: ExecutionSnapshot,
                          ownership: dict[tuple[int, ...], dict[str, Decimal]], *,
                          log_stage: str = 'before_buys') -> str | None:
    if not ready(snapshot, stage=log_stage):
        return 'snapshot_not_ready'
    if not _positions_match(snapshot, ownership):
        return 'positions_mismatch'
    return None
