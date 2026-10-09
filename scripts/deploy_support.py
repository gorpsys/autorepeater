"""Bounded JSON transport and safe diagnostics shared only by deployment helpers."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 2_000_000
HTTP_TIMEOUT = 15


class DeployError(Exception):
    """Fixed diagnostic facts only; never include provider bodies or credentials."""


class NoRedirect(HTTPRedirectHandler):
    """Credential-bearing requests must not forward Authorization to another origin."""

    # urllib requires this override signature even though redirects are always refused.
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def redirect_request(self, req: Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


def request_json(request: Request) -> object:
    """Read finite JSON, suppressing arbitrary HTTP error bodies and tracebacks."""
    try:
        with build_opener(NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeployError('HTTP response exceeds size limit')
        return json.loads(raw)
    except HTTPError as error:
        raise DeployError(f'HTTP request failed (status {error.code})') from None
    except (OSError, URLError, ValueError):
        raise DeployError('HTTP request or JSON response failed') from None


def record(value: object) -> dict[str, object]:
    """Require a JSON object without coercing producer mistakes."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DeployError('expected a JSON object')
    return value


def text(value: object, label: str) -> str:
    """Single-line identifiers cannot inject workflow outputs or CLI property lists."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise DeployError(f'invalid {label}')
    return value


def commit_sha(value: object) -> str:
    """Only an immutable, nonzero full commit ID is deployable."""
    result = text(value, 'commit SHA')
    if re.fullmatch('[0-9a-f]{40}', result) is None or result == '0' * 40:
        raise DeployError('invalid commit SHA')
    return result
