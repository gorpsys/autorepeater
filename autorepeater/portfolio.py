"""Shared portfolio reading and the strategy target contract."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class TargetPortfolio:
    """Full target in units, with per-unit estimates used for the threshold."""
    quantities: dict[str, Decimal]
    prices: dict[str, Decimal]


def validate_target(target):
    """Validate target maps, quantities, and per-unit estimates without coercion."""
    if not isinstance(target.quantities, dict):
        raise ValueError('target quantities must be a dict')
    if not isinstance(target.prices, dict):
        raise ValueError('target prices must be a dict')
    for uid, quantity in target.quantities.items():
        if not isinstance(uid, str) or not uid:
            raise ValueError(f'invalid target quantity UID: {uid}')
        if not isinstance(quantity, Decimal) or not quantity.is_finite():
            raise ValueError(f'invalid target quantity for UID: {uid}')
        price = target.prices.get(uid)
        if not isinstance(price, Decimal) or not price.is_finite():
            raise ValueError(f'invalid target price for UID: {uid}')


def get_portfolio(client, account_id):
    """Load one portfolio without filtering positions or reporting it."""
    return client.operations.get_portfolio(account_id=account_id)
