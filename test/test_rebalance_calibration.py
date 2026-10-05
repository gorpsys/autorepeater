"""Frozen, public-data acceptance of policy decisions without SDK or trading."""
import json
from decimal import Decimal
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

import pytest

from scripts import check_rebalance_policy as calibration


D = Decimal


@pytest.fixture(name='inputs', scope='module')
def fixture_inputs():
    """One immutable capture and independent validation seed."""
    return calibration.load_inputs()


def test_account_independent_frozen_validation(inputs):
    """The accepted limit is assessed, never refitted on the holdout."""
    manifest, capture = inputs
    report = calibration.account_validation(manifest, capture)
    assert report['count'] == 24000
    assert report['counts'] == manifest['historical_validation_counts']
    assert report['agreement'] == '0.913375'
    assert report['limit'] == '0.0092'
    assert report['model_accounts'] is True
    assert report['real_account_snapshot_available'] is False
    assert report['public_quote_count'] == 44
    assert report['new']['turnover'] > 0
    assert report['old']['order_count'] > 0
    assert report['new']['estimated_commission'] == (
        report['new']['turnover'] * D(manifest['commission_rate']))


@pytest.mark.parametrize('lot, old_allowed', [(1, True), (10, False), (100, False)])
def test_old_oracle_preserves_lot_volume_units(lot, old_allowed):
    """The frozen oracle intentionally prices lots without multiplying lot size."""
    current = {'a': D(5100), 'b': D(4900)}
    target = {'a': D(5000), 'b': D(5000)}
    orders, allowed = calibration.legacy_orders(current, target, {'a': D(1), 'b': D(1)},
                                               {'a': lot, 'b': lot}, D(10000) / D('.99'))
    assert allowed is old_allowed
    assert len(orders) == 2
    assert orders[0][:3] == ('a', 'SELL', 100 // lot)


def test_oracle_strict_equality_and_zero_delta():
    """No volume at equality and no orders on an unchanged portfolio."""
    assert calibration.legacy_orders({'a': D(1)}, {'a': D(0)}, {'a': D(1)},
                                     {'a': 1}, D(250))[1] is False
    assert calibration.legacy_orders({'a': D(1)}, {'a': D(1)}, {'a': D(1)},
                                     {'a': 1}, D(1)) == ([], False)


def test_index_repeats_monthly_tail_and_price_paths(inputs):
    """Lot budgets, unchanged reruns and monthly funding use real plan methods."""
    manifest, capture = inputs
    rows = calibration.index_acceptance(manifest, capture)
    assert len(rows) == len(manifest['index_budgets'])
    rejected = [row for row in rows if 'main_error' in row]
    assert rejected and all('positive quantity' in row['main_error'] for row in rejected)
    rows = [row for row in rows if 'main_error' not in row]
    assert all(row['repeat_equal'] for row in rows)
    assert all(row['post_trade_repeat_orders'] == 0 for row in rows)
    assert all(row['monthly_deposit'] == D(2000) for row in rows)
    assert all(row['cash_nonnegative'] for row in rows)
    assert any(row['tail_added'] for row in rows)
    assert all(row['price_metric'] is None or row['price_metric'] >= 0 for row in rows)
    assert any(row['price_metric'] for row in rows)
    assert any(row['extrapolated'] for row in rows)
    assert any(row['off_grid'] for row in rows)
    assert all(row['months'] == 12 for row in rows)


def test_every_builtin_table_boundary_and_historical_limits(inputs):
    """No gap at exact boundaries; historical calibration values are preserved."""
    rows = calibration.table_acceptance(inputs[1])
    assert rows['IMOEX']['ranges'] == 95
    assert rows['IMOEX']['historical_limits_equal']
    assert all(row['starts_at_zero'] and row['ends_at_null']
               and row['boundaries_checked'] for row in rows.values())


def test_composite_acceptance_and_ownership_risk():
    """Repeat reserve-only shortage never invents sale permission or cash."""
    report = calibration.composite_acceptance()
    assert report['drift_budgets'] == ['60000', '30000']
    assert report['capital_shortage_budgets'] == ['60300', '29700']
    assert report['reserve_only_repeated_sales'] == 0
    assert report['reserve_restored_with_deposit']
    assert report['unassigned_unfunded_buys'] == 0
    assert report['nested_cash_floor'] == D(262)
    assert report['shared_pieces'] == [D('7.5'), D('2.5')]
    assert report['protected_owner_sales'] == 0
    assert report['shared_price_reattribution_changes']
    assert report['ownership_history_unknown'] is True
    assert report['shared_component_metric'] == D('.375')
    assert report['shared_child_metrics'][1] == 1


def test_composite_filled_replay_and_unchanged_second_run():
    """Actual bounded controller sells first, buys with fresh cash, then stays put."""
    first, snap_a = calibration.model_leaf(('a',))
    second, snap_b = calibration.model_leaf(('b',))
    root = calibration.compose((first, second), ('.6', '.3'))
    snapshot = calibration.CompositeSnapshot((snap_a, snap_b))
    model = calibration.ModelExecution({'a': D(8000), 'b': D(2000)},
                                       {'a': D(10), 'b': D(10)}, {'a': 1, 'b': 1}, D(0))
    plan = calibration.model_plan(root, snapshot, model)
    assert not plan.buys
    calibration.execute_plan('MODEL', plan, model, model)
    assert model.orders == [('a', 'SELL', 2000, 1), ('b', 'BUY', 1000, 1)]
    assert model.quantities == {'a': D(6000), 'b': D(3000)}
    assert model.cash == D(10000)
    again = calibration.model_plan(root, snapshot, model)
    calibration.execute_plan('MODEL', again, model, model)
    assert not again.sells and not again.buys
    assert len(model.orders) == 2


def test_nested_unassigned_proceeds_need_a_confirmed_fill():
    """Modelled nested unassigned value is economic capital but no initial cash."""
    child, child_snapshot = calibration.model_leaf()
    root = calibration.compose((child,), ('1',))
    snapshot = calibration.CompositeSnapshot((child_snapshot,))
    model = calibration.ModelExecution({'unknown': D(100)},
                                       {'a': D(10), 'unknown': D(10)},
                                       {'a': 1, 'unknown': 1}, D(0))
    plan = calibration.model_plan(root, snapshot, model)
    assert not plan.buys
    assert plan.ownership[()] == {'unknown': D(100)}
    calibration.execute_plan('MODEL', plan, model, model)
    assert model.orders == [('unknown', 'SELL', 100, 1), ('a', 'BUY', 100, 1)]
    assert model.cash == 0


def test_leaf_unassigned_is_outside_its_control_portfolio():
    """Match the engine: an unassigned paper cannot create a leaf drift signal."""
    # pylint: disable=no-member
    child, snapshot = calibration.model_leaf()
    model = calibration.ModelExecution({'unknown': D(100)},
                                       {'a': D(10), 'unknown': D(10)},
                                       {'a': 1, 'unknown': 1}, D(0))
    plan = calibration.model_plan(child, snapshot, model)
    assert plan.strategy.decision.mode == calibration.TradeMode.BUY_ONLY
    assert not plan.strategy.positions
    assert plan.strategy.unassigned == {'unknown': D(100)}
    assert not plan.buys and len(plan.sells) == 1


def test_cli_module_entrypoint_is_offline(monkeypatch, capsys):
    """The executable module needs no credentials and never imports the SDK."""
    monkeypatch.setattr(sys, 'argv', ['check_rebalance_policy', '--repetitions', '1'])
    with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')) as connect:
        runpy.run_path(str(calibration.ROOT / 'scripts/check_rebalance_policy.py'),
                       run_name='__main__')
    connect.assert_not_called()
    report = json.loads(capsys.readouterr().out)
    assert report['account']['count'] == 240
    assert 'no live sandbox' in report['scope']


def test_report_and_cli_are_reproducible(inputs, tmp_path, capsys):
    """Reports omit wall time and record input hashes instead of credentials."""
    first = calibration.make_report(repetitions=1)
    second = calibration.make_report(repetitions=1)
    assert first == second
    assert first['hashes']['test/data/imoex_snapshot.json'] == inputs[0]['capture_sha256']
    output = tmp_path / 'report.json'
    calibration.main(['--repetitions', '1', '--output', str(output)])
    assert json.loads(output.read_text()) == json.loads(capsys.readouterr().out)
    calibration.main(['--repetitions', '1'])
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_text())
    with pytest.raises(SystemExit):
        calibration.main(['--repetitions', '0'])


def test_capture_hash_is_mandatory(tmp_path, inputs):
    """Changed input bytes cannot silently pass as the frozen public capture."""
    manifest, _ = inputs
    path = tmp_path / 'capture.json'
    path.write_text('{}')
    with pytest.raises(ValueError, match='capture SHA-256'):
        calibration.load_inputs(capture_path=path)
    assert Path(calibration.ROOT / 'test/data' / manifest['capture']).is_file()
