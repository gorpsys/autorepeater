"""Bounded public market facts, never SDK reprs, names or transport details."""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re
from time import monotonic

from autorepeater.logging_config import logger
from autorepeater.fx_data import ORDER_BOOK_FUTURE_TOLERANCE, ORDER_BOOK_MAX_AGE_SECONDS

_CONTEXT = ContextVar('market_diagnostic_context', default={})
_TIMINGS = ContextVar('market_diagnostic_timings', default=None)
_OPERATION = ContextVar('market_diagnostic_operation', default=None)
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
    previous = _CONTEXT.get()
    if (stage, scenario) != (previous.get('stage'), previous.get('scenario')):
        _TIMINGS.set({} if stage == 'sandbox_e2e' else None)
    _CONTEXT.set({'stage': safe_symbol(stage), 'scenario': safe_symbol(scenario, limit=240),
                  'phase': safe_symbol(phase)})


def diagnostic_context() -> dict[str, str]:
    """Return a copy so an observation cannot mutate the next diagnostic."""
    return dict(_CONTEXT.get())


def emit_market_fact(fact: dict[str, object], *, level: int = logging.WARNING) -> dict:
    """Only callers constructing explicit public field lists may use this emitter."""
    fact = {**diagnostic_context(), **fact}
    logger.log(level, 'Market diagnostic: %s', json.dumps(fact))
    return fact


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _record_timing(operation: str, duration: float) -> None:
    timings = _TIMINGS.get()
    if timings is not None:
        fact = timings.setdefault(operation, {'count': 0, 'duration_seconds': 0.0})
        fact['count'] += 1
        fact['duration_seconds'] += duration


def timing_totals() -> dict[str, dict]:
    """One entry per named operation, independent of the number of successful reads."""
    return {key: dict(value) for key, value in (_TIMINGS.get() or {}).items()}


@contextmanager
def measure_stage(operation: str):
    """Observe preparation/load stages only while the E2E framework supplies context."""
    with diagnostic_operation(operation):
        start = monotonic() if _TIMINGS.get() is not None else None
        try:
            yield
        finally:
            if start is not None:
                _record_timing(operation, monotonic() - start)


@contextmanager
def diagnostic_operation(operation: str):
    """Attribute attempts/waits to a fixed boundary label, never request arguments."""
    token = _OPERATION.set(operation if _TIMINGS.get() is not None else None)
    try:
        yield
    finally:
        _OPERATION.reset(token)


@contextmanager
def measure_rpc_part(part: str):
    """No counters or extra clocks outside a scoped E2E boundary operation."""
    operation = _OPERATION.get()
    if operation is None:
        yield
    else:
        with measure_stage(operation + '.' + part):
            yield


def emit_timing_summary() -> None:
    """The fixture emits a compact aggregate before its existing dump/cleanup."""
    if _TIMINGS.get() is not None:
        emit_market_fact({'event': 'market_stage_timings', 'operations': timing_totals()},
                         level=logging.INFO)


class BookTiming:
    """Observational clocks never supply or replace the adapter's validation clock."""

    def __init__(self, operation: str, metadata: dict | None = None, *,
                 event: str = 'security_book_rejected') -> None:
        self.operation = operation
        self.metadata = metadata
        self.event = event
        self.facts = {'request_start_monotonic': monotonic(),
                      'request_start_utc': _time_fact(_utc_now())}
        self.response_utc = None

    def received(self) -> None:
        """Called immediately after the existing SDK read returns."""
        end = monotonic()
        self.response_utc = _utc_now()
        duration = end - self.facts['request_start_monotonic']
        self.facts.update(response_monotonic=end, response_utc=_time_fact(self.response_utc),
                          request_duration_seconds=duration)
        _record_timing(self.operation, duration)

    def checked(self) -> None:
        """Observe when the original validation timestamp is consumed."""
        checked = monotonic()
        self.facts.update(checked_monotonic=checked, checked_utc=_time_fact(_utc_now()),
                          response_to_check_seconds=checked - self.facts['response_monotonic'])

    def rejection_facts(self, market_time: object, age: object) -> dict:
        """The check age uses the original business clock; receipt age is observational."""
        receipt_age = 'unavailable'
        if _time_category(market_time) is None and _time_category(self.response_utc) is None:
            receipt_age = (self.response_utc - market_time).total_seconds()
        return {'request_timing': self.facts, 'age_at_response_seconds': receipt_age,
                'age_at_check_seconds': age,
                **({'metadata': self.metadata} if self.metadata is not None else {})}


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
                      market_time: object, observed: object, *,
                      timing: BookTiming | None = None) -> None:
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
        if delta < -ORDER_BOOK_FUTURE_TOLERANCE:
            reasons.append('market_time_future')
        elif age > ORDER_BOOK_MAX_AGE_SECONDS:
            reasons.append('market_time_stale')
    if reasons:
        emit_market_fact({
            'event': 'security_book_rejected' if timing is None else timing.event,
            'reasons': reasons,
            'expected_uid': safe_symbol(expected_uid), 'received_uid': safe_symbol(received_uid),
            'market_time': _time_fact(market_time), 'observed_utc': _time_fact(observed),
            'age_seconds': age, 'freshness_limit_seconds': ORDER_BOOK_MAX_AGE_SECONDS,
            **({} if timing is None else timing.rejection_facts(market_time, age)),
        })


def warn_metadata_failure(uid: str, item: object, reason: str) -> None:
    """Supplement the adapter's original validation exception with selected facts."""
    logger.warning('Market diagnostic: %s', json.dumps({
        **diagnostic_context(), 'event': 'instrument_metadata_rejected', 'reason': reason,
        'expected_uid': safe_symbol(uid), **instrument_facts(item),
    }))
