"""Native money, valuation estimates and conservative bounds, never net receipts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from autorepeater.strategy_plan import exact_product, exact_sum, finite_decimal

if TYPE_CHECKING:
    from autorepeater.execution import ExecutionReceipt


def currency_code(value: str) -> str:
    """Normalize case only; never infer a currency or substitute an alias."""
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha():
        raise ValueError('invalid monetary currency')
    return value.lower()


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    """Signed units of an explicitly identified currency, independent of SDK."""
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, 'currency', currency_code(self.currency))
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise ValueError('money amount must be a finite Decimal')

    @classmethod
    def from_units_nano(cls, currency: str, units: int, nano: int) -> 'MoneyAmount':
        """Reject malformed SDK components without rounding even a large amount."""
        if (any(not isinstance(value, int) or isinstance(value, bool) for value in (units, nano))
                or abs(nano) >= 1000000000 or units * nano < 0):
            raise ValueError('invalid money units/nano')
        fraction = exact_product(Decimal(nano), Decimal('1e-9'))
        return cls(currency, exact_sum((Decimal(units), fraction)))


class SecurityQuoteUnit(Enum):
    """The order book's dimension; neither variant is a BESTPRICE order price."""
    MONEY_PER_PIECE = 'money_per_piece'
    PERCENT_NOMINAL = 'percent_nominal'


@dataclass(frozen=True, slots=True)
class SecurityQuote:
    """Native bid/ask with explicit bond nominal and ACI per piece."""
    currency: str
    bid: Decimal
    ask: Decimal
    unit: SecurityQuoteUnit
    nominal: MoneyAmount | None = None
    accrued_interest: MoneyAmount | None = None

    def estimate_per_piece(self) -> MoneyAmount:
        """Convert clean percent quotes once, then add explicit ACI once."""
        currency = currency_code(self.currency)
        finite_decimal(self.bid, 'security bid', positive=True)
        finite_decimal(self.ask, 'security ask', positive=True)
        if self.bid > self.ask or not isinstance(self.unit, SecurityQuoteUnit):
            raise ValueError('invalid security quote')
        midpoint = exact_product(exact_sum((self.bid, self.ask)), Decimal('0.5'))
        if self.unit == SecurityQuoteUnit.PERCENT_NOMINAL:
            if not _native_nonnegative(self.nominal, currency) or self.nominal.amount == 0:
                raise ValueError('bond nominal must be positive native money')
            if not _native_nonnegative(self.accrued_interest, currency):
                raise ValueError('bond ACI must be explicit native money')
            fraction = exact_product(midpoint, Decimal('0.01'))
            clean_price = exact_product(fraction, self.nominal.amount)
            midpoint = exact_sum((clean_price, self.accrued_interest.amount))
        elif self.nominal is not None or self.accrued_interest is not None:
            raise ValueError('money quote must already include any bond ACI')
        return MoneyAmount(currency, midpoint)


def _native_nonnegative(value: object, currency: str) -> bool:
    return isinstance(value, MoneyAmount) and value.currency == currency and value.amount >= 0


class FeeInclusion(Enum):
    """Field-specific evidence, never inferred from an account balance delta."""
    UNKNOWN = 'unknown'
    INCLUDED = 'included'
    SEPARATE = 'separate'


@dataclass(frozen=True, slots=True)
class OrderAmounts:  # pylint: disable=too-many-instance-attributes
    """Direct order fields; total already includes any accrued bond interest."""
    uid: str
    side: str
    order_id: str
    lots: int
    total: MoneyAmount | None
    executed_commission: MoneyAmount | None
    fee_inclusion: FeeInclusion = FeeInclusion.UNKNOWN


@dataclass(frozen=True, slots=True)
class CashBound:
    """BUY debit upper bound or SELL credit lower bound; never exact net cash."""
    order_id: str
    side: str
    amount: MoneyAmount


def order_cash_bound(receipt: ExecutionReceipt, fields: OrderAmounts) -> CashBound | None:
    """Require a complete matching FILL and native money; no history lookup."""
    # Money incompleteness must not invalidate an otherwise factual order receipt.
    if any(not isinstance(value, str) or not value for value in (fields.uid, fields.order_id)):
        return None
    identity_matches = ((receipt.uid, receipt.side, receipt.order_id)
                        == (fields.uid, fields.side, fields.order_id))
    if fields.side not in ('BUY', 'SELL') or receipt.status != 'FILL' or not identity_matches:
        return None
    if (not isinstance(fields.lots, int) or isinstance(fields.lots, bool) or fields.lots <= 0
            or any(not isinstance(value, int) or isinstance(value, bool) or value != fields.lots
                   for value in (receipt.lots_requested, receipt.lots_executed))):
        return None
    total, fee = fields.total, fields.executed_commission
    if (not isinstance(total, MoneyAmount) or total.amount < 0
            or not _native_nonnegative(fee, total.currency)
            or not isinstance(fields.fee_inclusion, FeeInclusion)):
        return None
    if fields.fee_inclusion == FeeInclusion.INCLUDED and fee.amount > total.amount:
        return None
    adjustment = Decimal(0) if fields.fee_inclusion == FeeInclusion.INCLUDED else fee.amount
    if fields.side == 'SELL':
        adjustment = adjustment.copy_negate()
    bound = max(Decimal(0), exact_sum((total.amount, adjustment)))
    return CashBound(fields.order_id, fields.side, MoneyAmount(total.currency, bound))


@dataclass(frozen=True, slots=True)
class PurchaseCashNeed:
    """A purchase plus its fee bound; reserve shortage alone is not a purchase."""
    purchase: MoneyAmount
    fee_upper: MoneyAmount

    def __post_init__(self) -> None:
        if (not isinstance(self.purchase, MoneyAmount) or self.purchase.amount < 0
                or not _native_nonnegative(self.fee_upper, self.purchase.currency)):
            raise ValueError('purchase and fee require nonnegative same-currency money')

    @property
    def debit_upper(self) -> MoneyAmount:
        """Fees consume available owner money, including their existing reserve."""
        amount = exact_sum((self.purchase.amount, self.fee_upper.amount))
        return MoneyAmount(self.purchase.currency, amount)

    def requires_exchange(self, available: MoneyAmount) -> bool:
        """Only insufficient purchase funding can justify FX; this grants no rights."""
        if not _native_nonnegative(available, self.purchase.currency):
            raise ValueError('available money must use purchase currency')
        return self.purchase.amount > 0 and available.amount < self.debit_upper.amount
