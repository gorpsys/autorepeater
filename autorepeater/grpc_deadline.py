"""Finite SDK sync unary deadlines; stream lifetimes remain unrestricted."""
from collections import namedtuple
from math import isfinite

import grpc


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
        details = _CallDetails(
            client_call_details.method,
            self.timeout if timeout is None else min(timeout, self.timeout),
            client_call_details.metadata, client_call_details.credentials,
            client_call_details.wait_for_ready, client_call_details.compression)
        return continuation(details, request)
