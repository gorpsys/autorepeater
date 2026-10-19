"""Pass-local market wiring and confirmed native-cash execution, without SDK types."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic, sleep

from autorepeater.cash_facts import MoneyAmount, currency_code, order_cash_bound
from autorepeater.cash_pools import (
    CashPools, CashProceeds, cash_headroom, credit_cash_pools, native_allowance,
    split_cash,
)
from autorepeater.execution import (
    ExecutionReceipt, _fresh, _positions_match, _submit,
)
from autorepeater.execution_data import ExecutionData
from autorepeater.fx import FxSnapshot, select_direct_pair, validate_pair_uids
from autorepeater.fx_data import ORDER_BOOK_MAX_AGE_SECONDS, FxData
from autorepeater.fx_funding import FundingInputs, build_funding_plan
from autorepeater.logging_config import logger
from autorepeater.market_diagnostics import safe_symbol
from autorepeater.money import format_decimal
from autorepeater.order_plan import (
    OrderIntent, OrderPlan, ready, sale_intents, spending_limits, validate_rules,
)
from autorepeater.purchase_plan import ZERO, difference, funding_candidates, validate_intent
from autorepeater.reporting import print_fx_snapshot
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import exact_product, exact_sum, validate_plan
from autorepeater.valuation import SecurityData, Valuation

SETTLEMENT_TIMEOUT = 30
SETTLEMENT_RETRY_INTERVAL = 2


class MarketValuation(Valuation):
    """Discover native currencies during read-only loading; each rate is fixed once."""

    def __init__(self, market: 'ExecutionMarket') -> None:
        super().__init__(FxSnapshot({}, market.clock()), market.securities, clock=market.clock)
        self.market = market
        self._pairs = None

    def to_rub(self, amount: MoneyAmount) -> MoneyAmount:
        if amount.currency != 'rub' and amount.currency not in self.fx.quotes:
            if self._pairs is None:
                self._pairs = self.market.fx.list_pairs()
            pair = select_direct_pair(self._pairs, amount.currency,
                                      pair_uid=self.market.fx_pair_uids.get(amount.currency))
            quote = self.market.fx.get_quote(pair)
            if quote.pair != pair:
                raise ValueError('FX quote identity differs from selected pair')
            self.fx = FxSnapshot({**self.fx.quotes, amount.currency: quote}, self.market.clock())
        return super().to_rub(amount)


@dataclass
class _Ledger:
    """Conservative attributed cash and immutable-pass remaining security ceilings."""
    cash: CashPools
    ownership: dict
    limits: dict

    def change(self, debits, cash_credits) -> bool:
        """Apply proven bounds to their original owners; never redistribute a shortfall."""
        balances = {path: dict(money) for path, money in self.cash.balances.items()}
        for path, money in cash_credits.items():
            for currency, amount in money.items():
                values = balances.setdefault(path, {})
                values[currency] = exact_sum((values.get(currency, ZERO), amount))
        for path, money in debits.items():
            for currency, amount in money.items():
                values = balances.setdefault(path, {})
                if amount > values.get(currency, ZERO):
                    return False
                values[currency] = difference(values.get(currency, ZERO), amount)
        self.cash = CashPools(balances, self.cash.protected)
        return True

    def fits(self, snapshot) -> bool:
        """Global availability can constrain ownership, never establish it."""
        currencies = {code for money in (*self.cash.balances.values(), self.cash.protected)
                      for code in money}
        return all(self.cash.total(code) <= snapshot.available_cash.get(code, ZERO)
                   for code in currencies)


class ExecutionMarket:
    """The same explicit read-only ports serve CLI, cloud and offline execution."""

    def __init__(self, fx: FxData, securities: SecurityData, *,
                 clock: Callable[[], datetime] | None = None,
                 fx_pair_uids: Mapping[str, str] | None = None) -> None:
        self.fx = fx
        self.securities = securities
        self.fx_pair_uids = validate_pair_uids({} if fx_pair_uids is None else fx_pair_uids)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def valuation(self) -> Valuation:
        """No IO on construction; required rates are assembled before planning effects."""
        return MarketValuation(self)

    def funding_inputs(self, account_id, value, uids, *, trading=True) -> FundingInputs:
        """Native books and actual FX caps, with no GetOrderPrice or history dependency."""
        prices = {uid: value.security(uid) for uid in uids}
        for price in prices.values():
            value.to_rub(price)
        fx_rules = {quote.pair.uid: self.fx.get_trade_rules(account_id, quote.pair)
                    for quote in value.fx.quotes.values()} if trading else {}
        return FundingInputs(native_prices=prices, fx_rules=fx_rules)

    def _fresh_inputs(self, account_id, value, uids, *, trading=True):
        value.refresh_books()
        prices = {uid: value.security(uid) for uid in uids}
        quotes = {}
        for currency, old in value.fx.quotes.items():
            quote = self.fx.get_quote(old.pair)
            if quote.pair != old.pair:
                raise ValueError('execution FX pair changed during pass')
            quotes[currency] = quote
        fx = FxSnapshot(quotes, self.clock())
        own = {quote.pair.uid: self.fx.get_trade_rules(account_id, quote.pair)
               for quote in quotes.values()} if trading else {}
        validate_rules(own)
        print_fx_snapshot(fx, stage='execution')
        return fx, FundingInputs(native_prices=prices, fx_rules=own)

    @staticmethod
    def needs_trading(plan, snapshot, ownership, marks, own):
        """Conditional zero-cap demands count; a pure no-op needs no currency status/caps."""
        demands = funding_candidates(plan, ownership, snapshot.quantities, marks, own,
                                     spending_limits(plan, ownership, marks))
        return bool(demands or sale_intents(plan, snapshot, ownership, own, native=True))

    @staticmethod
    def _preflight(plan, fx, own) -> bool:
        needed = {}
        for intent in (*plan.sells, *plan.funding.demands):
            currency = currency_code(plan.rules[intent.uid].currency)
            if currency != 'rub':
                needed.setdefault(currency, set()).add(intent.side)
        for intent in plan.funding.exchanges:
            currency = next(code for code, quote in fx.quotes.items()
                            if quote.pair.uid == intent.uid)
            needed.setdefault(currency, set()).add(intent.side)
        for currency, sides in needed.items():
            quote = fx.quotes[currency]
            rule = own[quote.pair.uid]
            sides_available = ('BUY' not in sides or quote.pair.buy_available) and (
                'SELL' not in sides or quote.pair.sell_available)
            if (not quote.pair.api_trade_available or not rule.api_trade_available
                    or not rule.bestprice_order_available or not sides_available):
                logger.info('Defer entire pass: currency trading unavailable currency=%s', currency)
                return False
        return True

    def execute(self, account_id: str, plan: OrderPlan, data: ExecutionData, executor,
                value: Valuation) -> tuple[ExecutionReceipt, ...]:
        """Preflight the whole tree, then stop on uncertainty without resubmission/rollback."""
        if plan.funding is None:
            raise ValueError('native execution requires a funding plan')
        validate_plan(plan.strategy)
        for intent in (*plan.sells, *plan.buys, *plan.funding.demands,
                       *(purchase.intent for purchase in plan.funding.purchases)):
            validate_intent(intent)
        if not ready(plan.snapshot):
            return ()
        if not plan.sells and not plan.funding.demands:
            # Valuation freshness remains independent of trading availability.
            self._fresh_inputs(account_id, value, sorted(plan.rules), trading=False)
            return ()
        return _ExecutionPass(self, account_id, plan, data, executor, value).run()


@dataclass
class _ExecutionPass:  # pylint: disable=too-many-instance-attributes
    """Finite state of one non-atomic execution; targets and ownership never restart."""
    market: ExecutionMarket
    account_id: str
    plan: OrderPlan
    data: ExecutionData
    executor: object
    value: Valuation
    terminal_reason: str = field(init=False, default='preflight_or_sales_rejected')

    def __post_init__(self):
        self.uids = sorted(self.plan.rules)
        self.ledger = _Ledger(self.plan.funding.cash, {
            path: dict(values) for path, values in self.plan.ownership.items()}, {})
        self.snapshot, self.own = self.plan.snapshot, self.plan.rules
        self.fx, self.inputs = self.market._fresh_inputs(  # pylint: disable=protected-access
            self.account_id, self.value, self.uids)
        self.receipts = []
        self.submitted = set()
        self.fx_policy = self.inputs.fx_policy

    def _preflight(self):
        if any(self.market.clock() - quote.market_time > timedelta(
                seconds=ORDER_BOOK_MAX_AGE_SECONDS) for quote in self.fx.quotes.values()):
            self._market_refresh()
        self.fx.validate_fresh(self.market.clock())
        return self.market._preflight(  # pylint: disable=protected-access
            self.plan, self.fx, self.inputs.fx_rules)

    def _market_refresh(self):
        self.fx, self.inputs = self.market._fresh_inputs(  # pylint: disable=protected-access
            self.account_id, self.value, self.uids)

    def _fresh(self):
        self.snapshot, self.own = _fresh(
            self.account_id, self.data, self.uids, valuation=self.value)

    def _settled(self, currency=None):
        minimum = self.ledger.cash.total(currency) if currency is not None else ZERO
        deadline = monotonic() + SETTLEMENT_TIMEOUT
        while True:
            self._fresh()
            money_ready = currency is None or self.snapshot.available_cash.get(
                currency, ZERO) >= minimum
            if ready(self.snapshot) and _positions_match(
                    self.snapshot, self.ledger.ownership) and money_ready:
                return True
            if not _positions_match(self.snapshot, self.ledger.ownership):
                logger.info('Waiting for settlement: positions differ from fixed ownership')
            if not money_ready:
                logger.info('Waiting for settlement: owned cash exceeds fresh availability')
            remaining = deadline - monotonic()
            if remaining <= 0:
                logger.warning('Execution settlement timeout; remaining purchases deferred')
                return False
            sleep(min(SETTLEMENT_RETRY_INTERVAL, remaining))

    def run(self):
        """All-currency preflight precedes even the first independent RUB sale."""
        try:
            if not self._preflight() or not self._sales():
                return tuple(self.receipts)
            self.ledger.limits = spending_limits(
                self.plan.strategy, self.ledger.ownership, self.plan.marks)
            while self._can_continue():
                funding = self._funding()
                if not funding.purchases:
                    self.terminal_reason = 'no_funded_purchase'
                    if funding.demands:
                        logger.info('Defer remaining purchases: native cash/FX lot/own caps/budget')
                    break
                self.terminal_reason = 'submission_or_refresh_failed'
                continued = (self._exchange(funding.exchanges[0]) if funding.exchanges
                             else self._buy(funding))
                if not continued:
                    break
                self.terminal_reason = 'readiness_positions_cash_or_preflight'
                self._market_refresh()
            return tuple(self.receipts)
        finally:
            remaining = [safe_symbol(uid) for uid, target in sorted(
                self.plan.strategy.target.quantities.items()) if uid not in self.submitted
                and target > exact_sum(values.get(uid, ZERO)
                                       for values in self.ledger.ownership.values())]
            logger.info('Native execution end: terminal_reason=%s remaining_buy_uids=%s '
                        'omitted_count=%d', self.terminal_reason, remaining[:20],
                        max(0, len(remaining) - 20))

    def _can_continue(self):
        return (ready(self.snapshot) and _positions_match(self.snapshot, self.ledger.ownership)
                and self.ledger.fits(self.snapshot) and self._preflight())

    def _sales(self):
        for intent in self.plan.sells:
            if not self._preflight() or not _sale_allowed(
                    intent, self.snapshot, self.own, self.plan.rules):
                return False
            receipt = _submit(self.account_id, intent, self.executor)
            self.receipts.append(receipt)
            for path, pieces in intent.pieces.items():
                self.ledger.ownership[path][intent.uid] = difference(
                    self.ledger.ownership[path][intent.uid], pieces)
            if not self._settled() or not _sale_fee(self.ledger, intent, receipt):
                return False
            self._market_refresh()
        return not self.plan.sells or _credit_sales(
            self.plan, self.ledger, self.receipts, self.snapshot)

    def _funding(self):
        marks = {uid: self.fx.to_rub(price).amount
                 for uid, price in self.inputs.native_prices.items()}
        inputs = replace(self.inputs, cash=_planning_cash(self.plan, self.ledger, marks, self.fx),
                         fx_policy=self.fx_policy)
        own = {uid: replace(rule, api_trade_available=False) if uid in self.submitted else rule
               for uid, rule in self.own.items()}
        return build_funding_plan(
            self.plan.strategy, replace(self.snapshot, fx_snapshot=self.fx),
            self.ledger.ownership, marks, own, self.ledger.limits, inputs)

    def _exchange(self, exchange):
        intent = OrderIntent(exchange.uid, exchange.side, exchange.lots,
                             exchange.lot_size, dict(exchange.base_contributions))
        self.fx.validate_fresh(self.market.clock())
        receipt = _submit(self.account_id, intent, self.executor, wait=True)
        self.receipts.append(receipt)
        self.fx_policy = self.fx_policy.after_fill(intent.uid, intent.side)
        if not _exchange_cash(self.ledger, exchange, receipt, self.fx):
            self.terminal_reason = 'fx_cash_attribution_unavailable'
            logger.info('Defer FX dependencies: cash attribution unavailable')
            self._settled()
            return False
        currency = next(code for code, quote in self.fx.quotes.items()
                        if quote.pair.uid == intent.uid) if intent.side == 'BUY' else 'rub'
        self.terminal_reason = 'fx_settlement_unconfirmed'
        return self._settled(currency)

    def _buy(self, funding):
        intent = funding.purchases[0].intent
        self.fx.validate_fresh(self.market.clock())
        receipt = _submit(self.account_id, intent, self.executor)
        self.receipts.append(receipt)
        self.submitted.add(intent.uid)
        if not _purchase_cash(self.ledger, intent, receipt, self.plan.marks,
                              currency_code(self.own[intent.uid].currency)):
            self.terminal_reason = 'purchase_cash_rejected'
            return False
        for path, pieces in intent.pieces.items():
            self.ledger.ownership[path][intent.uid] = exact_sum((
                self.ledger.ownership[path].get(intent.uid, ZERO), pieces))
        self.terminal_reason = ('purchase_settlement_unconfirmed' if len(funding.purchases) > 1
                                else 'no_further_funded_purchase')
        return len(funding.purchases) > 1 and self._settled()


def _split_amount(amount, pieces):
    paths = sorted(pieces)
    parts = split_cash(amount, [pieces[path] for path in paths])
    return dict(zip(paths, parts))


def _bound(receipt):
    return order_cash_bound(receipt, receipt.amounts) if receipt.amounts is not None else None


def _exchange_cash(ledger, intent, receipt, fx):
    bound = _bound(receipt)
    if bound is None or bound.amount.currency != 'rub' or bound.amount.amount <= 0:
        return False
    currency = next(code for code, quote in fx.quotes.items() if quote.pair.uid == intent.uid)
    rub = _split_amount(bound.amount.amount, intent.base_contributions)
    base = intent.base_contributions
    debits = {path: {'rub': rub[path]} if intent.side == 'BUY' else {currency: amount}
              for path, amount in base.items()}
    cash_credits = {path: {currency: amount} if intent.side == 'BUY' else {'rub': rub[path]}
                    for path, amount in base.items()}
    if any(amount > intent.debit_limits.get(path, {}).get(code, ZERO)
           for path, money in debits.items() for code, amount in money.items()):
        logger.info('Defer FX dependencies: FX debit exceeds owner limit')
        return False
    return ledger.change(debits, cash_credits)


def _purchase_cash(ledger, intent, receipt, marks, settlement):
    bound = _bound(receipt)
    if bound is None or bound.amount.amount <= 0 or bound.amount.currency != settlement:
        logger.info('Defer further purchases: monetary debit bound unavailable')
        return False
    principal = {path: exact_product(pieces, marks[intent.uid])
                 for path, pieces in intent.pieces.items()}
    debits = {path: {bound.amount.currency: amount}
              for path, amount in _split_amount(bound.amount.amount, intent.pieces).items()}
    for path, amount in principal.items():
        reason = ('security_capacity_exceeded' if amount > ledger.limits[path]
                  else 'owner_native_cash_exceeded' if debits[path][settlement] > (
                      ledger.cash.amount(path, settlement)) else 'within_owner_bounds')
        logger.info('Native purchase bounds: uid=%s path=%s reason=%s currency=%s '
                    'planning_cost_rub=%s capacity_rub=%s principal_native=%s '
                    'commission_native=%s debit_upper_native=%s owner_cash_native=%s',
                    safe_symbol(intent.uid), path, reason, settlement,
                    format_decimal(amount), format_decimal(ledger.limits[path]),
                    format_decimal(receipt.amounts.total.amount),
                    format_decimal(receipt.amounts.executed_commission.amount),
                    format_decimal(debits[path][settlement]),
                    format_decimal(ledger.cash.amount(path, settlement)))
    if any(amount > ledger.limits[path] for path, amount in principal.items()):
        return False
    if not ledger.change(debits, {}):
        return False
    for path, amount in principal.items():
        ledger.limits[path] = difference(ledger.limits[path], amount)
    return True


def _sale_fee(ledger, intent, receipt):
    if not receipt.cash and receipt.amounts is not None:
        fee = receipt.amounts.executed_commission
        if fee is not None and fee.amount >= 0:
            debits = {path: {fee.currency: min(amount, ledger.cash.amount(path, fee.currency))}
                      for path, amount in _split_amount(fee.amount, intent.pieces).items()}
            return ledger.change(debits, {})
    return True


def _credit_sales(plan, ledger, receipts, snapshot):
    if not ledger.fits(snapshot):
        return False
    ledger.cash = credit_cash_pools(
        plan.strategy, ledger.cash, snapshot.available_cash,
        cash_headroom(plan.strategy, ledger.ownership, plan.marks), plan.snapshot.fx_snapshot,
        [CashProceeds(intent.pieces, receipt.cash or None)
         for intent, receipt in zip(plan.sells, receipts)])
    return True


def _sale_allowed(intent, snapshot, own, initial):
    rule = own.get(intent.uid)
    return (rule is not None and rule.lot == intent.lot_size
            and rule.currency == initial[intent.uid].currency
            and rule.api_trade_available and rule.bestprice_order_available
            and rule.sell_max_lots >= intent.lots
            and snapshot.available_quantities.get(intent.uid, ZERO) >= (
                Decimal(intent.lots * intent.lot_size)))


def _planning_cash(plan, ledger, marks, fx):
    """Currency gains do not increase the original owner's assigned spending budget."""
    headroom = cash_headroom(plan.strategy, ledger.ownership, marks)
    balances, protected = {}, dict(ledger.cash.protected)
    for path, money in ledger.cash.balances.items():
        codes = sorted(money)
        values = [fx.to_rub(MoneyAmount(code, money[code])).amount for code in codes]
        total = exact_sum(values)
        if total <= headroom[path]:
            balances[path] = dict(money)
            continue
        parts = proportional_split(headroom[path], values, ceilings=values) if total else values
        balances[path] = {code: native_allowance(part, code, fx)
                          for code, part in zip(codes, parts)}
        for code in codes:
            protected[code] = exact_sum((protected.get(code, ZERO), difference(
                money[code], balances[path][code])))
    return CashPools(balances, protected)
