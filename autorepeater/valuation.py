"""Pass-local RUB marks, retaining original broker prices and native settlement units."""
from dataclasses import dataclass, replace
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from autorepeater.cash_facts import MoneyAmount, SecurityQuote, currency_code
from autorepeater.fx import FxSnapshot
from autorepeater.fx_data import FxData
from autorepeater.strategy_data import (
    InstrumentType, PortfolioSnapshot, PriceQuote, StrategyData,
)


class SecurityData(Protocol):
    """Read native security books and independently identify portfolio currency UIDs."""

    def get_security_quote(self, uid: str) -> SecurityQuote:
        """Return money per piece or explicit nominal-percent and ACI units."""

    def get_cash_currency(self, uid: str) -> str:
        """Return actual ISO currency from currency metadata, never current_price.currency."""


@dataclass(frozen=True)
class ValuationSources:
    """Optional read-only capability of an adapter, independent of the StrategyData port."""
    fx: FxData
    securities: SecurityData


class Valuation:
    """One immutable FX snapshot and one native book observation per UID for a pass."""

    def __init__(self, fx: FxSnapshot, data: SecurityData, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.fx = fx
        self._data = data
        self._prices: dict[str, MoneyAmount] = {}
        self._cash: dict[str, str] = {}
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def begin_pass(self) -> None:
        """Reject expired FX before reading/planning and discard books from a previous pass."""
        self.fx.validate_fresh(self._clock())
        self._prices.clear()
        self._cash.clear()

    def validate_fresh(self) -> None:
        """No effect may rely on an FX observation that expired during planning."""
        self.fx.validate_fresh(self._clock())

    def security(self, uid: str) -> MoneyAmount:
        """Midpoint in settlement money; the FX midpoint is a distinct observation."""
        if uid not in self._prices:
            self._prices[uid] = self._data.get_security_quote(uid).estimate_per_piece()
        return self._prices[uid]

    def to_rub(self, amount: MoneyAmount) -> MoneyAmount:
        """One conversion boundary also lets runtime discover the required currencies."""
        return self.fx.to_rub(amount)

    def refresh_books(self) -> None:
        """Read new native prices after effects without rebuilding targets or ownership."""
        self._prices.clear()

    def portfolio(self, portfolio: PortfolioSnapshot) -> PortfolioSnapshot:
        """Enrich, never overwrite broker fields; cash enters capital exactly once."""
        positions = []
        for entry in portfolio.positions:
            currency_code(entry.currency)
            if entry.instrument_type == InstrumentType.CURRENCY:
                if entry.uid not in self._cash:
                    self._cash[entry.uid] = currency_code(self._data.get_cash_currency(entry.uid))
                currency = self._cash[entry.uid]
                cash = MoneyAmount(currency, entry.quantity)
                entry = replace(entry, native_cash=cash, native_price=Decimal(1),
                                settlement_currency=currency,
                                rub_price=self.to_rub(MoneyAmount(currency, Decimal(1))).amount)
            else:
                native = self.security(entry.uid)
                entry = replace(entry, native_price=native.amount,
                                settlement_currency=native.currency,
                                rub_price=self.to_rub(native).amount)
            positions.append(entry)
        return PortfolioSnapshot(tuple(positions))

    def strategy_data(self, data: StrategyData) -> StrategyData:
        """Expose the established strategy port with explicit marks and native observations."""
        return ValuedStrategyData(data, self)


class ValuedStrategyData:
    """Read-only decorator; all leaves and destination share the same Valuation."""

    def __init__(self, data: StrategyData, valuation: Valuation) -> None:
        self._data = data
        self._valuation = valuation

    def begin_snapshot(self):
        """The enclosing pass owns the immutable market observations."""
        self._data.begin_snapshot()

    def get_portfolio(self, account_id):
        """Preserve all original broker position fields."""
        return self._valuation.portfolio(self._data.get_portfolio(account_id))

    def get_instrument(self, uid):
        """Metadata settlement currency remains independent of valuation currency."""
        return self._data.get_instrument(uid)

    def find_instruments(self, query):
        """Keep catalog order and API selection unchanged."""
        return self._data.find_instruments(query)

    def get_last_prices(self, uids):
        """The valuation path uses books; a last trade is not a midpoint substitute."""
        result = []
        for uid in uids:
            native = self._valuation.security(uid)
            result.append(PriceQuote(uid, self._valuation.to_rub(native).amount,
                                     None, native.amount, native.currency))
        return result

    def position_events(self, account_ids):
        """Events retain their existing blocking contract."""
        return self._data.position_events(account_ids)
