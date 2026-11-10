"""Diagnostics for one SDK attempt, without inspecting requests or client state."""
import logging
from collections.abc import Callable
from typing import TypeVar

from grpc import StatusCode
from t_tech.invest import RequestError
from t_tech.invest.logging import Metadata

from autorepeater.logging_config import logger

Result = TypeVar('Result')


def _text(value: object, *, representation: bool = False) -> str:
    """A broken SDK formatter must not change a result or hide the original error."""
    try:
        return repr(value) if representation else str(value)
    except Exception:  # pylint: disable=broad-exception-caught
        operation = 'repr' if representation else 'str'
        return f'<{type(value).__name__}: {operation} failed>'


def log_response(method_name: str, response: object) -> None:
    """Render a complete received SDK response only when DEBUG is enabled."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug('SDK response: method=%s response=%s', method_name,
                     _text(response, representation=True))


def log_request_error(method_name: str, error: RequestError) -> None:
    """Record complete incoming SDK diagnostics and the original traceback chain."""
    status = error.code.name if isinstance(error.code, StatusCode) else _text(error.code)
    fields = {'api_method': method_name, 'grpc_status': status,
              'sdk_details': _text(error.details), 'sdk_metadata': _text(error.metadata)}
    if isinstance(error.metadata, Metadata) and error.metadata.tracking_id is not None:
        fields['tracking_id'] = error.metadata.tracking_id
    logger.error('SDK request failed: method=%s error=%s', method_name, _text(error),
                 exc_info=(type(error), error, error.__traceback__), extra=fields)


def call_once(method: Callable[..., Result], **kwargs: object) -> Result:
    """Perform one SDK call, retaining response/error identity and programming errors."""
    name = getattr(method, '__name__', None)
    if not isinstance(name, str):
        name = type(method).__name__
    try:
        response = method(**kwargs)
    except RequestError as error:
        log_request_error(name, error)
        raise
    log_response(name, response)
    return response
