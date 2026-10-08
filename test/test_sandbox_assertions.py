"""Live journal assertions tested offline with explicit events and no API access."""
import json
from types import SimpleNamespace
from decimal import Decimal
from typing import Protocol
from unittest.mock import call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError

from e2e.conftest import pytest_runtest_makereport
from e2e.test_sandbox import _check_app_events, _wait_app_operations
from autorepeater.strategy_data import DataAccessError
from scripts.sandbox_lifecycle import SandboxFailure, safe_call
from scripts.sandbox_evidence import MoneyEvidence, OperationEvidence, TradeEvidence


class OperationSource(Protocol):
    """Minimal read and clock contract used by operation reconciliation."""

    def list_operations(self, account_id: str) -> list[OperationEvidence]:
        """Return freshly observed trade and payment facts."""

    def monotonic(self) -> float:
        """Return the injected polling clock."""

    def sleep(self, seconds: float) -> None:
        """Wait between observation attempts."""


@pytest.mark.parametrize('mode', ['settled', 'pending', 'timeout'])
def test_reconciliation_consumes_typed_operation_evidence(mode):
    """Typed operations still prove executed lots and cash, or fail on timeout."""
    session = create_autospec(OperationSource, instance=True, spec_set=True)
    stamp = '2026-10-08T00:00:00+00:00'
    operation = OperationEvidence(
        'op', 'uid', 'figi', 'OPERATION_TYPE_BUY', 'OPERATION_STATE_EXECUTED', 20, 0,
        stamp, MoneyEvidence('rub', '-40'), MoneyEvidence('rub', '2'),
        (TradeEvidence('trade', 20, stamp, MoneyEvidence('rub', '2')),))
    session.list_operations.side_effect = ([[operation]] if mode == 'settled'
                                           else [[], [operation]])
    session.monotonic.side_effect = [0, 40 if mode == 'timeout' else 1]
    if mode == 'timeout':
        with pytest.raises(SandboxFailure, match='operations disagree'):
            _wait_app_operations(session, 'dst', set(), {('uid', 'BUY'): Decimal(20)},
                                 Decimal(-40))
    else:
        _wait_app_operations(session, 'dst', set(), {('uid', 'BUY'): Decimal(20)}, Decimal(-40))
    attempts = 2 if mode == 'pending' else 1
    assert session.list_operations.call_args_list == [call('dst')] * attempts
    assert session.sleep.call_args_list == ([call(2)] if mode == 'pending' else [])


def test_live_report_keeps_safe_short_reason_and_structured_error():
    """Console summaries must show the transport status without exposing traceback locals."""
    error = DataAccessError('private transport')
    error.__cause__ = RequestError(StatusCode.DEADLINE_EXCEEDED, 'private', {'token': 'private'})
    item = SimpleNamespace(nodeid='e2e/scenario')
    report = SimpleNamespace(when='call', sections=['private captured output'])
    hook = pytest_runtest_makereport(item, SimpleNamespace(excinfo=SimpleNamespace(value=error)))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.longrepr == 'e2e/scenario: DataAccessError; grpc=DEADLINE_EXCEEDED'
    assert report.sandbox_error[1]['grpc_status'] == 'DEADLINE_EXCEEDED'
    assert report.sections == []
    assert item.report_call is report
    assert 'private' not in json.dumps(report.__dict__)


def test_setup_failure_report_shows_safe_broker_code():
    """A hidden SDK traceback must not hide the reason for setup rejection."""
    def post_order(**_):
        raise RequestError(StatusCode.FAILED_PRECONDITION, '30052', {'token': 'private'})

    with pytest.raises(SandboxFailure) as caught:
        safe_call(post_order)
    item = SimpleNamespace(nodeid='e2e/setup')
    report = SimpleNamespace(when='call', sections=['private captured output'])
    hook = pytest_runtest_makereport(
        item, SimpleNamespace(excinfo=SimpleNamespace(value=caught.value)))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert report.longrepr == ('e2e/setup: sandbox API failed: post_order; '
                               'grpc=FAILED_PRECONDITION; api_code=30052')
    assert report.sections == []
    assert 'private' not in json.dumps(report.__dict__)


def trade_events(side, *, confirmed=True):
    """Keep receipt and confirmation separate, as in the real application journal."""
    events = [{'event': 'submit', 'message': f'side={side}'},
              {'event': 'response', 'side': side}]
    if confirmed:
        events.append({'event': 'confirm', 'message': f'side={side}'})
    return events


@pytest.mark.parametrize('empty', [False, True])
def test_noop_or_empty_requires_no_submissions(empty):
    """Both stable no-op and rejected empty main prohibit any application order."""
    assert _check_app_events([], sells=False, buys=False, no_orders=True, empty=empty) == (
        [], [], [])
    with pytest.raises(SandboxFailure, match='unexpected app order'):
        _check_app_events(trade_events('BUY'), sells=False, buys=False,
                          no_orders=True, empty=empty)


def test_confirmed_sell_then_buy_is_accepted():
    """Receipts are preserved in order, after sell confirmation and before the next phase."""
    events = trade_events('SELL') + trade_events('BUY')
    responses, sells, buys = _check_app_events(
        events, sells=True, buys=True, no_orders=False, empty=False)
    assert responses == [events[1], events[4]]
    assert sells == [events[1]] and buys == [events[4]]


@pytest.mark.parametrize('events,sells,buys,reason', [
    ([], True, False, 'mandatory rebalance SELL'),
    ([], False, True, 'mandatory BUY'),
    (trade_events('SELL'), False, False, 'forbidden SELL'),
    ([{'event': 'submit', 'message': 'side=BUY'}], False, False, 'actual broker receipt'),
    (trade_events('SELL', confirmed=False), True, False, 'unconfirmed sale'),
    (trade_events('SELL', confirmed=False) + trade_events('BUY'), True, True,
     'BUY submitted before SELL FILL'),
])
def test_journal_rejects_missing_or_out_of_order_trade_facts(events, sells, buys, reason):
    """Refactoring must not relax receipts, required trades, or sell-before-buy checks."""
    with pytest.raises(SandboxFailure, match=reason):
        _check_app_events(events, sells=sells, buys=buys, no_orders=False, empty=False)
