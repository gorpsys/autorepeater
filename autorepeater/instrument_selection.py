"""Pure settlement preference for already loaded, API-available instruments."""
from collections.abc import Sequence

from autorepeater.cash_facts import currency_code
from autorepeater.strategy_data import InstrumentInfo


def prefer_instrument_currency(candidates: Sequence[InstrumentInfo],
                               preferred_currency: str = 'rub') -> list[InstrumentInfo]:
    """Narrow to matching currency, retaining the original set when no match exists.

    Callers still require a unique result and retain their own error boundary.
    Missing or malformed live currencies never acquire a default identity.
    """
    preferred = currency_code(preferred_currency)
    matches = [item for item in candidates if currency_code(item.currency) == preferred]
    return matches or list(candidates)
