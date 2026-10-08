"""Live fixtures are siblings of test/, so its zero-timeout fixture cannot apply."""
import logging
import os
from pathlib import Path
import signal
from collections.abc import Generator
from typing import Any

import pytest

from scripts.sandbox_lifecycle import (
    SandboxFailure, SandboxLifecycle, list_exception_facts, sandbox_client,
)
from scripts.sandbox_support import OrderJournal, SandboxSession, write_failure_dump
from scripts.sandbox_retry_report import AttemptRecorder, attempt_recorder


class SandboxInterrupted(BaseException):
    """Let fixture finally blocks run on supervisor termination."""


def pytest_sessionstart(session: pytest.Session) -> None:
    """Recording is enabled only by the supervised controller's explicit path."""
    path = os.environ.get('E2E_ATTEMPT_REPORT')
    if path:
        setattr(session.config, 'sandbox_retry_recorder', AttemptRecorder(Path(path)))


def pytest_collection_finish(session: pytest.Session) -> None:
    """Capture actual post-selection collection and failures before tests execute."""
    recorder = attempt_recorder(session)
    if recorder is not None:
        recorder.selected = tuple(item.nodeid for item in session.items)
        recorder.collection_errors = getattr(session, 'testsfailed', 0)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Persist all original phases, including cached setup errors and teardown."""
    recorder = attempt_recorder(session)
    if recorder is not None:
        recorder.finish(int(exitstatus))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
        item: pytest.Item, call: pytest.CallInfo) -> Generator[None, Any, None]:
    """Transport causes and pytest locals must never reach console/JUnit artifacts."""
    outcome = yield
    report = outcome.get_result()
    recorder = attempt_recorder(getattr(item, 'session', None))
    if recorder is not None:
        recorder.phase(item.nodeid, report.when, report.outcome,
                       None if call.excinfo is None else call.excinfo.value)
    if call.excinfo is not None:
        exception = call.excinfo.value
        report.sandbox_error = list_exception_facts(exception)
        reason = (str(exception) if isinstance(exception, SandboxFailure)
                  else type(exception).__name__)
        transport = next((item for item in report.sandbox_error if 'grpc_status' in item), None)
        if transport is not None:
            reason += '; grpc=' + transport['grpc_status']
            if 'api_code' in transport:
                reason += '; api_code=' + transport['api_code']
        report.longrepr = f'{item.nodeid}: {reason}'
        report.sections = []
    setattr(item, 'report_' + report.when, report)


@pytest.fixture(scope='session', name='sandbox')
def fixture_sandbox(request: pytest.FixtureRequest) -> Generator[SandboxSession, None, None]:
    """Caller must be the serialized bounded launcher, never direct plain pytest."""
    namespace = os.environ.get('E2E_NAMESPACE')
    run_id = os.environ.get('E2E_RUN_ID')
    if namespace not in ('local', 'ci') or not run_id:
        raise SandboxFailure('launch live tests with make e2e')
    if os.environ.get('PYTEST_XDIST_WORKER'):
        raise SandboxFailure('live scenarios must execute sequentially')
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(*_: object) -> None:
        raise SandboxInterrupted

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with sandbox_client() as client:
            lifecycle = SandboxLifecycle(client.sandbox, client.users, namespace, run_id)
            session_initialized = False
            try:
                lifecycle.remove_orphans()
                session_initialized = True
                yield SandboxSession(client, lifecycle)
            finally:
                lifecycle.cleanup()
                if session_initialized:
                    lifecycle.remove_orphans(run_id=lifecycle.run_id)
                recorder = attempt_recorder(request.session)
                if recorder is not None:
                    recorder.session_cleanup = True
                print('Sandbox session cleanup complete', flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.fixture
def live(request: pytest.FixtureRequest, sandbox: SandboxSession
         ) -> Generator[tuple[SandboxSession, OrderJournal], None, None]:
    """Every scenario owns separate created accounts and cleanup even if dump fails."""
    sandbox.scenario = request.node.name
    sandbox.records = []
    sandbox.configs = {}
    sandbox.quotes = {}
    sandbox.data.begin_snapshot()
    journal = OrderJournal()
    logger = logging.getLogger('tinkoffBot')
    logger.addHandler(journal)
    recorder = attempt_recorder(request.session)
    if recorder is not None:
        recorder.entered.append(request.node.nodeid)
    try:
        yield sandbox, journal
    finally:
        sandbox.records.extend(journal.events)
        report = getattr(request.node, 'report_call', None)
        if report is not None and report.failed:
            sandbox.records.append({'event': 'failure', 'reason': str(report.longrepr),
                                    'error': report.sandbox_error})
        logger.removeHandler(journal)
        artifact_root = Path(os.environ.get(
            'E2E_ARTIFACT_DIR', '/tmp/autorepeater-e2e-' + sandbox.lifecycle.run_id))
        try:
            write_failure_dump(sandbox, artifact_root / (request.node.name + '.json'))
            print(f'Sandbox evidence: {artifact_root / (request.node.name + ".json")}', flush=True)
        except Exception as error:
            print('Sandbox diagnostic failed: ' + type(error).__name__, flush=True)
            raise SandboxFailure('failed to write sandbox diagnostics') from None
        finally:
            sandbox.lifecycle.cleanup()
            if recorder is not None:
                recorder.cleaned.append(request.node.nodeid)
            print('Sandbox scenario cleanup complete', flush=True)
