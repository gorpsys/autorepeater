# pylint: disable=too-many-arguments,too-many-positional-arguments
"""Execution diagnostics distinguish incomplete selection from skipped trading."""
from decimal import Decimal
from dataclasses import replace
import json
import logging
import subprocess
import sys
from test.test_autorepeater import client_tinvest  # pylint: disable=unused-import
from test.test_runtime_policy import fixture_runtime  # pylint: disable=unused-import
from test.test_order_plan import destination, leaf, rules
from unittest.mock import patch

import pytest

from autorepeater import logging_config, reporting, runner as runner_module, serverless, strategies
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import PositionEvent
from autorepeater.execution import ExecutionReceipt, OrderExecutionError
from autorepeater.repeater import AutoRepeater
from autorepeater.order_plan import build_order_plan


@pytest.fixture(name='execution')
def fixture_execution(client):
    """SDK client is used only by the explicit browse lifecycle test."""
    return None, client, None


def test_policy_money_rules_and_debug_are_info(runtime, caplog):
    """Debug retains every validated financial and physical explanation without I/O for labels."""
    strategy, data, execution, executor = runtime
    engine = AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(True)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        engine.sync_accounts('dst')
    for fragment in ('Target position', 'mode=BUY_ONLY', 'metric=None', 'cash_floor=0',
                     'scoped_money', 'Rules UID stock', 'Intent UID stock', 'debug=True'):
        assert any(fragment in message for message in caplog.messages)
    assert all(record.levelno == logging.INFO for record in caplog.records)
    executor.submit_order.assert_not_called()
    data.find_instruments.assert_not_called()


def test_partial_result_is_error_and_stops(runtime, caplog):
    """An order identifier with NEW or a partial fill cannot yield a successful pass."""
    strategy, data, execution, executor = runtime
    executor.submit_order.return_value = ExecutionReceipt(
        'stock', 'BUY', 'pending', 'NEW', 10, 0, {})
    executor.submit_order.side_effect = None
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME), pytest.raises(OrderExecutionError):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    assert any(record.levelno == logging.ERROR and 'status NEW' in record.message
               for record in caplog.records)


def test_unavailable_bestprice_is_info_skip(runtime, caplog):
    """Closed instruments remain in main; current permissions defer physical orders."""
    strategy, data, execution, executor = runtime
    execution.get_trade_rules.return_value['stock'] = replace(
        execution.get_trade_rules.return_value['stock'], bestprice_order_available=False)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        AutoRepeater(strategy, data, execution, executor).sync_accounts('dst')
    assert any('SKIP' in message for message in caplog.messages)
    assert any('BESTPRICE=False' in message for message in caplog.messages)
    executor.submit_order.assert_not_called()












@pytest.mark.parametrize('algoritm, src', [('COMPOSITE', 'BALANCED'), ('INDEX', 'IMOEX')])
def test_cloud_logs_resolved_selection_without_credentials(algoritm, src, caplog, monkeypatch):
    """Overrides must be visible; the token must never appear in diagnostic output."""
    monkeypatch.setenv('INVEST_TOKEN', 'secret-test-token')
    with patch.object(serverless, 'Runner', autospec=True) as runner, \
            patch.object(serverless, 'configure_yc_logging'), \
            caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        serverless.handler(
            {'queryStringParameters': {'algoritm': algoritm, 'src': src, 'dst': 'dst'}}, None)
    assert f'Launch: algoritm={algoritm} src={src} dst=dst' in caplog.messages
    assert not any('secret-test-token' in message for message in caplog.messages)
    runner.return_value.run_sync.assert_called_once_with()


def test_target_diagnostics_handle_zero_signed_and_extra_prices(caplog):
    """Report the target contract without inspecting strategy internals or spare prices."""
    target = TargetPortfolio({'zero': Decimal('0'), 'short': Decimal('-0.5')},
                             {'zero': Decimal('1'), 'short': Decimal('2'), 'unused': None})
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_target(target, Decimal('100'))
    assert caplog.messages == [
        'Target position: uid=zero quantity=0.0 price=1.0 estimated_value=0.0',
        'Target position: uid=short quantity=-0.5 price=2.0 estimated_value=-1.0',
        'Target: instruments=2 estimated_value=-1.0 budget=100.0 estimated_cash=101.0',
    ]


def test_rebalance_diagnostics_format_all_decimal_values(caplog):
    """Money and ownership maps use ordinary decimal text, including nano and integers."""
    node = leaf(target={'X': Decimal(1)}, budget='1', floor='0.000000001')
    node = replace(node, decision=replace(node.decision, metric=Decimal('1E-9'),
                                         limit=Decimal(0)))
    plan = build_order_plan(node, destination(cash='1'), {(): {}},
                            {'X': Decimal('1E-9')}, rules('X'))
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_rebalance_plan(plan, True)
    messages = '\n'.join(caplog.messages)
    assert 'metric=0.000000001 limit=0.0' in messages
    assert 'budget=1.0 cash_floor=0.000000001' in messages
    assert 'money={rub: 1.0} scoped_money={(): 0.999999999}' in messages
    assert 'buy_money=100000000000000000000.0' in messages
    assert 'pieces={(): 1.0}' in messages
    assert 'Decimal(' not in messages and 'E-' not in messages and 'E+' not in messages






def test_untriggered_event_reports_info(caplog):
    """Pure event predicates remain silent; reporting explains the engine's decision."""
    event = PositionEvent(False, '', (), (), 'ping')
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_skipped_strategy_event(event)
    assert caplog.records[-1].levelno == logging.INFO
    assert caplog.messages[-1] == (
        'Skipping synchronization: event did not trigger rebalance; '
        'account= has_position=False securities=0 money=0')


@pytest.mark.parametrize('mode', ['run', 'run_sync'])
def test_missing_destination_reports_info(execution, mode, caplog):
    """The optional portfolio-view mode must explicitly say it does not trade."""
    _, client, _ = execution
    with patch.object(runner_module, 'Client', autospec=True) as sdk, \
            patch.object(runner_module, 'print_all_portfolio'), \
            patch.object(runner_module, 'configure_local_logging'), \
            caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        sdk.return_value.__enter__.return_value = client
        runner = runner_module.Runner(
            'test-token', strategies.prepare_strategy('INDEX', 'IMOEX'), None)
        getattr(runner, mode)()
    assert caplog.messages == [f'Skipping trading: destination account is not set; mode={mode}']
    assert caplog.records[0].levelno == logging.INFO
    client.operations.get_portfolio.assert_not_called()
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('configure', ['configure_local_logging', 'configure_yc_logging'])
def test_info_diagnostics_enabled_by_real_logging_configuration(configure, monkeypatch):
    """Production log levels must not silently filter out the new skip diagnostics."""
    logger = logging_config.logger
    monkeypatch.setattr(logger, 'level', logging.WARNING)
    monkeypatch.setattr(logger, 'handlers', [])
    monkeypatch.setattr(logger, 'propagate', True)
    monkeypatch.setattr(logging.getLogger(), 'level', logging.WARNING)
    getattr(logging_config, configure)()
    assert logger.isEnabledFor(logging.INFO)


@pytest.mark.parametrize('configure', ['configure_local_logging', 'configure_yc_logging'])
def test_info_diagnostics_reach_stderr_in_fresh_process(configure):
    """A clean CLI process needs an output handler, not only an enabled log level."""
    script = f'''
from autorepeater import logging_config, reporting
logging_config.{configure}()
logging_config.{configure}()
reporting.print_missing_destination('probe')
'''
    result = subprocess.run([sys.executable, '-c', script], check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    message = 'Skipping trading: destination account is not set; mode=probe'
    if configure == 'configure_yc_logging':
        record = json.loads(lines[0])
        assert record['level'] == 'INFO'
        assert record['message'] == message
    else:
        assert lines[0] == 'INFO:tinkoffBot:' + message
