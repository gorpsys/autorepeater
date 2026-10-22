"""SDK-independent direct-pair metadata, market observations and read-only port."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Protocol

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.strategy_plan import exact_product, exact_sum, finite_decimal

ORDER_BOOK_MAX_AGE_SECONDS = 180
ORDER_BOOK_FUTURE_TOLERANCE = timedelta(seconds=1)


def market_timestamp(value: datetime) -> None:
    """A receipt clock or last-trade time cannot replace the market-data timestamp."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('FX timestamp must be timezone-aware')


def fx_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Use contract precision/rounding without losing nanos to a caller's context."""
    with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
        return numerator / denominator


@dataclass(frozen=True, slots=True)
class FxPair:  # pylint: disable=too-many-instance-attributes
    """Full currency identity; nominal base units and lot base units are distinct."""
    uid: str
    base_currency: str
    quote_currency: str
    nominal: Decimal
    lot_size: int
    api_trade_available: bool
    buy_available: bool
    sell_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, 'base_currency', currency_code(self.base_currency))
        object.__setattr__(self, 'quote_currency', currency_code(self.quote_currency))
        if (not isinstance(self.uid, str) or not self.uid
                or any(char.isspace() for char in self.uid)):
            raise ValueError('invalid FX pair UID')
        finite_decimal(self.nominal, 'FX nominal', positive=True)
        if (not isinstance(self.lot_size, int) or isinstance(self.lot_size, bool)
                or self.lot_size <= 0):
            raise ValueError('invalid FX pair lot')
        if any(not isinstance(flag, bool) for flag in (
                self.api_trade_available, self.buy_available, self.sell_available)):
            raise ValueError('FX metadata permissions must be bool')


@dataclass(frozen=True, slots=True)
class FxLotQuote:
    """RUB per nominal base units; one trading lot contains lot_size base units."""
    uid: str
    base_currency: str
    quote_currency: str
    nominal: Decimal
    lot_size: int
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, 'base_currency', currency_code(self.base_currency))
        object.__setattr__(self, 'quote_currency', currency_code(self.quote_currency))
        if (not isinstance(self.uid, str) or not self.uid or self.base_currency == 'rub'
                or self.quote_currency != 'rub'):
            raise ValueError('invalid direct RUB pair identity')
        if (not isinstance(self.lot_size, int) or isinstance(self.lot_size, bool)
                or self.lot_size <= 0):
            raise ValueError('invalid direct RUB pair lot')
        for value in (self.nominal, self.bid, self.ask):
            finite_decimal(value, 'FX quote', positive=True)
        if self.bid > self.ask:
            raise ValueError('inverted FX bid/ask')

    @property
    def midpoint_per_unit(self) -> Decimal:
        """Valuation only, independently of a security's native midpoint."""
        midpoint = exact_product(exact_sum((self.bid, self.ask)), Decimal('0.5'))
        return fx_ratio(midpoint, self.nominal)

    def base_amount(self, lots: int) -> MoneyAmount:
        """Base units, not securities and not the quote's nominal."""
        if not isinstance(lots, int) or isinstance(lots, bool) or lots <= 0:
            raise ValueError('FX lots must be a positive integer')
        return MoneyAmount(self.base_currency, Decimal(lots * self.lot_size))

    def quote_amount(self, side: str, lots: int) -> MoneyAmount:
        """Ask cost for BUY, bid proceeds for SELL; excludes fees and price guarantees."""
        if side not in ('BUY', 'SELL'):
            raise ValueError('invalid FX side')
        price = self.ask if side == 'BUY' else self.bid
        total = fx_ratio(exact_product(self.base_amount(lots).amount, price), self.nominal)
        return MoneyAmount(self.quote_currency, total)

    def can_trade(self, side: str, lots: int, rules: TradeRules,
                  available_cash: dict[str, Decimal], *, ready: bool) -> bool:
        """Caller-scoped fresh cash and own caps permit TOM without a calendar wait."""
        cost = self.quote_amount(side, lots)
        base = self.base_amount(lots)
        if (ready is not True or rules.api_trade_available is not True
                or rules.bestprice_order_available is not True or rules.lot != self.lot_size
                or currency_code(rules.currency) != self.quote_currency):
            return False
        native_currency = self.quote_currency if side == 'BUY' else self.base_currency
        if native_currency not in available_cash:
            return False
        cash = available_cash[native_currency]
        finite_decimal(cash, 'native cash')
        finite_decimal(rules.buy_money_amount, 'own FX money')
        cap = rules.buy_max_lots if side == 'BUY' else rules.sell_max_lots
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < lots:
            return False
        if side == 'BUY':
            return cost.amount <= min(cash, rules.buy_money_amount)
        return base.amount <= cash


@dataclass(frozen=True, slots=True)
class FxQuote:
    """Bid/ask observation with separate market and receipt clocks; no trading effects."""
    pair: FxPair
    bid: Decimal
    ask: Decimal
    market_time: datetime
    received_time: datetime

    def __post_init__(self) -> None:
        # Constructing the unit contract validates direct orientation and both prices.
        _ = self.lot_quote
        market_timestamp(self.market_time)
        market_timestamp(self.received_time)
        if self.market_time - self.received_time > ORDER_BOOK_FUTURE_TOLERANCE:
            raise ValueError('FX market timestamp is in the future')

    @property
    def lot_quote(self) -> FxLotQuote:
        """Use the same nominal/lot arithmetic as the monetary contract tests."""
        return FxLotQuote(self.pair.uid, self.pair.base_currency, self.pair.quote_currency,
                          self.pair.nominal, self.pair.lot_size, self.bid, self.ask)

    def validate_fresh(self, now: datetime) -> None:
        """Inclusive 120-second market age; receipt ordering remains strict."""
        market_timestamp(now)
        if now < self.received_time:
            raise ValueError('FX receipt timestamp is in the future')
        if now - self.market_time > timedelta(seconds=ORDER_BOOK_MAX_AGE_SECONDS):
            raise ValueError('stale FX order book')

    def can_trade(self, side: str, lots: int, rules: TradeRules,
                  state: ExecutionSnapshot, *, now: datetime) -> bool:
        """Fresh caller-scoped broker facts only, without reserve or calendar inference."""
        self.validate_fresh(now)
        if side not in ('BUY', 'SELL'):
            raise ValueError('invalid FX side')
        permission = self.pair.buy_available if side == 'BUY' else self.pair.sell_available
        return (self.pair.api_trade_available and permission
                and self.lot_quote.can_trade(side, lots, rules, state.available_cash,
                                             ready=state.limits_ready))


class FxData(Protocol):
    """Read-only FX port; account readiness remains the ExecutionData contract."""

    def list_pairs(self) -> list[FxPair]:
        """Read full currency metadata, without a ticker-based identity guess."""

    def get_quote(self, pair: FxPair) -> FxQuote:
        """Read a fresh two-sided order book for the exact selected pair."""

    def get_trade_rules(self, account_id: str, pair: FxPair) -> TradeRules:
        """Read API/BESTPRICE permissions and own nonmargin limits afresh."""
