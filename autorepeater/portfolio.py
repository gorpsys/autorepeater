"""Shared portfolio reading and the strategy target contract."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class TargetPortfolio:
    """Full target in units, with per-unit estimates used for the threshold."""
    quantities: dict[str, Decimal]
    prices: dict[str, Decimal]


def validate_target(target):
    """Require a finite Decimal price for every target UID, even a zero holding."""
    for uid in target.quantities:
        price = target.prices.get(uid)
        if not isinstance(price, Decimal) or not price.is_finite():
            raise ValueError(f'invalid target price for UID: {uid}')


def get_portfolio(client, account_id):
    """Load one portfolio without filtering positions or reporting it."""
    return client.operations.get_portfolio(account_id=account_id)
