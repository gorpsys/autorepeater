# pylint: disable=too-many-arguments,too-many-positional-arguments
"""Execution diagnostics distinguish incomplete selection from skipped trading."""
from decimal import Decimal
import json
import logging
import subprocess
import sys
from test.test_autorepeater import client_tinvest  # pylint: disable=unused-import
from unittest.mock import call, create_autospec, patch

import pytest
from t_tech import invest

from autorepeater import logging_config, reporting, runner as runner_module, serverless, strategies
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.portfolio import TargetPortfolio
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_contract import Strategy
from autorepeater.strategy_data import PositionEvent, StrategyData


@pytest.fixture(name='execution')
def fixture_execution(client):
    """Shares are tradable; funds remain in the target but are closed for trading."""
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    strategy.load_snapshot.return_value = object()
    strategy.build_target.return_value = TargetPortfolio(
        {'stock': Decimal('6'), 'bond': Decimal('2'), 'gold': Decimal('1')},
        {uid: Decimal('10') for uid in ('stock', 'bond', 'gold')})
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    client.operations.get_portfolio.side_effect = None
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(instrument_type='currency',
                                 current_price=invest.MoneyValue('RUB', 1, 0),
                                 quantity=invest.Quotation(100, 0))])
    normal = invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING
    closed = invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING
    client.instruments.get_instrument_by.side_effect = lambda **params: invest.InstrumentResponse(
        instrument=invest.Instrument(
            uid=params['id'], ticker=params['id'].upper(), name=params['id'], lot=1,
            class_code='TQBR' if params['id'] == 'stock' else 'TQTF',
            trading_status=normal if params['id'] == 'stock' else closed))
    return AutoRepeater(client, strategy, data), client, data


def test_nontrading_funds_are_reported_without_changing_orders(execution, caplog):
    """Reproduce shares bought and fund budgets left in cash, without real API calls."""
    engine, client, data = execution
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        engine.sync_accounts('dst')
    for uid in ('bond', 'gold'):
        assert any(f'Target position: uid={uid}' in message for message in caplog.messages)
        assert any(f'Skipping buy: {uid}({uid.upper()})' in message
                   and 'SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING' in message
                   and 'class_code=TQTF' in message for message in caplog.messages)
    assert ('Target: instruments=3 estimated_value=90.0 budget=100.0 '
            'estimated_cash=10.0') in caplog.messages
    assert ('Execution: sells=0 buys=1 volume=60.0 '
            'threshold_value=0.4 submit=True') in caplog.messages
    assert all(record.levelno == logging.INFO for record in caplog.records
               if record.message.startswith(('Target', 'Skipping', 'Execution', 'Order result')))
    client.orders.post_order.assert_called_once_with(
        instrument_id='stock', quantity=6, direction=invest.OrderDirection.ORDER_DIRECTION_BUY,
        account_id='dst', order_type=invest.OrderType.ORDER_TYPE_BESTPRICE)
    client.operations.get_portfolio.assert_called_once_with(account_id='dst')
    assert client.instruments.get_instrument_by.call_args_list == [
        call(id_type=invest.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        for uid in ('stock', 'bond', 'gold')]
    assert data.mock_calls == []


@pytest.mark.parametrize('debug, threshold, message', [
    (True, Decimal('0'), 'Execution: debug mode, sells=0 buys=1; no orders submitted'),
    (False, Decimal('0.6'),
     'Execution: sells=0 buys=1 volume=60.0 threshold_value=60.0 submit=False'),
])
def test_suppressed_execution_explains_why(execution, caplog, debug, threshold, message):
    """Debug keeps its no-valuation contract; equality still suppresses trading."""
    engine, client, _ = execution
    engine.set_debug(debug)
    engine.set_threshold(threshold)
    with patch('autorepeater.repeater.get_max_sum_positions_price',
               return_value=Decimal('60')) as volume, \
            caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        engine.sync_accounts('dst')
    assert message in caplog.messages
    assert volume.call_count == (0 if debug else 1)
    client.orders.post_order.assert_not_called()


def test_order_execution_status_is_not_mistaken_for_filled_order(execution, caplog):
    """An order ID alone does not prove execution; log the returned broker status."""
    engine, client, _ = execution
    client.orders.post_order.return_value = invest.PostOrderResponse(
        order_id='test-order', lots_requested=6, lots_executed=0,
        execution_report_status=invest.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        engine.sync_accounts('dst')
    assert ('Order result: id=test-order status=EXECUTION_REPORT_STATUS_NEW '
            'lots_requested=6 lots_executed=0') in caplog.messages


def test_nontrading_sale_reports_same_guard(execution, caplog):
    """The sell guard is unchanged and does not silently discard an instrument."""
    engine, client, _ = execution
    position = invest.PortfolioPosition(quantity=invest.Quotation(2, 0))
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert engine.calc_sell_positions({'bond': position}, {}) == []
    assert any('Skipping sell: bond(BOND)' in message for message in caplog.messages)
    client.orders.post_order.assert_not_called()


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


@pytest.mark.parametrize('side, held, target, reason', [
    ('buy', None, '0.4', 'rounded quantity is not positive'),
    ('buy', None, '0', 'rounded quantity is not positive'),
    ('buy', None, '-1', 'rounded quantity is not positive'),
    ('buy', '1', '1.4', 'rounded quantity is not positive'),
    ('buy', '1', '1', 'target does not require this direction'),
    ('buy', '2', '1', 'target does not require this direction'),
    ('sell', '0.4', None, 'rounded quantity is not positive'),
    ('sell', '-1', None, 'rounded quantity is not positive'),
    ('sell', '1.4', '1', 'rounded quantity is not positive'),
    ('sell', '1', '1', 'target does not require this direction'),
    ('sell', '1', '2', 'target does not require this direction'),
])
def test_every_no_order_quantity_branch_reports_info(execution, caplog, side, held, target, reason):
    """Cover both missing-position branches and every unchanged/rounded delta branch."""
    engine, client, _ = execution
    positions = {}
    if held is not None:
        quantity = Decimal(held)
        units = int(quantity)
        positions['stock'] = invest.PortfolioPosition(
            quantity=invest.Quotation(units, int((quantity - units) * 1_000_000_000)))
    targets = {} if target is None else {'stock': Decimal(target)}
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert getattr(engine, f'calc_{side}_positions')(positions, targets) == []
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert f'Skipping {side}: stock(STOCK)' in record.message
    assert f'reason={reason}' in record.message
    assert f'target={target or "0"}' in record.message
    client.orders.post_order.assert_not_called()


def test_empty_target_and_no_executable_orders_report_info(execution, caplog):
    """A deliberate empty target and an achieved target are different skip reasons."""
    engine, client, _ = execution
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        engine.strategy.build_target.return_value = TargetPortfolio({}, {}, 'test empty component')
        engine.sync_accounts('dst')
        engine.strategy.build_target.return_value = TargetPortfolio(
            {'stock': Decimal('0')}, {'stock': Decimal('10')})
        engine.sync_accounts('dst')
    assert 'Skipping synchronization for destination dst: test empty component' in caplog.messages
    assert 'Skipping trading: no executable orders' in caplog.messages
    assert all(record.levelno == logging.INFO for record in caplog.records
               if record.message.startswith('Skipping'))
    client.orders.post_order.assert_not_called()


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
