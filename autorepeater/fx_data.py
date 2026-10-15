"""Minimal direct-pair monetary contract; no SDK, route discovery or submission."""
from dataclasses import dataclass
from decimal import Decimal

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.execution_data import TradeRules
from autorepeater.strategy_plan import exact_product, exact_sum, finite_decimal


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
        return exact_product(exact_sum((self.bid, self.ask)), Decimal('0.5')) / self.nominal

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
        total = exact_product(self.base_amount(lots).amount, price) / self.nominal
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
