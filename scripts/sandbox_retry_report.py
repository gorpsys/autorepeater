"""Sanitized original pytest facts, recorded only after actual fixture finalizers."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from e2e.retry_policy import auth_failure
from scripts.retry_evidence import checked_sha
from scripts.retry_results import PhaseResult


class AttemptRecorder:  # pylint: disable=too-many-instance-attributes
    """One child process's mutable recording shell, frozen into a typed report once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.selected: tuple[str, ...] = ()
        self.entered: list[str] = []
        self.cleaned: list[str] = []
        self.session_cleanup = False
        self.collection_errors = 0
        self.results: list[PhaseResult] = []
        self.retry_blocked = False

    def phase(self, node: str, when: str, outcome: str, error: BaseException | None,
              *, retryable: bool = False) -> None:
        """Do not retain error objects, locals, messages or token-bearing arguments."""
        self.retry_blocked = self.retry_blocked or auth_failure(error)
        self.results.append(PhaseResult(node, when, outcome, retryable))

    def finish(self, exit_status: int) -> None:
        """Run at sessionfinish, after scenario and session teardown reports exist."""
        sha = os.environ.get('E2E_TESTED_SHA')
        fields = {
            'version': 1, 'selected': self.selected,
            'phases': [asdict(replace(phase, retryable=False)
                              if self.retry_blocked else phase) for phase in self.results],
            'entered': self.entered, 'cleaned': self.cleaned,
            'session_cleanup': self.session_cleanup,
            'collection_errors': self.collection_errors, 'exit_status': exit_status,
            'sha': checked_sha(sha) if sha is not None else None,
        }
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
