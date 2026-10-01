"""Shared arithmetic for a leaf strategy's configured reserve."""
from decimal import Decimal


def available_budget(budget, reserve):
    """Subtract the configured fraction before any target arithmetic."""
    return budget * (Decimal('1') - reserve)
