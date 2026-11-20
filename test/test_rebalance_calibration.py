"""Frozen, public-data acceptance of policy decisions without SDK or trading."""
import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import runpy
import sys
from test.test_fx_funding import NOW, market as fx_snapshot
from test.test_order_plan import destination, leaf, parent, rules
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from autorepeater import reporting
from autorepeater.cash_facts import MoneyAmount
from autorepeater.execution import OrderExecutor
from autorepeater.execution_data import AccountReadiness, ExecutionData
from autorepeater.execution_market import ExecutionMarket
from autorepeater.fx_funding import FundingInputs
from autorepeater.portfolio import TargetPortfolio
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_contract import Strategy
from autorepeater.strategy_data import PortfolioSnapshot, StrategyData
from autorepeater.strategy_plan import AllocationProfile, exact_sum
from autorepeater.valuation import SecurityData, Valuation
from scripts import check_rebalance_policy as calibration


D = Decimal


@pytest.fixture(name='pass_case')
def fixture_pass_case(monkeypatch):
    """Supply identical ready facts through the working and replay ports."""
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    snapshot = object()
    strategy.load_snapshot.return_value = snapshot
    strategy.allocation_profile.return_value = AllocationProfile({'X': D(1)}, {'X': D(10)}, D(0))
    strategy.build_plan.side_effect = lambda _, context: replace(
        leaf(budget=str(context.budget)), positions=context.positions,
        target=TargetPortfolio({'X': D(10)}, {'X': context.marks['X']}))
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    execution.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    market = create_autospec(ExecutionMarket, instance=True, spec_set=True)
    model = create_autospec(calibration.ModelExecution, instance=True, spec_set=True)
    engine = AutoRepeater(strategy, data, execution, executor)
    engine.set_debug(True)
    output = create_autospec(reporting.print_rebalance_plan)
    monkeypatch.setattr(reporting, 'print_rebalance_plan', output)
    return SimpleNamespace(strategy=strategy, snapshot=snapshot, execution=execution,
                           executor=executor, market=market, model=model, engine=engine,
                           output=output)


def pass_facts(case, state, trade_rules):
    """Bind each strict read port to the same observation and instrument limits."""
    for port in (case.execution, case.model):
        port.get_destination.return_value = state
        port.get_trade_rules.return_value = trade_rules


def enable_market(case, fx):
    """Exercise the live funding branch without reading books or sending orders."""
    case.engine.market = case.market
    return Valuation(fx, create_autospec(SecurityData, instance=True, spec_set=True),
                     clock=lambda: NOW)


def compare_passes(case, *, valuation=None, funding=None):
    """Compare the complete physical plans without sending orders."""
    assert case.engine.sync_accounts('MODEL', valuation=valuation) == ()
    case.output.assert_called_once()
    working, debug = case.output.call_args.args
    assert debug is True
    replay = calibration.model_plan(case.strategy, case.snapshot, case.model, funding=funding)
    assert working == replay
    case.market.execute.assert_not_called()
    case.executor.submit_order.assert_not_called()
    case.model.submit_order.assert_not_called()
    return replay


@pytest.mark.parametrize('damage, message', [
    ('budget', 'plan path/budget must match context'),
    ('positions', 'plan positions must match context'),
])
def test_pass_pair_rejects_strategy_context_mismatches(pass_case, damage, message):
    """A valid-looking tree cannot replace the budget or discard passed holdings."""
    case = pass_case
    hold = (calibration.entry('X', D(2), D(10)),) if damage == 'positions' else ()
    quantities = {item.uid: item.quantity for item in hold}
    fx = fx_snapshot()
    pass_facts(case, replace(destination(quantities), portfolio=PortfolioSnapshot(hold),
                             fx_snapshot=fx), rules('X'))
    case.strategy.build_plan.side_effect = lambda _, inputs: replace(
        leaf(budget='50' if damage == 'budget' else str(inputs.budget)), positions=())
    case.engine.set_debug(False)

    with pytest.raises(ValueError, match=message):
        case.engine.sync_accounts('MODEL', valuation=enable_market(case, fx))
    with pytest.raises(ValueError, match=message):
        calibration.model_plan(case.strategy, case.snapshot, case.model)

    case.execution.get_trade_rules.assert_not_called()
    case.model.get_trade_rules.assert_not_called()
    case.market.funding_inputs.assert_not_called()
    case.market.execute.assert_not_called()
    case.executor.submit_order.assert_not_called()
    case.model.submit_order.assert_not_called()
    case.output.assert_not_called()


def test_pass_pair_builds_ten_lots_with_one_hundred_rubles(pass_case):
    """The unchanged budget at ten roubles per lot buys ten lots in both passes."""
    pass_facts(pass_case, destination(), rules('X'))
    buy, = compare_passes(pass_case).buys
    assert (buy.uid, buy.side, buy.lots) == ('X', 'BUY', 10)


def test_pass_pair_reads_child_and_destination_uids_with_no_profile_only_reads(pass_case):
    """Read the union of child targets and physical holdings, not every model mark."""
    case = pass_case
    child_uid, held_uid, unassigned_uid = 'A_CHILD', 'H_HELD', 'U_UNASSIGNED'
    holdings = {held_uid: D(1), unassigned_uid: D(1)}
    positions = tuple(calibration.entry(uid, quantity, D(10))
                      for uid, quantity in holdings.items())
    prices = {child_uid: D(10), held_uid: D(10), 'P_PROFILE_ONLY': D(10)}
    case.strategy.allocation_profile.return_value = AllocationProfile(
        {child_uid: D('.5'), held_uid: D('.5')}, prices, D(0))

    def build_plan(_, inputs):
        child = replace(leaf((0,), {child_uid: D(1)}), positions=inputs.positions)
        return replace(parent((child,), unassigned={unassigned_uid: D(9)}),
                       target=TargetPortfolio({held_uid: D(1)}, {held_uid: D(10)}),
                       positions=inputs.positions)

    case.strategy.build_plan.side_effect = build_plan
    expected = [child_uid, held_uid, unassigned_uid]
    state = replace(destination(holdings), portfolio=PortfolioSnapshot(positions))
    pass_facts(case, state, rules(*expected))
    plan = compare_passes(case)
    assert plan.strategy.target.quantities == {held_uid: D(1)}
    assert plan.strategy.unassigned == {unassigned_uid: D(1)}
    assert plan.ownership == {(): {unassigned_uid: D(1)}, (0,): {held_uid: D(1)}}
    case.execution.get_trade_rules.assert_called_once_with('MODEL', expected)
    case.model.get_trade_rules.assert_called_once_with('MODEL', expected)


def test_pass_pair_preserves_nested_shared_and_unassigned_holdings(pass_case):
    """Both paths retain leaf ownership and a zero target in a valid strategy tree."""
    left, left_snapshot = calibration.model_leaf(('shared',))
    middle, middle_snapshot = calibration.model_leaf(('other',))
    right, right_snapshot = calibration.model_leaf(('shared',))
    inner = calibration.compose((left, middle), ('.5', '.5'))
    strategy = calibration.compose((inner, right), ('.5', '.5'))
    pass_case.snapshot = calibration.CompositeSnapshot((
        calibration.CompositeSnapshot((left_snapshot, middle_snapshot)), right_snapshot))
    pass_case.strategy.load_snapshot.return_value = pass_case.snapshot

    def profile(snapshot):
        base = strategy.allocation_profile(snapshot)
        return replace(base, prices={**base.prices, 'zero': D(10)})

    def build_plan(snapshot, inputs):
        plan = strategy.build_plan(snapshot, inputs)
        return replace(plan, target=TargetPortfolio(
            {**plan.target.quantities, 'zero': D(0)}, {**plan.target.prices, 'zero': D(10)}))

    pass_case.strategy.allocation_profile.side_effect = profile
    pass_case.strategy.build_plan.side_effect = build_plan
    holdings = {'shared': D(2), 'unknown': D(1)}
    positions = tuple(calibration.entry(uid, quantity, D(10))
                      for uid, quantity in holdings.items())
    pass_facts(
        pass_case,
        replace(destination(holdings, cash='970'), budget=D(1000),
                portfolio=PortfolioSnapshot(positions)),
        rules('other', 'shared', 'unknown', 'zero'))

    plan = compare_passes(pass_case)
    assert plan.ownership[()] == {'unknown': D(1)}
    portions = [plan.ownership[path]['shared'] for path in ((0, 0), (1,))]
    assert D(0) < portions[0] < portions[1]
    assert exact_sum(portions) == D(2)
    assert plan.strategy.target.quantities['zero'] == D(0)
    assert all(intent.uid != 'zero' for intent in (*plan.sells, *plan.buys))
    pass_case.model.get_trade_rules.assert_called_once_with(
        'MODEL', ['other', 'shared', 'unknown', 'zero'])


def test_pass_pair_uses_rub_destination_mark_before_profile_price(pass_case):
    """Capital recovery observes the explicit RUB mark of a CNY holding."""
    case = pass_case
    holding = replace(calibration.entry('X', D(2), D(10)), currency='cny', rub_price=D(120))
    case.strategy.allocation_profile.return_value = AllocationProfile(
        {'X': D(1)}, {'X': D(150)}, D(0))
    state = replace(destination({'X': D(2)}, cash='0'), budget=D(1000),
                    portfolio=PortfolioSnapshot((holding,)))
    pass_facts(case, state, rules('X', currency='cny'))

    assert compare_passes(case).marks == {'X': D(120)}
    assert case.strategy.build_plan.call_count == 2
    for args, _ in case.strategy.build_plan.call_args_list:
        assert args[1].marks == {'X': D(120)}
        assert args[1].previous_budget == D(240)


@pytest.mark.parametrize('reversed_rows, mark, previous', [
    (False, 100, 300), (True, 110, 330),
])
def test_pass_pair_aggregates_compatible_duplicates_in_input_order(
        pass_case, reversed_rows, mark, previous):
    """The last compatible row supplies metadata, even when its mark is implicit."""
    case = pass_case
    first = replace(calibration.entry('X', D(1), D(100)), rub_price=D(110))
    last = replace(first, quantity=D(2), rub_price=None)
    rows = (last, first) if reversed_rows else (first, last)
    state = replace(destination({'X': D(3)}, cash='0'), budget=D(1000),
                    portfolio=PortfolioSnapshot(rows))
    pass_facts(case, state, rules('X'))

    plan = compare_passes(case)
    assert plan.ownership == {(): {'X': D(3)}}
    assert plan.marks == {'X': D(mark)}
    assert case.strategy.build_plan.call_count == 2
    for args, _ in case.strategy.build_plan.call_args_list:
        assert args[1].positions == (replace(rows[-1], quantity=D(3)),)
        assert args[1].previous_budget == D(previous)


@pytest.mark.parametrize('conflict, message', [
    (True, 'incompatible destination duplicate UID X'),
    (False, 'ownership does not conserve destination UID X'),
])
def test_pass_pair_rejects_inconsistent_destination_facts(pass_case, conflict, message):
    """Metadata conflicts fail early; physical discrepancies use the port quantities."""
    case = pass_case
    rows = (calibration.entry('X', D(1), D(100)),
            calibration.entry('X', D(2), D(101 if conflict else 100)))
    state = replace(destination({'X': D(4)}, cash='0'), budget=D(1000),
                    portfolio=PortfolioSnapshot(rows))
    pass_facts(case, state, rules('X'))

    with pytest.raises(ValueError, match=message):
        case.engine.sync_accounts('MODEL')
    with pytest.raises(ValueError, match=message):
        calibration.model_plan(case.strategy, case.snapshot, case.model)

    assert case.execution.get_trade_rules.call_count == int(not conflict)
    assert case.model.get_trade_rules.call_count == int(not conflict)
    case.executor.submit_order.assert_not_called()
    case.model.submit_order.assert_not_called()
    case.output.assert_not_called()


@pytest.mark.parametrize(('invalid_lot', 'message'), [
    (True, r'trade rules\[X\]: invalid lot'),
    (False, 'marks missing target UID X'),
])
def test_pass_pair_validates_physical_facts_before_native_probe(pass_case, invalid_lot, message):
    """Invalid physical facts fail before funding reads in the real preflight path."""
    case = pass_case
    fx = fx_snapshot()
    pass_facts(case, replace(destination(), fx_snapshot=fx),
               rules('X', lot=0 if invalid_lot else 1))
    if not invalid_lot:
        case.strategy.allocation_profile.return_value = AllocationProfile(
            {'Y': D(1)}, {'Y': D(10)}, D(0))
    case.strategy.build_plan.side_effect = None
    case.strategy.build_plan.return_value = leaf()
    case.market.needs_trading.side_effect = ExecutionMarket.needs_trading
    funding = FundingInputs(native_prices={'X': MoneyAmount('rub', D(10))}, fx_rules={})
    case.market.funding_inputs.return_value = funding
    case.engine.set_debug(False)

    with pytest.raises(ValueError, match=message):
        case.engine.sync_accounts('MODEL', valuation=enable_market(case, fx))
    with pytest.raises(ValueError, match=message):
        calibration.model_plan(case.strategy, case.snapshot, case.model, funding=funding)

    case.execution.get_trade_rules.assert_called_once_with('MODEL', ['X'])
    case.model.get_trade_rules.assert_called_once_with('MODEL', ['X'])
    case.market.needs_trading.assert_not_called()
    case.market.funding_inputs.assert_not_called()
    case.market.execute.assert_not_called()
    case.executor.submit_order.assert_not_called()
    case.model.submit_order.assert_not_called()
    case.output.assert_not_called()


@pytest.mark.parametrize('invalid_fx_lot', [False, True])
def test_pass_pair_uses_the_same_native_funding_facts(pass_case, invalid_fx_lot):
    """Native demand, exchange and dependent lots agree with the cash-pool example."""
    case = pass_case
    fx = fx_snapshot()
    state = replace(destination(cash='1000'), budget=D(1000), fx_snapshot=fx)
    pass_facts(case, state, rules('C', currency='cny', buy=0))
    case.strategy.allocation_profile.return_value = AllocationProfile(
        {'C': D('.9')}, {'C': D(720)}, D('.1'))
    case.strategy.build_plan.side_effect = None
    case.strategy.build_plan.return_value = replace(
        leaf(budget='1000', floor='100'), target=TargetPortfolio({'C': D(1)}, {'C': D(720)}))
    fx_rules = rules('cny-rub', 'usd-rub', lot=1)
    if invalid_fx_lot:
        fx_rules['cny-rub'] = replace(fx_rules['cny-rub'], lot=True)
    funding = FundingInputs(native_prices={'C': MoneyAmount('cny', D(60))}, fx_rules=fx_rules)
    case.market.needs_trading.side_effect = ExecutionMarket.needs_trading
    case.market.funding_inputs.return_value = funding
    valuation = enable_market(case, fx)

    if invalid_fx_lot:
        case.engine.set_debug(False)
        with pytest.raises(ValueError, match=r'trade rules\[cny-rub\]: invalid lot'):
            case.engine.sync_accounts('MODEL', valuation=valuation)
        with pytest.raises(ValueError, match=r'trade rules\[cny-rub\]: invalid lot'):
            calibration.model_plan(case.strategy, case.snapshot, case.model, funding=funding)
        case.market.funding_inputs.assert_called_once_with('MODEL', valuation, ['C'], trading=True)
        case.market.execute.assert_not_called()
        case.executor.submit_order.assert_not_called()
        case.model.submit_order.assert_not_called()
        case.output.assert_not_called()
        return

    result = compare_passes(case, valuation=valuation, funding=funding).funding
    working = case.output.call_args.args[0].funding
    assert result == working
    demand, = result.demands
    assert (demand.uid, demand.side, demand.lots) == ('C', 'BUY', 1)
    exchange, = result.exchanges
    assert (exchange.uid, exchange.side, exchange.lots) == ('cny-rub', 'BUY', 65)
    assert exchange.debits[()] == {'rub': D(780)}
    purchase, = result.purchases
    assert (purchase.intent.uid, purchase.intent.lots) == ('C', 1)
    assert purchase.fx_dependencies == ('cny-rub',)
    assert purchase.executable_now is False
    case.market.funding_inputs.assert_called_once_with('MODEL', valuation, ['C'], trading=True)


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
    root = Path(__file__).resolve().parents[1]
    for path in ('autorepeater/order_intent.py', 'autorepeater/pass_planning.py'):
        assert first['hashes'][path] == sha256((root / path).read_bytes()).hexdigest()
    output = tmp_path / 'report.json'
    calibration.main(['--repetitions', '1', '--output', str(output)])
    first_json = output.read_bytes()
    assert first_json == capsys.readouterr().out.encode('utf-8')
    calibration.main(['--repetitions', '1'])
    assert capsys.readouterr().out.encode('utf-8') == first_json
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
