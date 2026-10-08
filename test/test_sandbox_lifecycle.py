"""Offline lifecycle failures use strict SDK service doubles, never a channel."""
import inspect
from contextlib import contextmanager
import logging
import signal
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import create_autospec

import pytest
from t_tech.invest import (Account, CloseSandboxAccountResponse, GetAccountsResponse,
                           OpenSandboxAccountResponse, PortfolioResponse, PositionsResponse)
from t_tech.invest.services import OperationsService, SandboxService, UsersService

from scripts.sandbox_lifecycle import SandboxFailure, SandboxLifecycle, owned_name
from scripts import sandbox_lifecycle


@pytest.fixture(name='services')
def fixture_services():
    """Explicit SDK DTOs and spec_set forbid invented methods."""
    sandbox = create_autospec(inspect.unwrap(SandboxService), instance=True, spec_set=True)
    users = create_autospec(inspect.unwrap(UsersService), instance=True, spec_set=True)
    sandbox.open_sandbox_account.return_value = OpenSandboxAccountResponse(account_id='123')
    sandbox.close_sandbox_account.return_value = CloseSandboxAccountResponse()
    users.get_accounts.return_value = GetAccountsResponse(accounts=[])
    return sandbox, users


@pytest.mark.parametrize('name', [
    'personal', 'autorepeater-e2e-local-v1-' + 'a' * 32 + '-initial-dst-extra',
    'autorepeater-e2e-ci-v1-' + 'a' * 32 + '-initial-dst',
    'autorepeater-e2e-local-v2-' + 'a' * 32 + '-initial-dst',
    'autorepeater-e2e-local-v1-short-initial-dst',
    'autorepeater-e2e-local-v1-' + 'a' * 32 + '-initial-dst\n',
])
def test_orphan_match_is_strict(name):
    """Partial prefixes, versions and other namespaces never establish ownership."""
    assert not owned_name(name, 'local')


def test_create_tracks_actual_id_and_cleanup(services):
    """Teardown closes the actual returned ID exactly once."""
    sandbox, users = services
    lifecycle = SandboxLifecycle(sandbox, users, 'local', 'a' * 32)
    assert lifecycle.create('initial', 'dst') == '123'
    sandbox.open_sandbox_account.assert_called_once_with(
        name='autorepeater-e2e-local-v1-' + 'a' * 32 + '-initial-dst')
    lifecycle.cleanup()
    lifecycle.cleanup()
    sandbox.close_sandbox_account.assert_called_once_with(account_id='123')


def test_orphans_close_only_complete_owned_names(services):
    """A personal account is never touched by orphan cleanup."""
    sandbox, users = services
    own = 'autorepeater-e2e-local-v1-' + 'b' * 32 + '-initial-dst'
    users.get_accounts.return_value = GetAccountsResponse(accounts=[
        Account(id='orphan', name=own), Account(id='personal', name='personal')])
    SandboxLifecycle(sandbox, users, 'local', 'a' * 32).remove_orphans()
    users.get_accounts.assert_called_once_with()
    sandbox.close_sandbox_account.assert_called_once_with(account_id='orphan')


def test_other_namespace_blocks_overlap_without_deletion(services):
    """The other execution namespace belongs to a different serialized caller."""
    sandbox, users = services
    users.get_accounts.return_value = GetAccountsResponse(accounts=[Account(
        id='ci', name='autorepeater-e2e-ci-v1-' + 'b' * 32 + '-initial-dst')])
    with pytest.raises(SandboxFailure, match='other namespace'):
        SandboxLifecycle(sandbox, users, 'local', 'a' * 32).remove_orphans()
    sandbox.close_sandbox_account.assert_not_called()


def test_cleanup_continues_and_retains_failures(services):
    """Cleanup errors are safe and retain only IDs needing a later close."""
    sandbox, users = services
    lifecycle = SandboxLifecycle(sandbox, users, 'local', 'a' * 32)
    sandbox.open_sandbox_account.side_effect = [
        OpenSandboxAccountResponse(account_id='one'), OpenSandboxAccountResponse(account_id='two')]
    lifecycle.create('initial', 'src')
    lifecycle.create('initial', 'dst')
    sandbox.close_sandbox_account.side_effect = [RuntimeError('private'), None]
    with pytest.raises(SandboxFailure, match='one') as error:
        lifecycle.cleanup()
    assert 'private' not in str(error.value)
    assert lifecycle.created == ['one']
    assert sandbox.close_sandbox_account.call_count == 2


def test_uncertain_open_is_not_retried(services):
    """An unknown create result must be recovered by name on the next run."""
    sandbox, users = services
    sandbox.open_sandbox_account.side_effect = RuntimeError('private')
    with pytest.raises(SandboxFailure, match='open_sandbox_account') as error:
        SandboxLifecycle(sandbox, users, 'local', 'a' * 32).create('initial', 'dst')
    assert 'private' not in str(error.value)
    sandbox.open_sandbox_account.assert_called_once()


@pytest.mark.parametrize('namespace,run_id', [('prod', 'a' * 32), ('local', 'invalid')])
def test_invalid_name_inputs_before_api(services, namespace, run_id):
    """Names cannot escape the versioned test namespace."""
    sandbox, users = services
    with pytest.raises(ValueError):
        SandboxLifecycle(sandbox, users, namespace, run_id)
    sandbox.open_sandbox_account.assert_not_called()


def test_name_roundtrip_required_for_orphan_ownership(services):
    """Sandbox account names must survive an independent GetAccounts read."""
    sandbox, users = services
    lifecycle = SandboxLifecycle(sandbox, users, 'local', 'a' * 32)
    account_id = lifecycle.create('initial', 'dst')
    users.get_accounts.return_value = GetAccountsResponse(accounts=[Account(
        id=account_id, name='autorepeater-e2e-local-v1-' + 'a' * 32 + '-initial-dst')])
    lifecycle.verify_name(account_id)
    users.get_accounts.return_value = GetAccountsResponse(accounts=[Account(
        id=account_id, name='changed')])
    with pytest.raises(SandboxFailure, match='round-trip'):
        lifecycle.verify_name(account_id)
    assert lifecycle.created == [account_id]


def test_timeout_cleanup_only_current_run(services):
    """Timeout recovery never closes another run even within the same namespace."""
    sandbox, users = services
    users.get_accounts.return_value = GetAccountsResponse(accounts=[
        Account(id='own', name='autorepeater-e2e-local-v1-' + 'a' * 32 + '-initial-dst'),
        Account(id='other', name='autorepeater-e2e-local-v1-' + 'b' * 32 + '-initial-dst'),
        Account(id='ci', name='autorepeater-e2e-ci-v1-' + 'c' * 32 + '-initial-dst')])
    SandboxLifecycle(sandbox, users, 'local', 'a' * 32).remove_orphans('a' * 32)
    sandbox.close_sandbox_account.assert_called_once_with(account_id='own')


@pytest.mark.parametrize('account_id', ['', None, ' '])
def test_invalid_open_id_is_fatal_without_retry(services, account_id):
    """Unknown account identity cannot be closed using a made-up alias."""
    sandbox, users = services
    sandbox.open_sandbox_account.return_value = OpenSandboxAccountResponse(account_id=account_id)
    with pytest.raises(SandboxFailure, match='invalid account ID'):
        SandboxLifecycle(sandbox, users, 'local', 'a' * 32).create('initial', 'dst')
    sandbox.open_sandbox_account.assert_called_once()


def test_bad_scenario_does_not_open(services):
    """Names containing separators cannot broaden orphan ownership."""
    sandbox, users = services
    with pytest.raises(ValueError, match='name'):
        SandboxLifecycle(sandbox, users, 'local', 'a' * 32).create('bad-name', 'dst')
    sandbox.open_sandbox_account.assert_not_called()


@pytest.mark.parametrize('value,expected', [
    ('00012', 'ASCII digits'), ('a' * 32, 'UUID'),
    ('notnumeric', 'non-numeric string')])
def test_id_format_never_aliases_real_ids(value, expected):
    """A format report never mutates or prints an actual probe ID."""
    assert sandbox_lifecycle.id_format(value) == expected


def test_sandbox_client_requires_explicit_credential_and_endpoint(monkeypatch):
    """No production-token fallback and a unary interceptor on setup services."""
    monkeypatch.delenv('SANDBOX_TOKEN', raising=False)
    monkeypatch.setenv('INVEST_TOKEN', 'offline-production-marker')
    with pytest.raises(SandboxFailure, match='SANDBOX_TOKEN'):
        sandbox_lifecycle.sandbox_client()
    factory = create_autospec(sandbox_lifecycle.SandboxClient, spec_set=True)
    monkeypatch.setattr(sandbox_lifecycle, 'SandboxClient', factory)
    sdk_logger = logging.getLogger('t_tech.invest.logging')
    monkeypatch.setattr(sdk_logger, 'disabled', False)
    monkeypatch.setenv('SANDBOX_TOKEN', 'offline-sandbox-marker')
    sandbox_lifecycle.sandbox_client()
    args = factory.call_args.kwargs
    assert args['token'] == 'offline-sandbox-marker'
    assert args['target'] == sandbox_lifecycle.INVEST_GRPC_API_SANDBOX
    assert len(args['interceptors']) == 1
    assert isinstance(args['interceptors'][0], sandbox_lifecycle.UnaryDeadlineInterceptor)
    assert sdk_logger.disabled


@pytest.mark.parametrize('mode,close_fails', [
    ('probe', False), ('cleanup', False), ('probe', True)])
def test_probe_and_cleanup_use_bounded_safe_lifecycle(
        services, monkeypatch, capsys, mode, close_fails):
    """One probe account closes even after compatibility reporting; close errors fail."""
    sandbox, users = services
    if close_fails:
        sandbox.close_sandbox_account.side_effect = RuntimeError('private details')

    @contextmanager
    def client():
        yield SimpleNamespace(sandbox=sandbox, users=users)
    monkeypatch.setattr(sandbox_lifecycle, 'sandbox_client', client)
    alarm = create_autospec(signal.alarm, spec_set=True)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'alarm', alarm)
    handler = create_autospec(signal.signal, spec_set=True)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'signal', handler)
    ids = create_autospec(lambda: UUID('a' * 32), spec_set=True,
                          return_value=UUID('a' * 32))
    assert sandbox_lifecycle.main([mode], run_id_factory=ids) == int(close_fails)
    ids.assert_called_once_with()
    assert alarm.call_args_list[0].args == (60,)
    assert alarm.call_args_list[-1].args == (0,)
    if mode == 'probe':
        sandbox.open_sandbox_account.assert_called_once_with(
            name='autorepeater-e2e-local-v1-' + 'a' * 32 + '-probe-dst')
        sandbox.close_sandbox_account.assert_called_once_with(account_id='123')
    output = capsys.readouterr()
    assert 'private' not in output.out + output.err


def test_probe_programming_error_is_safe_and_fails(monkeypatch, capsys):
    """A failing channel constructor must not print a transport traceback."""
    def broken_client():
        raise RuntimeError('private')
    monkeypatch.setattr(sandbox_lifecycle, 'sandbox_client', broken_client)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'alarm', lambda _: None)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'signal', lambda *_: None)
    assert sandbox_lifecycle.main(['probe']) == 1
    assert capsys.readouterr().err == 'RuntimeError\n'


def test_identifier_probe_compares_only_owned_record(services):
    """An independently returned numeric ID would be visible without guessing aliases."""
    sandbox, users = services
    operations = create_autospec(inspect.unwrap(OperationsService), instance=True, spec_set=True)
    lifecycle = SandboxLifecycle(sandbox, users, 'local', 'a' * 32)
    account_id = lifecycle.create('identifier', 'dst')
    own_name = lifecycle.names[account_id]
    users.get_accounts.return_value = GetAccountsResponse(accounts=[
        Account(id='999', name=own_name), Account(id='PRIVATE-ID', name='personal')])
    sandbox.get_sandbox_accounts.return_value = GetAccountsResponse(accounts=[
        Account(id=account_id, name=own_name), Account(id='PRIVATE-ID', name='personal')])
    operations.get_portfolio.return_value = PortfolioResponse(account_id=account_id)
    operations.get_positions.return_value = PositionsResponse(account_id=account_id)
    facts = sandbox_lifecycle.identifier_facts(
        SimpleNamespace(sandbox=sandbox, users=users, operations=operations), lifecycle, account_id)
    assert facts.primary_id == '999'
    assert facts.legacy_id == facts.portfolio_account_id == account_id
    assert facts.positions_account_id == account_id
    assert facts.primary_numeric is True
    assert 'PRIVATE-ID' not in str(facts)
    users.get_accounts.assert_called_once_with()
    sandbox.get_sandbox_accounts.assert_called_once_with()
    operations.get_portfolio.assert_called_once_with(account_id=account_id)
    operations.get_positions.assert_called_once_with(account_id=account_id)


def test_identifier_probe_failure_still_closes_created_account(services, monkeypatch, capsys):
    """An identifier read failure cannot leave the known probe account without teardown."""
    sandbox, users = services

    @contextmanager
    def client():
        yield SimpleNamespace(sandbox=sandbox, users=users)
    monkeypatch.setattr(sandbox_lifecycle, 'sandbox_client', client)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'alarm', lambda _: None)
    monkeypatch.setattr(sandbox_lifecycle.signal, 'signal', lambda *_: None)
    assert sandbox_lifecycle.main(['identifiers']) == 1
    sandbox.open_sandbox_account.assert_called_once()
    sandbox.close_sandbox_account.assert_called_once_with(account_id='123')
    output = capsys.readouterr()
    assert 'Traceback' not in output.err
