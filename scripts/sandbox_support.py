"""Real setup, observation and allowlisted diagnostics for finite sandbox scenarios."""
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
import json
import logging
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

from t_tech.invest import (
    MoneyValue, OrderDirection, OrderExecutionReportStatus, OrderType,
    OrderState, PostOrderResponse, Quotation,
)
from t_tech.invest.services import Services

from autorepeater.execution_data import ExecutionSnapshot
from autorepeater.strategy_data import InstrumentInfo, InstrumentType
from autorepeater.tinvest_execution_data import TInvestExecutionData
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from scripts.sandbox_evidence import (
    AccountEvidence, MoneyEvidence, OperationEvidence,
    OrderEvidence, QuoteEvidence, TradeEvidence, evidence_json,
)
from scripts.sandbox_lifecycle import SandboxFailure, SandboxLifecycle, safe_call

D = Decimal
FILL = OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL
PENDING = (OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
           OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL)


def decimal_value(value: MoneyValue | Quotation) -> D:
    """Preserve SDK nanos without any intermediate float."""
    return D(value.units) + D(value.nano) / D(1000000000)


def money_fields(value: MoneyValue) -> MoneyEvidence:
    """Only public monetary fields; never SDK repr or auth/transport metadata."""
    return MoneyEvidence(value.currency, format(decimal_value(value), 'f'))


def order_fields(value: PostOrderResponse | OrderState) -> OrderEvidence:
    """Allowlist facts needed to reconcile broker state and physical positions."""
    return OrderEvidence(value.order_id, value.instrument_uid, value.direction.name,
                         value.execution_report_status.name,
                         value.lots_requested, value.lots_executed)


def _api_event(message: str) -> dict[str, str | float | int] | None:
    """Parse only the wrapper's fixed safe diagnostics, never arbitrary SDK logs."""
    failure = re.fullmatch(
        r'API request failed: method=([A-Za-z_][A-Za-z_0-9]*) status=([A-Z_]+) '
        r'elapsed_seconds=([0-9]+\.[0-9]+)', message)
    if failure:
        method, status, elapsed = failure.groups()
        return {'event': 'api_failure', 'method': method, 'status': status,
                'elapsed_seconds': float(elapsed)}
    quota = re.fullmatch(
        r'API rate limit exhausted: method=([A-Za-z_][A-Za-z_0-9]*); '
        r'retrying in 10 seconds', message)
    if quota:
        return {'event': 'quota_retry', 'method': quota.group(1), 'delay_seconds': 10}
    return None


class OrderJournal(logging.Handler):
    """Observe checked app logs without replacing Runner, adapters or executor."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.events: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        api_event = _api_event(message)
        if api_event is not None:
            self.events.append({'time': record.created, **api_event})
            return
        response = re.fullmatch(
            r'Order response: request_id=(\S+) broker_order_id=(\S+) uid=(\S+) side=(BUY|SELL) '
            r'status=(\S+) requested=(\d+) executed=(\d+)', message)
        if response:
            request_id, order_id, uid, side, status, requested, executed = response.groups()
            self.events.append({'event': 'response', 'time': record.created,
                                'request_id': request_id, 'order_id': order_id, 'uid': uid,
                                'side': side, 'status': status, 'requested': int(requested),
                                'executed': int(executed)})
        elif message.startswith(('Submitting order:', 'Confirmed order:')):
            event = 'submit' if message.startswith('Submitting') else 'confirm'
            self.events.append({'event': event,
                                'time': record.created, 'message': message})


class SandboxSession:  # pylint: disable=too-many-instance-attributes
    """Shared real sandbox services; no I/O during construction."""

    def __init__(self, client: Services, lifecycle: SandboxLifecycle, *,
                 order_id_factory: Callable[[], UUID | str] | None = None,
                 monotonic_clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.client: Services = client
        self.lifecycle: SandboxLifecycle = lifecycle
        self.data: TInvestStrategyData = TInvestStrategyData(client)
        self.execution: TInvestExecutionData = TInvestExecutionData(client, self.data)
        self.order_id_factory: Callable[[], UUID | str] = (
            uuid4 if order_id_factory is None else order_id_factory)
        self.monotonic: Callable[[], float] = (
            time.monotonic if monotonic_clock is None else monotonic_clock)
        self.sleep: Callable[[float], None] = time.sleep if sleep is None else sleep
        self.configs: dict[str, object] = {}
        self.quotes: dict[str, QuoteEvidence] = {}
        self.records: list[dict[str, object]] = []
        self.scenario: str = 'unknown'

    def instrument(self, ticker: str) -> InstrumentInfo:
        """Setup resolves the same exact share/ETF ticker and full API permission."""
        candidates = [self.data.get_instrument(item.uid)
                      for item in self.data.find_instruments(ticker)
                      if item.ticker == ticker
                      and item.instrument_type in (InstrumentType.SHARE, InstrumentType.ETF)]
        available = [item for item in candidates if item.api_trade_available is True]
        if not available:
            raise SandboxFailure('required setup ticker unavailable: ' + ticker)
        instrument = available[0]
        quotes = self.data.get_last_prices([instrument.uid])
        valid_quote = (len(quotes) == 1 and quotes[0].uid == instrument.uid and quotes[0].price > 0)
        valid_lot = (not isinstance(instrument.lot, bool) and isinstance(instrument.lot, int)
                     and instrument.lot > 0)
        if not valid_quote or instrument.currency.lower() != 'rub' or not valid_lot:
            raise SandboxFailure('invalid setup instrument or quote')
        self.quotes[ticker] = QuoteEvidence(instrument.uid, instrument.lot,
                                            format(quotes[0].price, 'f'),
                                            (quotes[0].time.isoformat()
                                             if quotes[0].time is not None else None))
        return instrument

    def state(self, account_id: str) -> ExecutionSnapshot:
        """Fresh availability includes own cash, physical positions and active orders."""
        return safe_call(self.execution.get_destination, account_id=account_id)

    def pay_in(self, account_id: str, amount: D) -> None:
        """Pay-in has no idempotency key; never automatically repeat an uncertain mutation."""
        if not isinstance(amount, D) or not amount.is_finite() or amount <= 0:
            raise ValueError('pay-in must be a positive finite Decimal')
        units = int(amount)
        nano = int((amount - units) * D(1000000000))
        if D(units) + D(nano) / D(1000000000) != amount:
            raise ValueError('pay-in exceeds nano precision')
        safe_call(self.client.sandbox.sandbox_pay_in, account_id=account_id,
                  amount=MoneyValue(currency='rub', units=units, nano=nano))
        self.records.append({'event': 'pay_in', 'account_id': account_id,
                             'amount': format(amount, 'f'), 'time': time.time()})

    def wait_positions(self, account_id: str, quantities: dict[str, D]) -> ExecutionSnapshot:
        """Setup waits for a fresh exact physical snapshot before entering the app."""
        deadline = self.monotonic() + 30
        while True:
            state = self.state(account_id)
            if state.limits_ready and not state.active_orders and state.quantities == quantities:
                return state
            if self.monotonic() >= deadline:
                raise SandboxFailure('fresh positions did not settle within 30 seconds')
            self.sleep(2)

    def buy_setup(self, account_id: str, instrument: InstrumentInfo, lots: int) -> None:
        """One stable request, checked real broker FILL, then fresh physical positions."""
        if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
            raise ValueError('setup requires positive integer lots')
        before = self.state(account_id).quantities
        request_id = str(self.order_id_factory())
        self.records.append({'event': 'setup_submit', 'request_id': request_id,
                             'account_id': account_id, 'uid': instrument.uid, 'lots': lots,
                             'time': time.time()})
        response = safe_call(self.client.orders.post_order, account_id=account_id,
                             instrument_id=instrument.uid, quantity=lots,
                             direction=OrderDirection.ORDER_DIRECTION_BUY,
                             order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=request_id)
        deadline = self.monotonic() + 30
        broker_id = response.order_id
        while True:
            self.check_order(response, instrument.uid, lots, OrderDirection.ORDER_DIRECTION_BUY,
                             broker_id)
            self.records.append({'event': 'setup_response', 'request_id': request_id,
                                 'time': time.time(), **asdict(order_fields(response))})
            if response.execution_report_status == FILL:
                break
            if response.execution_report_status not in PENDING or self.monotonic() >= deadline:
                raise SandboxFailure('setup order not fully filled')
            self.sleep(2)
            response = safe_call(self.client.orders.get_order_state,
                                 account_id=account_id, order_id=broker_id)
        state = safe_call(self.client.orders.get_order_state,
                          account_id=account_id, order_id=broker_id)
        self.check_order(state, instrument.uid, lots, OrderDirection.ORDER_DIRECTION_BUY, broker_id)
        if state.execution_report_status != FILL:
            raise SandboxFailure('setup broker state is not FILL')
        expected = dict(before)
        expected[instrument.uid] = expected.get(instrument.uid, D(0)) + lots * instrument.lot
        self.wait_positions(account_id, expected)

    @staticmethod
    def check_order(response: PostOrderResponse | OrderState, uid: str, lots: int,
                    direction: OrderDirection, broker_id: str) -> None:
        """Independent broker identity, enums and quantities, never intent fallbacks."""
        identity = (isinstance(broker_id, str) and bool(broker_id)
                    and response.order_id == broker_id
                    and response.instrument_uid == uid and response.direction == direction)
        enums = (isinstance(response.direction, OrderDirection)
                 and isinstance(response.execution_report_status, OrderExecutionReportStatus))
        counts = (all(not isinstance(value, bool) and isinstance(value, int)
                      for value in (response.lots_requested, response.lots_executed))
                  and 0 <= response.lots_executed <= response.lots_requested == lots
                  and (response.execution_report_status != FILL or response.lots_executed == lots))
        if not identity or not enums or not counts:
            raise SandboxFailure('broker order identity or lot facts mismatch')

    def list_operations(self, account_id: str) -> list[OperationEvidence]:
        """New disposable accounts have a bounded history; request the complete run window."""
        response = safe_call(self.client.operations.get_operations, account_id=account_id)
        return [
            OperationEvidence(
                item.id, item.instrument_uid, item.figi, item.operation_type.name,
                item.state.name, item.quantity, item.quantity_rest, item.date.isoformat(),
                money_fields(item.payment), money_fields(item.price),
                tuple(TradeEvidence(trade.trade_id, trade.quantity, trade.date_time.isoformat(),
                                    money_fields(trade.price)) for trade in item.trades))
            for item in response.operations
        ]

    def diagnostic(self, account_id: str) -> AccountEvidence:
        """Explicit safe fields only; snapshot failure is handled per account by the writer."""
        state = self.state(account_id)
        orders = safe_call(self.client.orders.get_orders, account_id=account_id)
        return AccountEvidence(
            format(state.budget, 'f'), state.limits_ready,
            {uid: format(value, 'f') for uid, value in state.quantities.items()},
            {uid: format(value, 'f') for uid, value in state.marks.items()},
            {currency: format(value, 'f') for currency, value in state.available_cash.items()},
            tuple(order_fields(item) for item in orders.orders),
            tuple(self.list_operations(account_id)))


def write_failure_dump(session: SandboxSession, path: Path) -> None:
    """A failed account read cannot suppress other diagnostics or caller's finally cleanup."""
    accounts: dict[str, AccountEvidence | dict[str, str]] = {}
    for account_id in session.lifecycle.created:
        try:
            accounts[account_id] = session.diagnostic(account_id)
        except Exception as error:  # pylint: disable=broad-exception-caught
            accounts[account_id] = {'error': type(error).__name__}
    document = {'time': datetime.now(timezone.utc).isoformat(), 'scenario': session.scenario,
                'configs': session.configs, 'quotes': session.quotes,
                'events': session.records, 'accounts': accounts}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=True, indent=2, default=evidence_json),
                    encoding='utf-8')
