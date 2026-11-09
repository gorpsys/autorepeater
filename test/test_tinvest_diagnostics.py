"""Full incoming SDK diagnostics never inspect outgoing request data."""
import inspect
import logging
from unittest.mock import create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import MoneyValue, PortfolioPosition, PortfolioResponse, RequestError
from t_tech.invest.logging import Metadata
from t_tech.invest.services import OperationsService

from autorepeater import tinvest_diagnostics
from autorepeater.logging_config import LOGGER_NAME


@pytest.fixture(name='operations')
def fixture_operations():
    """Use the actual unwrapped SDK service contract without constructing a client."""
    operations = create_autospec(inspect.unwrap(OperationsService), instance=True, spec_set=True)
    operations.get_portfolio.__name__ = 'get_portfolio'
    return operations


def test_call_once_returns_same_sdk_response_and_logs_complete_repr(operations, caplog):
    """The received DTO is returned unchanged and rendered only on DEBUG."""
    response = PortfolioResponse(positions=[PortfolioPosition(
        instrument_uid='server-uid', current_price=MoneyValue(currency='cny', units=123))])
    operations.get_portfolio.return_value = response
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        result = tinvest_diagnostics.call_once(operations.get_portfolio, account_id='account')
    assert result is response
    operations.get_portfolio.assert_called_once_with(account_id='account')
    assert caplog.messages == [f'SDK response: method=get_portfolio response={response!r}']
    assert caplog.records[0].levelno == logging.DEBUG


class UnrenderableResponse:  # pylint: disable=too-few-public-methods
    """Rendering a result is optional and may itself fail."""

    def __init__(self):
        self.repr_calls = 0

    def __repr__(self) -> str:  # pylint: disable=invalid-repr-returned
        self.repr_calls += 1
        raise AssertionError('response repr was evaluated')


@pytest.mark.parametrize('level', [logging.INFO, logging.DEBUG])
def test_response_repr_is_lazy_and_formatting_failure_preserves_result(operations, caplog, level):
    """INFO never renders; DEBUG substitutes a small formatting-failure placeholder."""
    response = UnrenderableResponse()
    operations.get_portfolio.return_value = response
    with caplog.at_level(level, logger=LOGGER_NAME):
        assert tinvest_diagnostics.call_once(operations.get_portfolio) is response
    operations.get_portfolio.assert_called_once_with()
    if level == logging.DEBUG:
        assert response.repr_calls == 1
        assert 'UnrenderableResponse' in caplog.messages[0]
        assert 'repr failed' in caplog.messages[0]
    else:
        assert response.repr_calls == 0
        assert caplog.records == []


@pytest.mark.parametrize('tracking_id', ['server-tracking-id', None])
def test_request_error_preserves_full_incoming_fields_traceback_and_identity(
        operations, caplog, tracking_id):
    """Large unicode server messages and all Metadata fields remain available."""
    details = 'server diagnostic: первая строка\n' + 'large server text ' * 1000 + '末尾'
    metadata = Metadata(tracking_id, '300', 7, 123456, 'full server metadata message')
    error = RequestError(StatusCode.RESOURCE_EXHAUSTED, details, metadata)
    cause = RuntimeError('original transport cause')
    error.__cause__ = cause
    operations.get_portfolio.side_effect = error
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME), pytest.raises(RequestError) as caught:
        tinvest_diagnostics.call_once(operations.get_portfolio, account_id='account')
    assert caught.value is error
    assert caught.value.__cause__ is cause
    operations.get_portfolio.assert_called_once_with(account_id='account')
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert str(error) in record.message
    assert record.api_method == 'get_portfolio'
    assert record.grpc_status == 'RESOURCE_EXHAUSTED'
    assert record.sdk_details == details
    assert record.sdk_metadata == str(metadata)
    assert getattr(record, 'tracking_id', None) == tracking_id
    assert record.exc_info[1] is error
    formatted = logging.Formatter().format(record)
    assert 'Traceback (most recent call last)' in formatted
    assert 'original transport cause' in formatted
    assert str(error) in formatted


@pytest.mark.parametrize('metadata', [None, {'arbitrary-incoming-field': 'server metadata'}])
def test_missing_tracking_never_invents_an_identifier(operations, caplog, metadata):
    """Non-SDK incoming metadata is still shown fully without guessing tracking IDs."""
    error = RequestError(StatusCode.UNAVAILABLE, 'server message', metadata)
    operations.get_portfolio.side_effect = error
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME), pytest.raises(RequestError):
        tinvest_diagnostics.call_once(operations.get_portfolio)
    record = caplog.records[0]
    assert record.sdk_metadata == str(metadata)
    assert getattr(record, 'tracking_id', None) is None


class UnrenderableError(RequestError):
    """An error with broken __str__ must still propagate unchanged."""

    def __str__(self):
        raise ValueError('formatting must not replace the SDK error')


class UnrenderableMetadata:
    """Incoming metadata can fail during formatting as well."""

    def __str__(self):
        raise ValueError('metadata formatting failed')

    def __repr__(self):
        raise ValueError('metadata repr failed')


def test_broken_error_and_metadata_formatting_preserve_original_error(operations, caplog):
    """No recursive formatting error hides the actual SDK failure."""
    error = UnrenderableError(StatusCode.INTERNAL, 'server details', UnrenderableMetadata())
    operations.get_portfolio.side_effect = error
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME), pytest.raises(RequestError) as caught:
        tinvest_diagnostics.call_once(operations.get_portfolio)
    assert caught.value is error
    record = caplog.records[0]
    assert 'UnrenderableError' in record.message
    assert 'str failed' in record.message
    assert 'UnrenderableMetadata' in record.sdk_metadata
    assert 'str failed' in record.sdk_metadata
    assert record.sdk_details == 'server details'
    assert record.exc_info[1] is error


@pytest.mark.parametrize('error', [TypeError('programming error'), ValueError('invalid input')])
def test_programming_error_is_not_converted_retried_or_logged(operations, caplog, error):
    """Only SDK RequestError belongs to the SDK diagnostic error route."""
    operations.get_portfolio.side_effect = error
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), pytest.raises(type(error)) as caught:
        tinvest_diagnostics.call_once(operations.get_portfolio)
    assert caught.value is error
    operations.get_portfolio.assert_called_once_with()
    assert caplog.records == []


@pytest.mark.parametrize('failure', [False, True])
def test_kwargs_values_and_callable_client_metadata_are_not_inspected(caplog, failure):
    """The wrapper forwards opaque outgoing values without rendering or reading them."""
    inspections = []

    class OpaqueCredential:
        """Any diagnostic access to request values is a bug."""

        def __repr__(self) -> str:  # pylint: disable=invalid-repr-returned
            inspections.append('repr')
            raise AssertionError('outgoing credential marker was inspected')

        def __str__(self) -> str:  # pylint: disable=invalid-str-returned
            inspections.append('str')
            raise AssertionError('outgoing credential marker was inspected')

    credential = OpaqueCredential()
    calls = []
    response = PortfolioResponse()
    error = RequestError(StatusCode.UNAUTHENTICATED, 'server diagnostic marker', None)

    class SdkMethod:
        """No callable name or client access is required to perform the SDK call."""

        @property
        def __self__(self):
            raise AssertionError('client was inspected')

        def __call__(self, **kwargs):
            calls.append(kwargs)
            if failure:
                raise error
            return response

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        if failure:
            with pytest.raises(RequestError) as caught:
                tinvest_diagnostics.call_once(SdkMethod(), account_id=credential)
            assert caught.value is error
        else:
            assert tinvest_diagnostics.call_once(SdkMethod(), account_id=credential) is response
    assert len(calls) == 1
    assert calls[0]['account_id'] is credential
    assert not inspections
    assert all('outgoing credential marker' not in message for message in caplog.messages)
    assert all('method=SdkMethod' in message for message in caplog.messages)
