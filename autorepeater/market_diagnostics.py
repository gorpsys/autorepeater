"""Bounded public market facts, never SDK reprs, names or transport details."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import re

from autorepeater.logging_config import logger

_CONTEXT = ContextVar('market_diagnostic_context', default={})
_KINDS = frozenset(('share', 'etf', 'bond', 'currency', 'other'))


def safe_symbol(value: object, *, limit: int = 64) -> str:
    """Reject malformed/oversized scalars rather than stringify arbitrary objects."""
    if value is None:
        return 'missing'
    if not isinstance(value, str):
        return 'invalid'
    if not value:
        return 'missing'
    if len(value) > limit or re.fullmatch(r'[A-Za-z0-9_.:/\[\]=+\-]+', value) is None:
        return 'invalid'
    return value


def set_diagnostic_context(*, stage: str, scenario: str, phase: str) -> None:
    """The test framework supplies context; production needs no test dependency."""
    _CONTEXT.set({'stage': safe_symbol(stage), 'scenario': safe_symbol(scenario, limit=240),
                  'phase': safe_symbol(phase)})


def diagnostic_context() -> dict[str, str]:
    """Return a copy so an observation cannot mutate the next diagnostic."""
    return dict(_CONTEXT.get())


def instrument_facts(item: object, *, loaded: bool = True) -> dict[str, object]:
    """Only explicit identifiers, category, currency and API permission are public."""
    kind = getattr(item, 'instrument_type', None)
    flag = getattr(item, 'api_trade_available', None)
    currency = getattr(item, 'currency', None)
    return {
        'uid': safe_symbol(getattr(item, 'uid', None)),
        'ticker': safe_symbol(getattr(item, 'ticker', None), limit=32),
        'class_code': safe_symbol(getattr(item, 'class_code', None), limit=16),
        'kind': (kind if isinstance(kind, str) and kind in _KINDS
                 else 'missing' if kind is None or isinstance(kind, str) and not kind
                 else 'invalid'),
        'currency': ('not_loaded' if not loaded else 'missing'
                     if currency is None or isinstance(currency, str) and not currency
                     else currency.lower() if isinstance(currency, str)
                     and re.fullmatch('[A-Za-z]{3}', currency) else 'invalid'),
        'api_available': ('not_loaded' if not loaded else flag if isinstance(flag, bool)
                          else 'missing' if flag is None else 'invalid'),
    }


def _time_category(value: object) -> str | None:
    if value is None:
        return 'missing'
    if not isinstance(value, datetime):
        return 'invalid'
    if value.tzinfo is None or value.utcoffset() is None:
        return 'naive'
    return None


def _time_fact(value: object) -> str:
    category = _time_category(value)
    if category in ('missing', 'invalid'):
        return category
    return (value.isoformat() if category == 'naive'
            else value.astimezone(timezone.utc).isoformat())


def warn_book_failure(expected_uid: str, received_uid: object,
                      market_time: object, observed: object) -> None:
    """Explain the existing UID/time guards without changing their decision or error."""
    reasons = []
    if received_uid != expected_uid:
        reasons.append('uid_mismatch')
    market_category, observed_category = _time_category(market_time), _time_category(observed)
    for prefix, category in (('market_time', market_category),
                             ('observed_time', observed_category)):
        if category is not None:
            reasons.append(prefix + '_' + category)
    age = 'missing' if market_category == 'missing' else 'unavailable'
    if market_category is None and observed_category is None:
        delta = observed - market_time
        age = delta.total_seconds()
        if age < 0:
            reasons.append('market_time_future')
        elif age > 60:
            reasons.append('market_time_stale')
    if reasons:
        logger.warning('Market diagnostic: %s', json.dumps({
            **diagnostic_context(), 'event': 'security_book_rejected', 'reasons': reasons,
            'expected_uid': safe_symbol(expected_uid), 'received_uid': safe_symbol(received_uid),
            'market_time': _time_fact(market_time), 'observed_utc': _time_fact(observed),
            'age_seconds': age, 'freshness_limit_seconds': 60,
        }))


def warn_metadata_failure(uid: str, item: object, reason: str) -> None:
    """Supplement the adapter's original validation exception with selected facts."""
    logger.warning('Market diagnostic: %s', json.dumps({
        **diagnostic_context(), 'event': 'instrument_metadata_rejected', 'reason': reason,
        'expected_uid': safe_symbol(uid), **instrument_facts(item),
    }))
