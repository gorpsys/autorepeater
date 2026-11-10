"""Loaded account identity and the public metadata lookup error."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DisplayAccount:
    """Already loaded account identity."""
    id: str
    name: str


class GetInstrumentException(Exception):
    """Instrument not found uniquely by instrument_id."""
