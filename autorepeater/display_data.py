"""Frozen values for the legacy account display; no broker-shaped objects."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DisplayAccount:
    """Already loaded account identity."""
    id: str
    name: str


@dataclass(frozen=True)
class DisplayPosition:
    """Loaded display values; raw-only compatibility needs no numerical fields."""
    uid: str | None
    instrument_type: str
    currency: str | None = None
    current_price: Decimal | None = None
    quantity: Decimal | None = None
    instrument_label: str | None = None
    diagnostic_text: str = ''


class GetInstrumentException(Exception):
    """Instrument not found uniquely by instrument_id."""
