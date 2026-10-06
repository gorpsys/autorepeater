"""Shared portfolio reading and the strategy target contract."""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class DisplayPosition(Protocol):  # pylint: disable=too-few-public-methods
    """SDK-shaped fields needed by the legacy account display, without SDK imports."""
    instrument_type: str
    instrument_uid: str


class DisplayPortfolio(Protocol):  # pylint: disable=too-few-public-methods
    """Read-only view of positions shown by the account display."""
    positions: Sequence[DisplayPosition]


class PortfolioReader(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal legacy portfolio-read service used by reporting."""

    def get_portfolio(self, *, account_id: str) -> DisplayPortfolio:
        """Read positions without changing them."""


class PortfolioClient(Protocol):  # pylint: disable=too-few-public-methods
    """Client surface required by the legacy reporting helper."""
    operations: PortfolioReader


@dataclass
class TargetPortfolio:
    """Full target in units, with per-unit estimates used for the threshold."""
    quantities: dict[str, Decimal]
    prices: dict[str, Decimal]
    empty_reason: str | None = None


def validate_target(target: TargetPortfolio) -> None:
    """Validate target maps, quantities, and per-unit estimates without coercion."""
    if not isinstance(target.quantities, dict):
        raise ValueError('target quantities must be a dict')
    if not isinstance(target.prices, dict):
        raise ValueError('target prices must be a dict')
    if target.empty_reason is not None:
        if not isinstance(target.empty_reason, str):
            raise ValueError('target empty_reason must be str or None')
        if target.quantities:
            raise ValueError('target empty_reason must be absent for nonempty quantities')
    for uid, quantity in target.quantities.items():
        if not isinstance(uid, str) or not uid:
            raise ValueError(f'invalid target quantity UID: {uid}')
        if not isinstance(quantity, Decimal) or not quantity.is_finite():
            raise ValueError(f'invalid target quantity for UID: {uid}')
        price = target.prices.get(uid)
        if not isinstance(price, Decimal) or not price.is_finite():
            raise ValueError(f'invalid target price for UID: {uid}')


def get_portfolio(client: PortfolioClient, account_id: str) -> DisplayPortfolio:
    """Load one portfolio without filtering positions or reporting it."""
    return client.operations.get_portfolio(account_id=account_id)
