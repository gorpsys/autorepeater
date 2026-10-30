"""Failure semantics belong to the tests, never to the CI result consumer."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater import fx_data
from autorepeater.fx_data import FxPair, FxQuote
from e2e.retry_policy import auth_failure, may_retry
from scripts.sandbox_lifecycle import SandboxFailure, safe_call


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
pytest_plugins = ('test.test_sandbox_retry_wiring',)


@pytest.mark.parametrize('limit', [90, 180, 257])
def test_real_fx_guard_uses_its_current_limit_without_ci_changes(monkeypatch, limit):
    """A threshold change requires no corresponding constant in CI."""
    monkeypatch.setattr(fx_data, 'ORDER_BOOK_MAX_AGE_SECONDS', limit)
    quote = FxQuote(FxPair('uid', 'cny', 'rub', Decimal(1), 1000, True, True, True),
                    Decimal(10), Decimal(11), NOW - timedelta(seconds=limit + 1), NOW)
    with pytest.raises(ValueError) as caught:
        quote.validate_fresh(NOW)
    assert may_retry(caught.value, 'call', 'failed')
    assert not may_retry(caught.value, 'teardown', 'failed')


@pytest.mark.parametrize('code,expected', [
    (StatusCode.UNAVAILABLE, True), (StatusCode.DEADLINE_EXCEEDED, True),
    (StatusCode.UNAUTHENTICATED, False), (StatusCode.PERMISSION_DENIED, False),
    (StatusCode.INTERNAL, False),
])
def test_transport_policy_stays_explicit_and_denies_other_failures(code, expected):
    """Only the two documented transient SDK statuses permit another attempt."""
    error = RequestError(code, 'not retained', None)
    assert may_retry(error, 'setup', 'failed') is expected
    assertion = AssertionError('financial assertion')
    assertion.__cause__ = error
    assert not may_retry(assertion, 'call', 'failed')


@pytest.mark.parametrize('error', [ValueError('stale FX order book'), AssertionError(), None])
def test_error_text_or_missing_error_never_grants_retry(error):
    """An arbitrary matching error message is not proof of a retryable guard."""
    assert not may_retry(error, 'call', 'failed')
    assert not may_retry(error, 'call', 'passed')


@pytest.mark.parametrize('fault,expected', [
    ('stale', True), ('mismatch', False), ('missing', False), ('future', False), ('naive', False),
])
def test_security_guard_is_classified_only_at_the_real_validation(security, fault, expected):
    """UID and timestamp failures remain distinct from retryable stale books."""
    client, adapter = security
    book = client.market_data.get_order_book.return_value
    book.orderbook_ts -= timedelta(seconds=300)
    if fault == 'mismatch':
        book.instrument_uid = 'other'
    elif fault == 'missing':
        book.orderbook_ts = None
    elif fault == 'future':
        book.orderbook_ts += timedelta(seconds=600)
    elif fault == 'naive':
        book.orderbook_ts = book.orderbook_ts.replace(tzinfo=None)
    with pytest.raises(ValueError) as caught:
        adapter.get_security_quote('uid')
    assert may_retry(caught.value, 'call', 'failed') is expected


def test_fx_future_receipt_and_unknown_error_chain_are_not_stale():
    """A stale price never excuses receipt ordering or an unknown exception wrapper."""
    quote = FxQuote(FxPair('uid', 'cny', 'rub', Decimal(1), 1000, True, True, True),
                    Decimal(10), Decimal(11), NOW - timedelta(seconds=400),
                    NOW + timedelta(seconds=1))
    with pytest.raises(ValueError) as caught:
        quote.validate_fresh(NOW)
    assert not may_retry(caught.value, 'call', 'failed')
    error = RuntimeError()
    error.__cause__ = caught.value
    assert not may_retry(error, 'call', 'failed')


@pytest.mark.parametrize('boundary', ['security', 'fx'])
@pytest.mark.parametrize('wrapper', ['known', 'unknown', 'auth', 'cycle'])
def test_safe_call_preserves_guard_provenance_and_retry_denials(security, boundary, wrapper):
    """Setup wrappers retain real stale guards without hiding auth or cyclic causes."""
    client, adapter = security
    client.market_data.get_order_book.return_value.orderbook_ts -= timedelta(seconds=181)
    operation = partial(adapter.get_security_quote, 'uid')
    if boundary == 'fx':
        value = FxQuote(FxPair('uid', 'cny', 'rub', Decimal(1), 1000, True, True, True),
                        Decimal(10), Decimal(11), NOW - timedelta(seconds=181), NOW)
        operation = partial(value.validate_fresh, NOW)

    def query():
        try:
            operation()
        except ValueError as error:
            if wrapper == 'unknown':
                raise RuntimeError('private') from error
            if wrapper == 'auth':
                raise RequestError(StatusCode.UNAUTHENTICATED, 'private', {}) from error
            if wrapper == 'cycle':
                error.__cause__ = error
            raise

    with pytest.raises(SandboxFailure) as caught:
        safe_call(query)
    assert caught.value.__suppress_context__
    assert auth_failure(caught.value) is (wrapper in ('auth', 'cycle'))
    assert may_retry(caught.value, 'setup', 'failed') is (wrapper == 'known')
