"""Sanitized original pytest facts, recorded only after actual fixture finalizers."""
from dataclasses import asdict
import json
from pathlib import Path

from grpc import StatusCode
from t_tech.invest import RequestError

from autorepeater.execution import OrderExecutionError
from autorepeater.execution_data import ExecutionDataError
from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure
from scripts.sandbox_retry_evidence import (
    AttemptReport, PhaseFact, TRANSPORT_ROOTS, TRANSIENT_STATUSES,
)

KNOWN_ERRORS = (
    RequestError, SandboxFailure, DataAccessError, ExecutionDataError, OrderExecutionError,
)


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
            return (*chain, *suffix), status_name
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
        self.phases.append(PhaseFact(node, when, outcome, chain, status))

    def finish(self, exit_status: int) -> None:
        """Run at sessionfinish, after scenario and session teardown reports exist."""
        report = AttemptReport(self.selected, tuple(self.phases), tuple(self.entered),
                               tuple(self.cleaned), self.session_cleanup,
                               self.collection_errors, exit_status)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(report)), encoding='utf-8')
        markers = 'Sandbox scenario cleanup complete\n' * len(self.cleaned)
        if self.session_cleanup:
            markers += 'Sandbox session cleanup complete\n'
        (self.path.parent / 'cleanup.log').write_text(markers, encoding='utf-8')


def attempt_recorder(session: object) -> AttemptRecorder | None:
    """Direct pytest-independent unit hooks have no session recorder and perform no I/O."""
    value = getattr(getattr(session, 'config', None), 'sandbox_retry_recorder', None)
    return value if isinstance(value, AttemptRecorder) else None
