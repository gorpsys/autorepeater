"""Test-owned failure policy; CI receives only its explicit boolean decision."""
from datetime import datetime, timedelta
from types import TracebackType

from t_tech.invest import RequestError

from scripts.sandbox_retry_report import (
    KNOWN_ERRORS, auth_failure, transport_facts, validation_method,
)


def validation_trace(error: BaseException | None) -> TracebackType | None:
    """Only a known wrapper chain can lead to a retryable validation error."""
    visited: set[int] = set()
    while error is not None and id(error) not in visited and len(visited) < 8:
        visited.add(id(error))
        if error.__class__ is ValueError:
            break
        if type(error) not in KNOWN_ERRORS or isinstance(error, RequestError):
            return None
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    if error is None or error.__class__ is not ValueError or error.__traceback__ is None:
        return None
    trace = error.__traceback__
    while trace.tb_next is not None:
        trace = trace.tb_next
    return trace


def stale_guard(error: BaseException | None) -> bool:
    """Recognize a real stale guard using its loaded threshold, not a copied constant."""
    trace = validation_trace(error)
    if trace is None:
        return False
    frame = trace.tb_frame
    values = frame.f_locals
    method = validation_method(frame.f_code)
    try:
        if method == 'get_security_quote':
            book = values['book']
            if values['uid'] != book.instrument_uid:
                return False
            stamp, now = book.orderbook_ts, values['now']
        elif method == 'validate_fresh':
            quote, now = values['self'], values['now']
            if not isinstance(now, datetime) or now < quote.received_time:
                return False
            stamp = quote.market_time
        else:
            return False
        limit = frame.f_globals['ORDER_BOOK_MAX_AGE_SECONDS']
        return (isinstance(stamp, datetime) and isinstance(now, datetime)
                and stamp.utcoffset() is not None and now.utcoffset() is not None
                and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
                and now - stamp > timedelta(seconds=limit))
    except (KeyError, TypeError, AttributeError, OverflowError):
        return False


def may_retry(error: BaseException | None, when: str, outcome: str) -> bool:
    """Financial assertions, authentication, teardown and unknown errors are permanent."""
    if when == 'teardown' or outcome != 'failed' or auth_failure(error):
        return False
    _, status = transport_facts(error)
    return status is not None or stale_guard(error)
