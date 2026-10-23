"""SDK-only rate-limit handling shared by data and submission adapters."""
import time
from collections.abc import Callable
from typing import TypeVar

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.grpc_deadline import remaining_read_timeout
from autorepeater.logging_config import logger
from autorepeater.market_diagnostics import measure_rpc_part

Result = TypeVar('Result')


def call_api(method: Callable[..., Result], **kwargs: object) -> Result:
    """Retry quota rejection within any read scope; preserve actual transport failures."""
    name = getattr(method, '__name__', None)
    if not isinstance(name, str):
        name = type(method).__name__
    quota_error = None
    try:
        while True:
            remaining_read_timeout()
            started = time.perf_counter()
            try:
                with measure_rpc_part('attempt'):
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
                quota_error = error
                remaining = remaining_read_timeout()
                wait = 10 if remaining is None else min(10, remaining)
                logger.warning('API rate limit exhausted: method=%s; retrying in %s seconds',
                               name, wait)
                with measure_rpc_part('quota_wait'):
                    time.sleep(wait)
    except TimeoutError as error:
        if quota_error is not None:
            raise error from quota_error
        raise
