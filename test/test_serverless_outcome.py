"""Offline tests of the one-shot execution result and its cloud projection."""
import logging
from contextlib import nullcontext
from unittest.mock import create_autospec, patch, sentinel

import pytest

from autorepeater import runner as runner_module, serverless
from autorepeater.execution import (
    ExecutionOutcome, ExecutionReceipt, ExecutionState, OrderExecutionError,
    confirmed_receipts_from_error,
)
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_contract import PreparedStrategy

FILL_RECEIPT = ExecutionReceipt('private-uid', 'SELL', 'private-order', 'FILL', 1, 1, {})


@pytest.fixture(name='one_shot')
def fixture_one_shot(monkeypatch):
    """Keep the real Runner and handler on a strict offline sync mock."""
    prepared = PreparedStrategy(lambda _source: sentinel.strategy, sentinel.source, 'offline')
    engine = create_autospec(AutoRepeater, instance=True, spec_set=True)
    monkeypatch.setenv('INVEST_TOKEN', 'credential-marker')
    monkeypatch.setattr(runner_module, 'configure_local_logging', lambda: None)
    monkeypatch.setattr(serverless, 'configure_yc_logging', lambda: None)
    monkeypatch.setattr(serverless, 'prepare_strategy', lambda *_args: prepared)
    with (
        patch.object(runner_module, 'create_strategy', autospec=True),
        patch.object(runner_module.Runner, '_create_client', autospec=True,
                     return_value=nullcontext(sentinel.client)),
        patch.object(runner_module.Runner, '_create_repeater', autospec=True,
                     return_value=engine),
    ):
        yield prepared, engine


@pytest.mark.parametrize('outcome', [
    ExecutionOutcome(ExecutionState.NOOP, 'finished', 'no_execution'),
    ExecutionOutcome(ExecutionState.DEFERRED, 'before_sales', 'snapshot_not_ready'),
    ExecutionOutcome(ExecutionState.PARTIAL, 'after_sales', 'settlement_timeout', (FILL_RECEIPT,)),
    ExecutionOutcome(ExecutionState.FINISHED, 'finished', 'queue_completed', (FILL_RECEIPT,)),
], ids=['noop', 'deferred', 'partial', 'finished'])
def test_cloud_projects_result_to_one_header_and_one_safe_log(one_shot, caplog, outcome):
    """The HTTP body stays compatible and fill identifiers stay out of the summary."""
    _, engine = one_shot
    engine.sync_accounts.return_value = outcome
    event = {'queryStringParameters': {'algoritm': 'INDEX', 'src': 'offline', 'dst': 'dst'}}
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        response = serverless.handler(event, None)

    assert response == {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/plain', 'X-Execution-Outcome': outcome.state.value},
        'isBase64Encoded': False,
        'body': 'Success sync, offline dst!',
    }
    assert caplog.messages == [
        'Launch: algoritm=INDEX src=offline dst=dst',
        f'Pass outcome: state={outcome.state.value} stage={outcome.stage} reason={outcome.reason} '
        f'confirmed_orders={len(outcome.receipts)}',
    ]
    assert [record.levelno for record in caplog.records] == [logging.INFO, logging.INFO]
    engine.sync_accounts.assert_called_once_with('dst')


def test_cloud_rejects_missing_result_without_success_log(one_shot, caplog):
    """An invalid finite-sync result cannot become a success response."""
    _, engine = one_shot
    engine.sync_accounts.return_value = None
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME), pytest.raises(
            RuntimeError, match='cloud sync returned no execution outcome'):
        serverless.handler({'queryStringParameters': {'dst': 'dst'}}, None)
    assert all(not message.startswith('Pass outcome:') for message in caplog.messages)
    engine.sync_accounts.assert_called_once_with('dst')


def test_cloud_propagates_stopped_pass_after_fill(one_shot, caplog):
    """The original exception and accepted FILL survive both entrypoints."""
    _, engine = one_shot
    stopped = OrderExecutionError('offline stop', request_id='request-id')
    cause = TimeoutError('offline timeout')
    stopped.__cause__ = cause
    receipts = (FILL_RECEIPT,)
    stopped.confirmed_receipts = receipts
    engine.sync_accounts.side_effect = stopped
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME), pytest.raises(
            OrderExecutionError) as caught:
        serverless.handler({'queryStringParameters': {'dst': 'dst'}}, None)
    assert caught.value is stopped
    assert caught.value.__cause__ is cause
    assert caught.value.request_id == 'request-id'
    assert confirmed_receipts_from_error(caught.value) is receipts
    assert all(not message.startswith('Pass outcome:') for message in caplog.messages)
    engine.sync_accounts.assert_called_once_with('dst')


@pytest.mark.parametrize('dst', ['dst', None])
def test_runner_returns_pass_outcome_or_reports_missing_destination(one_shot, caplog, dst):
    """Direct callers get the result unchanged, or a diagnostic when skipping sync."""
    prepared, engine = one_shot
    outcome = ExecutionOutcome(ExecutionState.NOOP, 'finished', 'no_execution')
    engine.sync_accounts.return_value = outcome
    runner = runner_module.Runner('credential-marker', prepared, dst)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = runner.run_sync()
    if dst is None:
        assert result is None
        engine.sync_accounts.assert_not_called()
        assert caplog.messages == [
            'Skipping trading: destination account is not set; mode=run_sync']
    else:
        assert result is outcome
        engine.sync_accounts.assert_called_once_with(dst)
        assert caplog.messages == []
