"""Test-owned failure policy; CI receives only its explicit boolean decision."""
from datetime import datetime, timedelta
import sys
from types import CodeType, TracebackType

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.execution import OrderExecutionError
from autorepeater.execution_data import ExecutionDataError
from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure


KNOWN_ERRORS = (
    RequestError, SandboxFailure, DataAccessError, ExecutionDataError, OrderExecutionError,
)
TRANSPORT_ROOTS = frozenset(error.__name__ for error in KNOWN_ERRORS)
TRANSIENT_STATUSES = frozenset(('DEADLINE_EXCEEDED', 'UNAVAILABLE'))


def validation_method(code: CodeType) -> str | None:
    """Match loaded method code identities without importing optional future runtime."""
    for module, owner, method in (
            ('autorepeater.tinvest_security_data', 'TInvestSecurityData', 'get_security_quote'),
            ('autorepeater.fx_data', 'FxQuote', 'validate_fresh')):
        cls = getattr(sys.modules.get(module), owner, None)
        if code is getattr(getattr(cls, method, None), '__code__', None):
            return method
    return None


def auth_failure(error: BaseException | None) -> bool:
    """Auth or incomplete bounded inspection forbids retries of every case."""
    visited: set[int] = set()
    while error is not None and id(error) not in visited and len(visited) < 8:
        visited.add(id(error))
        if isinstance(error, RequestError) and error.code in (
                StatusCode.UNAUTHENTICATED, StatusCode.PERMISSION_DENIED):
            return True
        # A full stored buffer cannot prove that no auth failure was truncated.
        if isinstance(error, SandboxFailure) and (len(error.api_facts) >= 8 or any(
                fact.get('grpc_status') in ('UNAUTHENTICATED', 'PERMISSION_DENIED')
                for fact in error.api_facts[:8])):
            return True
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return error is not None


def stored_transport(facts: tuple[dict[str, object], ...]) -> tuple[tuple[str, ...], str | None]:
    """safe_call records typed SDK facts; stop before its underlying gRPC details."""
    chain: list[str] = []
    for fact in facts[:8]:
        name = fact.get('type')
        if not isinstance(name, str) or name not in TRANSPORT_ROOTS:
            return (*chain, 'other'), None
        chain.append(name)
        if name == 'RequestError':
            status = fact.get('grpc_status')
            return tuple(chain), status if isinstance(status, str) and (
                status in TRANSIENT_STATUSES) else None
    return tuple(chain), None


def transport_facts(error: BaseException | None) -> tuple[tuple[str, ...], str | None]:
    """Known transport wrappers only; an assertion or unknown root is not retryable."""
    chain: list[str] = []
    visited: set[int] = set()
    while error is not None and id(error) not in visited and len(chain) < 8:
        visited.add(id(error))
        if type(error) not in KNOWN_ERRORS:
            return (*chain, 'other'), None
        chain.append(type(error).__name__)
        if isinstance(error, RequestError):
            status = error.code
            return tuple(chain), status.name if status in (
                StatusCode.DEADLINE_EXCEEDED, StatusCode.UNAVAILABLE) else None
        if isinstance(error, SandboxFailure) and error.api_facts:
            suffix, status_name = stored_transport(error.api_facts)
            combined = (*chain, *suffix)
            return combined[:8], status_name if len(combined) <= 8 else None
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return tuple(chain), None


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
