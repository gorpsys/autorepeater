"""SDK-only rate-limit handling shared by data and submission adapters."""
import time

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.logging_config import logger


def call_api(method, **kwargs):
    """Retry quota rejection indefinitely; all other failures retain their identity."""
    name = getattr(method, '__name__', None)
    if not isinstance(name, str):
        name = type(method).__name__
    while True:
        try:
            return method(**kwargs)
        except RequestError as error:
            if error.code != StatusCode.RESOURCE_EXHAUSTED:
                raise
            logger.warning('API rate limit exhausted: method=%s; retrying in 10 seconds',
                           name)
            time.sleep(10)
