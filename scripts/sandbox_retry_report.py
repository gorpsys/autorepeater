"""Sanitized original pytest facts, recorded only after actual fixture finalizers."""
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from types import CodeType

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.execution import OrderExecutionError
from autorepeater.execution_data import ExecutionDataError
from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure
from scripts.sandbox_retry_evidence import (
    AttemptReport, PhaseFact, TRANSPORT_ROOTS, TRANSIENT_STATUSES,
    PROTOCOLS, STALE_BOOK_MAX_AGE_SECONDS, RetryFailure, checked_sha, validate_stale_book,
)

KNOWN_ERRORS = (
    RequestError, SandboxFailure, DataAccessError, ExecutionDataError, OrderExecutionError,
)


def validation_method(code: CodeType) -> str | None:
    """Match loaded method code identities without importing optional future runtime."""
    for module, owner, method in (
            ('autorepeater.tinvest_security_data', 'TInvestSecurityData', 'get_security_quote'),
            ('autorepeater.fx_data', 'FxQuote', 'validate_fresh')):
        cls = getattr(sys.modules.get(module), owner, None)
        if code is getattr(getattr(cls, method, None), '__code__', None):
            return method
    return None


def _stale_validation_facts(error: ValueError) -> dict[str, object] | None:
    """Extract only named guard inputs; never serialize arbitrary traceback locals."""
    trace = error.__traceback__
    if trace is None:
        return None
    while trace.tb_next is not None:
        trace = trace.tb_next
    values = trace.tb_frame.f_locals
    method = validation_method(trace.tb_frame.f_code)
    try:
        if method == 'get_security_quote':
            book = values['book']
            uid, received = values['uid'], book.instrument_uid
            stamp, now = book.orderbook_ts, values['now']
        elif method == 'validate_fresh':
            quote, now = values['self'], values['now']
            if not isinstance(now, datetime) or now < quote.received_time:
                return None
            uid = received = quote.pair.uid
            stamp = quote.market_time
        else:
            return None
        if isinstance(stamp, datetime) and isinstance(now, datetime):
            return validate_stale_book({
                'reason': 'market_time_stale', 'expected_uid': uid, 'received_uid': received,
                'market_time': stamp.isoformat(), 'observed_utc': now.isoformat(),
                'freshness_limit_seconds': STALE_BOOK_MAX_AGE_SECONDS})
    except (KeyError, TypeError, AttributeError, RetryFailure):
        pass
    return None


def stale_book_facts(error: BaseException | None) -> dict[str, object] | None:
    """Read only explicit facts at the actual known validation raise, never error text."""
    visited: set[int] = set()
    while error is not None and id(error) not in visited and len(visited) < 8:
        visited.add(id(error))
        if isinstance(error, ValueError) and error.__class__ is ValueError:
            return _stale_validation_facts(error)
        if type(error) not in KNOWN_ERRORS or isinstance(error, RequestError):
            return None
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
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


class AttemptRecorder:
    """One child process's mutable recording shell, frozen into a typed report once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.selected: tuple[str, ...] = ()
        self.phases: list[PhaseFact] = []
        self.entered: list[str] = []
        self.cleaned: list[str] = []
        self.session_cleanup = False
        self.collection_errors = 0

    def phase(self, node: str, when: str, outcome: str, error: BaseException | None) -> None:
        """Do not retain error objects, locals, messages or token-bearing arguments."""
        chain, status = transport_facts(error)
        auth = auth_failure(error)
        stale = (stale_book_facts(error) if when != 'teardown' and outcome == 'failed'
                 and not auth and os.environ.get('E2E_EVIDENCE_PROTOCOL') == 'retry-v3' else None)
        if stale is not None:
            chain = (*chain[:-1], 'ValueError')
        self.phases.append(PhaseFact(node, when, outcome, chain, status, stale, auth))

    def finish(self, exit_status: int) -> None:
        """Run at sessionfinish, after scenario and session teardown reports exist."""
        report = AttemptReport(self.selected, tuple(self.phases), tuple(self.entered),
                               tuple(self.cleaned), self.session_cleanup,
                               self.collection_errors, exit_status)
        protocol = os.environ.get('E2E_EVIDENCE_PROTOCOL', 'retry-v1')
        if protocol not in PROTOCOLS:
            raise RetryFailure('unsupported evidence protocol')
        fields = asdict(report)
        if protocol != 'retry-v1':
            fields['sha'] = checked_sha(os.environ.get('E2E_TESTED_SHA', ''))
        else:
            del fields['sha']
        if protocol != 'retry-v3':
            for phase in fields['phases']:
                del phase['stale_book']
                del phase['auth_failure']
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(fields), encoding='utf-8')
        markers = 'Sandbox scenario cleanup complete\n' * len(self.cleaned)
        if self.session_cleanup:
            markers += 'Sandbox session cleanup complete\n'
        (self.path.parent / 'cleanup.log').write_text(markers, encoding='utf-8')


def attempt_recorder(session: object) -> AttemptRecorder | None:
    """Direct pytest-independent unit hooks have no session recorder and perform no I/O."""
    value = getattr(getattr(session, 'config', None), 'sandbox_retry_recorder', None)
    return value if isinstance(value, AttemptRecorder) else None
