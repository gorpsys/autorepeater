"""SDK-independent models and read-side contract used by strategies."""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class InstrumentType(StrEnum):
    """Stable instrument categories understood by strategy code."""

    SHARE = 'share'
    ETF = 'etf'
    CURRENCY = 'currency'
    OTHER = 'other'


@dataclass(frozen=True, slots=True)
class PortfolioEntry:
    """One portfolio position represented without an SDK object."""

    uid: str
    instrument_type: InstrumentType
    currency: str
    current_price: Decimal
    quantity: Decimal
    diagnostic_text: str


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """One ordered portfolio snapshot."""

    positions: tuple[PortfolioEntry, ...]


@dataclass(frozen=True, slots=True)
class InstrumentMatch:
    """Short instrument metadata returned by a search operation."""

    uid: str
    ticker: str
    name: str
    instrument_type: InstrumentType
    class_code: str


@dataclass(frozen=True, slots=True)
class InstrumentInfo:  # pylint: disable=too-many-instance-attributes
    """Full instrument metadata, including API permission, loaded for one UID."""

    uid: str
    ticker: str
    name: str
    instrument_type: InstrumentType
    class_code: str
    lot: int
    currency: str
    api_trade_available: bool


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """Per-unit price and its original quotation time."""

    uid: str
    price: Decimal
    time: datetime | None


@dataclass(frozen=True, slots=True)
class SecurityBlocking:
    """Blocking data needed from one security position event item."""

    blocked: int


@dataclass(frozen=True, slots=True)
class MoneyBlocking:
    """Blocking data needed from one money position event item."""

    blocked_value: Decimal


@dataclass(frozen=True, slots=True)
class PositionEvent:
    """One position or service event with ordered blocking details."""

    has_position: bool
    account_id: str
    securities: tuple[SecurityBlocking, ...]
    money: tuple[MoneyBlocking, ...]
    diagnostic_text: str


class DataAccessError(Exception):
    """Transport-level failure while reading strategy data."""


class StrategyData(Protocol):
    """Minimal read-only data port available to every strategy."""

    def get_portfolio(self, account_id: str) -> PortfolioSnapshot:
        """Return an ordered snapshot for one account."""

    def find_instruments(self, query: str) -> list[InstrumentMatch]:
        """Return ordered short metadata matching a query."""

    def get_instrument(self, uid: str) -> InstrumentInfo:
        """Return full metadata for one instrument UID."""

    def get_last_prices(self, uids: Sequence[str]) -> list[PriceQuote]:
        """Return ordered last-price records for requested UIDs."""

    def position_events(self, account_ids: Sequence[str]) -> Iterable[PositionEvent]:
        """Return a lazy stream of position and service events."""
