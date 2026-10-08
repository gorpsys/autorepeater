"""Sandbox-only account ownership, safe errors and a finite ID-format probe."""
import argparse
from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import uuid
from typing import TypeVar

from grpc import StatusCode
from t_tech.invest import GetAccountsResponse, RequestError
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest.services import SandboxService, Services, UsersService

from autorepeater.grpc_deadline import UnaryDeadlineInterceptor
from scripts.sandbox_evidence import IdentifierEvidence, evidence_json

Result = TypeVar('Result')


class SandboxFailure(Exception):
    """Fixed safe reason, without SDK details, arguments or chained transport data."""

    def __init__(self, message: str, *, api_facts: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.api_facts: tuple[dict[str, object], ...] = tuple(api_facts or ())


def list_exception_facts(error: BaseException | None) -> list[dict[str, object]]:
    """Allowlist transport codes and application frames, never messages or frame locals."""
    root = Path(__file__).resolve().parents[1]
    facts: list[dict[str, object]] = []
    visited = set()
    while error is not None and id(error) not in visited and len(facts) < 8:
        visited.add(id(error))
        frames: list[dict[str, object]] = []
        item: dict[str, object] = {'type': type(error).__name__, 'frames': frames}
        trace = error.__traceback__
        while trace is not None:
            try:
                relative = Path(trace.tb_frame.f_code.co_filename).relative_to(root)
            except ValueError:
                relative = None
            if relative is not None and relative.parts[0] in ('autorepeater', 'scripts', 'e2e'):
                frames.append({'file': relative.as_posix(),
                               'function': trace.tb_frame.f_code.co_name,
                               'line': trace.tb_lineno})
            trace = trace.tb_next
        if isinstance(error, RequestError):
            if isinstance(error.code, StatusCode):
                item['grpc_status'] = error.code.name
            if isinstance(error.details, str) and re.fullmatch('[0-9]{1,10}', error.details):
                item['api_code'] = error.details
        facts.append(item)
        if isinstance(error, SandboxFailure):
            facts.extend(dict(fact) for fact in error.api_facts[:8 - len(facts)])
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return facts


def safe_call(method: Callable[..., Result], **kwargs: object) -> Result:
    """Setup mutations are attempted once; an uncertain result is never resubmitted."""
    try:
        return method(**kwargs)
    except Exception as error:
        raise SandboxFailure(f'sandbox API failed: {method.__name__}',
                             api_facts=list_exception_facts(error)) from None


def owned_name(name: str, namespace: str) -> bool:
    """Match the entire versioned name, never a partial account-name prefix."""
    return isinstance(name, str) and re.fullmatch(
        rf'autorepeater-e2e-{namespace}-v1-[0-9a-f]{{32}}-[a-z][a-z0-9]{{0,23}}-(src|dst)',
        name) is not None


def sandbox_client() -> SandboxClient:
    """Only the explicitly supplied sandbox credential and endpoint are accepted."""
    token = os.environ.get('SANDBOX_TOKEN')
    if not token:
        raise SandboxFailure('SANDBOX_TOKEN is required')
    logging.getLogger('t_tech.invest.logging').disabled = True
    return SandboxClient(token=token, target=INVEST_GRPC_API_SANDBOX,
                         interceptors=[UnaryDeadlineInterceptor()])


class SandboxLifecycle:
    """Retain actual returned IDs until successful close, including partial setup."""

    def __init__(self, sandbox: SandboxService, users: UsersService,
                 namespace: str, run_id: str) -> None:
        if namespace not in ('local', 'ci') or re.fullmatch('[0-9a-f]{32}', run_id) is None:
            raise ValueError('invalid sandbox namespace or run ID')
        self.sandbox: SandboxService = sandbox
        self.users: UsersService = users
        self.namespace: str = namespace
        self.run_id: str = run_id
        self.created: list[str] = []
        self.names: dict[str, str] = {}

    def create(self, scenario: str, role: str) -> str:
        """Open once with a reconstructible name; no synthetic source aliases."""
        name = f'autorepeater-e2e-{self.namespace}-v1-{self.run_id}-{scenario}-{role}'
        if not owned_name(name, self.namespace):
            raise ValueError('invalid sandbox account name')
        response = safe_call(self.sandbox.open_sandbox_account, name=name)
        account_id = response.account_id
        if not isinstance(account_id, str) or not account_id or account_id.isspace():
            raise SandboxFailure('open returned invalid account ID; orphan cleanup required')
        self.created.append(account_id)
        self.names[account_id] = name
        return account_id

    def verify_name(self, account_id: str) -> None:
        """Prove the returned name supports strict orphan ownership before any funding."""
        accounts = safe_call(self.users.get_accounts).accounts
        matches = [item for item in accounts if item.id == account_id]
        if len(matches) != 1 or matches[0].name != self.names[account_id]:
            raise SandboxFailure('sandbox name round-trip failed; orphan ownership unproven')
        print('Sandbox name round-trip and strict ownership confirmed; ID format: '
              + id_format(account_id), flush=True)

    def remove_orphans(self, run_id: str | None = None) -> None:
        """CI caller must hold GitHub serialization; local caller holds its process lock."""
        accounts = safe_call(self.users.get_accounts).accounts
        other = 'ci' if self.namespace == 'local' else 'local'
        if run_id is None and any(owned_name(account.name, other) for account in accounts):
            raise SandboxFailure('other namespace accounts present; overlapping run forbidden')
        self.created.extend(account.id for account in accounts
                            if owned_name(account.name, self.namespace)
                            and (run_id is None or account.name.startswith(
                                f'autorepeater-e2e-{self.namespace}-v1-{run_id}-')))
        self.cleanup()

    def cleanup(self) -> None:
        """Try every owned ID; failed closes remain visible and make teardown fail."""
        failed = []
        for account_id in self.created:
            try:
                safe_call(self.sandbox.close_sandbox_account, account_id=account_id)
            except SandboxFailure:
                failed.append(account_id)
        self.created = failed
        if failed:
            raise SandboxFailure('cleanup failed for IDs: ' + ', '.join(failed))


def id_format(account_id: str) -> str:
    """Report identifier format without printing or changing the probe's account ID."""
    if account_id.isascii() and account_id.isdecimal():
        return 'ASCII digits'
    try:
        uuid.UUID(account_id)
    except ValueError:
        return 'non-numeric string'
    return 'UUID'


def identifier_facts(client: Services, lifecycle: SandboxLifecycle,
                     account_id: str) -> IdentifierEvidence:
    """Compare documented fields of one owned account, discarding all other records."""
    name = lifecycle.names[account_id]

    def owned_id(response: GetAccountsResponse) -> str:
        own = [account for account in response.accounts if account.name == name]
        if len(own) != 1 or not isinstance(own[0].id, str) or not own[0].id:
            raise SandboxFailure('identifier probe: unique owned name not returned')
        return own[0].id

    primary_id = owned_id(safe_call(client.users.get_accounts))
    legacy_id = owned_id(safe_call(client.sandbox.get_sandbox_accounts))
    portfolio = safe_call(client.operations.get_portfolio, account_id=account_id)
    positions = safe_call(client.operations.get_positions, account_id=account_id)
    return IdentifierEvidence(
        account_id, name, primary_id, legacy_id, portfolio.account_id, positions.account_id,
        primary_id.isascii() and primary_id.isdecimal(),
        legacy_id.isascii() and legacy_id.isdecimal())


def main(argv: list[str] | None = None, *,
         run_id_factory: Callable[[], uuid.UUID] | None = None) -> int:
    """Bounded one-account probe or orphan cleanup, invoked through sandbox_token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('probe', 'cleanup', 'identifiers'))
    parser.add_argument('--namespace', choices=('local', 'ci'), default='local')
    parser.add_argument('--run-id')
    args = parser.parse_args(argv)
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
    signal.alarm(60)
    try:
        with sandbox_client() as client:
            create_run_id = uuid.uuid4 if run_id_factory is None else run_id_factory
            lifecycle = SandboxLifecycle(client.sandbox, client.users,
                                         args.namespace, create_run_id().hex)
            if args.mode == 'cleanup':
                lifecycle.remove_orphans(args.run_id)
                print('Sandbox owned orphan cleanup complete')
            else:
                try:
                    scenario = 'identifier' if args.mode == 'identifiers' else 'probe'
                    account_id = lifecycle.create(scenario, 'dst')
                    if args.mode == 'identifiers':
                        print(json.dumps(identifier_facts(client, lifecycle, account_id),
                                         default=evidence_json),
                              flush=True)
                    else:
                        print('Sandbox account ID format: ' + id_format(account_id), flush=True)
                finally:
                    lifecycle.cleanup()
                print('Sandbox probe cleanup complete')
                if args.mode == 'identifiers':
                    remaining = [
                        item for item in safe_call(client.users.get_accounts).accounts
                        if item.id == account_id or item.name == lifecycle.names[account_id]]
                    if remaining:
                        raise SandboxFailure('probe orphan remains: ' + account_id)
                    print('Sandbox probe absent from primary GetAccounts after close', flush=True)
    except Exception as error:  # pylint: disable=broad-exception-caught
        reason = str(error) if isinstance(error, SandboxFailure) else type(error).__name__
        print(reason, file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    return 0


if __name__ == '__main__':
    sys.exit(main())
