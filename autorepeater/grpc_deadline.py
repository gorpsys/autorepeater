"""Finite SDK sync unary deadlines; stream lifetimes remain unrestricted."""
from collections import namedtuple
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from math import isfinite
from time import monotonic

import grpc


_READ_DEADLINE: ContextVar[float | None] = ContextVar('sdk_read_deadline', default=None)


@contextmanager
def read_deadline(timeout: float) -> Iterator[None]:
    """Share one monotonic budget across synchronous SDK reads and quota retries."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not isfinite(timeout) or timeout <= 0):
        raise ValueError('read timeout must be positive and finite')
    deadline = monotonic() + timeout
    parent = _READ_DEADLINE.get()
    token = _READ_DEADLINE.set(deadline if parent is None else min(deadline, parent))
    try:
        yield
    finally:
        _READ_DEADLINE.reset(token)


def remaining_read_timeout() -> float | None:
    """Return the scoped remainder, or None outside a scope; reject exhausted reads."""
    deadline = _READ_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError('SDK read deadline expired')
    return remaining


class _CallDetails(namedtuple('_CallDetails', (
        'method', 'timeout', 'metadata', 'credentials', 'wait_for_ready', 'compression')),
                   grpc.ClientCallDetails):
    """Copy all grpc.ClientCallDetails attributes while replacing timeout."""


class UnaryDeadlineInterceptor(grpc.UnaryUnaryClientInterceptor):  # pylint: disable=too-few-public-methods
    """Bound the first unary attempt without retries or stream interception."""

    def __init__(self, timeout=10):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not isfinite(timeout) or timeout <= 0):
            raise ValueError('unary timeout must be positive and finite')
        self.timeout = timeout

    def intercept_unary_unary(self, continuation, client_call_details, request):
        timeout = client_call_details.timeout
        timeout = self.timeout if timeout is None else min(timeout, self.timeout)
        remaining = remaining_read_timeout()
        if remaining is not None:
            timeout = min(timeout, remaining)
        details = _CallDetails(
            client_call_details.method, timeout,
            client_call_details.metadata, client_call_details.credentials,
            client_call_details.wait_for_ready, client_call_details.compression)
        return continuation(details, request)
