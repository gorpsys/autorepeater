"""SDK-only rate-limit handling shared by data and submission adapters."""
import time
from collections.abc import Callable
from typing import TypeVar

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.logging_config import logger

Result = TypeVar('Result')


def call_api(method: Callable[..., Result], **kwargs: object) -> Result:
    """Retry quota rejection indefinitely; all other failures retain their identity."""
    name = getattr(method, '__name__', None)
    if not isinstance(name, str):
        name = type(method).__name__
    while True:
        started = time.perf_counter()
        try:
            return method(**kwargs)
        except RequestError as error:
            if error.code != StatusCode.RESOURCE_EXHAUSTED:
                status = error.code.name if isinstance(error.code, StatusCode) else 'UNKNOWN'
                elapsed = time.perf_counter() - started
                logger.error('API request failed: method=%s status=%s elapsed_seconds=%.3f',
                             name, status, elapsed,
                             extra={'api_method': name, 'grpc_status': status,
                                    'elapsed_seconds': elapsed})
                raise
            logger.warning('API rate limit exhausted: method=%s; retrying in 10 seconds',
                           name)
            time.sleep(10)
