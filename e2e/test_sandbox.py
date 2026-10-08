"""Sequential real trades at present quotes, through the public finite app entry."""
from decimal import Decimal
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

import pytest
from t_tech.invest import OrderDirection

from autorepeater.runner import Runner
from autorepeater.strategies import prepare_strategy
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.execution_data import ExecutionSnapshot
from autorepeater.strategy_data import InstrumentInfo
from scripts.sandbox_lifecycle import SandboxFailure, safe_call
from scripts.sandbox_support import FILL, OrderJournal, SandboxSession

D = Decimal
LiveCase = tuple[SandboxSession, OrderJournal]


class AppEvents(NamedTuple):
    """Checked app receipts split by direction, with their original order preserved."""
    responses: list[dict[str, object]]
    sells: list[dict[str, object]]
    buys: list[dict[str, object]]


class ScenarioCatalog(NamedTuple):
    """Resolved instruments and the temporary ACCOUNT config directory."""
    instruments: list[InstrumentInfo]
    account_dir: Path


class ExpectedAppState(NamedTuple):
    """Independent position deltas and operation totals after confirmed orders."""
    quantities: dict[str, D]
    operation_totals: dict[tuple[str, str], D]


class AppPass(NamedTuple):
    """Receipts and settled account state from a finite application pass."""
    responses: list[dict[str, object]]
    state: ExecutionSnapshot


def check(condition: object, reason: str) -> None:
    """A fixed invariant reason is safe for console/JUnit without pytest local reprs."""
    if not condition:
        raise SandboxFailure(reason)


def create_account(session: SandboxSession, scenario: str, role: str) -> str:
    """Confirm the broker preserves owned names before setup mutations."""
    account_id = session.lifecycle.create(scenario, role)
    session.lifecycle.verify_name(account_id)
    return account_id


def config_catalog(live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   tickers: tuple[str, ...] = ('SBER', 'OBLG')) -> ScenarioCatalog:
    # pylint: disable=too-many-locals
    """Real quote references keep compact equal-weight targets far from cutoffs."""
    session, _ = live
    instruments = [session.instrument(ticker) for ticker in tickers]
    index = tmp_path / 'index'
    composite = tmp_path / 'composite'
    account = tmp_path / 'account'
    for directory in (index, composite, account):
        directory.mkdir()
    records = [{'ticker': ticker, 'reference_price': session.quotes[ticker].price,
                'effective_quantity': '1', 'free_float': '1', 'weight_limit': '1',
                'reference_weight': str(D(100) / len(tickers)),
                'reference_index_capitalization': '1'} for ticker in tickers]
    configs = {'PAIR': {'name': 'PAIR', 'reserve': '0.10', 'max_lot_weight_error': '0.05',
                        'min_position_value': '0', 'instruments': records[:2],
                        'allocation_drift_limits': [{'budget_from': '0', 'budget_to': None,
                                                    'upper_inclusive': False, 'limit': '0.10'}]}}
    for number, record in enumerate(records):
        configs['SOLO' + str(number)] = {
            **configs['PAIR'], 'name': 'SOLO' + str(number), 'instruments': [record],
            'allocation_drift_limits': [{'budget_from': '0', 'budget_to': None,
                                        'upper_inclusive': False, 'limit': '0'}]}
    for name, config in configs.items():
        (index / (name + '.json')).write_text(json.dumps(config), encoding='utf-8')
    mix = {'name': 'MIX', 'component_drift_limit': '0.20', 'components': [
        {'algoritm': 'INDEX', 'src': 'SOLO0', 'weight': '0.5'},
        {'algoritm': 'INDEX', 'src': 'SOLO1', 'weight': '0.5'}]}
    inner = {'name': 'INNER', 'component_drift_limit': '0.20', 'components': [
        {'algoritm': 'INDEX', 'src': 'PAIR', 'weight': '0.5'},
        {'algoritm': 'INDEX', 'src': 'SOLO2', 'weight': '0.5'}]}
    for config in (mix, inner) if len(tickers) == 3 else (mix,):
        (composite / (config['name'] + '.json')).write_text(json.dumps(config), encoding='utf-8')
    for name in ('IMOEX_CONFIG_PATH', 'ACCOUNT_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(index))
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(composite))
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(account))
    session.configs = {**configs, 'MIX': mix}
    if len(tickers) == 3:
        session.configs['INNER'] = inner
    return ScenarioCatalog(instruments, account)


def prepare_case(session: SandboxSession, algorithm: str, source: str,
                 instruments: list[InstrumentInfo], account_dir: Path,
                 scenario: str) -> PreparedStrategy:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """ACCOUNT uses the real source ID unchanged through the normal config loader."""
    source_id = None
    if algorithm == 'ACCOUNT':
        source_id = create_account(session, scenario, 'src')
        config = {'name': 'SOURCE', 'source_account_id': source_id,
                  'reserve': '0.10', 'allocation_drift_limit': '0.10'}
        session.configs['SOURCE'] = config
        (account_dir / 'source.json').write_text(json.dumps(config), encoding='utf-8')
    prepared = prepare_strategy(algorithm, source)
    if source_id is not None:
        session.pay_in(source_id, D(100000))
        for instrument in instruments:
            quote = D(session.quotes[instrument.ticker].price)
            session.buy_setup(source_id, instrument, int(D(40000) / (quote * instrument.lot)))
    return prepared


def _run_application(account_id: str, prepared: PreparedStrategy, *, empty: bool) -> None:
    """Exercise the real entrypoint, checking an expected empty main before any orders."""
    try:
        Runner(token=os.environ['SANDBOX_TOKEN'], prepared_strategy=prepared,
               dst=account_id, sandbox=True).run_sync()
    except ValueError as error:
        if not empty:
            raise
        check(str(error) == 'plan target must contain a positive quantity',
              'wrong main-empty rejection reason')
    else:
        check(not empty, 'main-empty must fail before app orders')


def _check_app_events(events: list[dict[str, object]], *, sells: bool, buys: bool,
                      no_orders: bool, empty: bool) -> AppEvents:
    """Verify required receipts, trading permissions and SELL-before-BUY confirmation."""
    submitted = [item for item in events if item['event'] == 'submit']
    responses = [item for item in events if item['event'] == 'response']
    check(len(submitted) == len(responses), 'every app submission needs an actual broker receipt')
    sell_responses = [item for item in responses if item['side'] == 'SELL']
    buy_responses = [item for item in responses if item['side'] == 'BUY']
    if no_orders or empty:
        check(not submitted, 'unexpected app order in stable no-op; inspect live quotes and budget')
    if sells:
        check(sell_responses, 'mandatory rebalance SELL missing')
    else:
        check(not sell_responses, 'forbidden SELL in BUY-only pass')
    if buys:
        check(buy_responses, 'mandatory BUY missing')
    pending_sales = 0
    for item in events:
        if item['event'] == 'submit' and 'side=SELL' in item['message']:
            pending_sales += 1
        if item['event'] == 'confirm' and 'side=SELL' in item['message']:
            pending_sales -= 1
        if item['event'] == 'submit' and 'side=BUY' in item['message']:
            check(pending_sales == 0, 'BUY submitted before SELL FILL confirmation')
    check(pending_sales == 0, 'unconfirmed sale in app journal')
    return AppEvents(responses, sell_responses, buy_responses)


def _expected_app_positions(session: SandboxSession, account_id: str,
                            instruments: list[InstrumentInfo], quantities: dict[str, D],
                            responses: list[dict[str, object]]) -> ExpectedAppState:
    """Read actual order states and independently reconstruct the physical position deltas."""
    lots = {item.uid: item.lot for item in instruments}
    expected = dict(quantities)
    operation_totals = {}
    for item in responses:
        check(item['requested'] > 0 and isinstance(item['requested'], int), 'invalid app lot count')
        broker = safe_call(session.client.orders.get_order_state,
                           account_id=account_id, order_id=item['order_id'])
        direction = (OrderDirection.ORDER_DIRECTION_BUY if item['side'] == 'BUY'
                     else OrderDirection.ORDER_DIRECTION_SELL)
        session.check_order(broker, item['uid'], item['requested'], direction, item['order_id'])
        check(broker.execution_report_status == FILL, 'broker did not confirm FILL')
        delta = D(item['requested'] * lots[item['uid']])
        key = (item['uid'], item['side'])
        operation_totals[key] = operation_totals.get(key, D(0)) + delta
        expected[item['uid']] = expected.get(item['uid'], D(0)) + (
            delta if item['side'] == 'BUY' else -delta)
        if expected[item['uid']] == 0:
            del expected[item['uid']]
    return ExpectedAppState(expected, operation_totals)


def _wait_app_operations(session: SandboxSession, account_id: str, prior_operations: set[str],
                         operation_totals: dict[tuple[str, str], D], cash_delta: D) -> None:
    """Wait for broker trade details and cash payments to agree with confirmed receipts."""
    deadline = session.monotonic() + 30
    while True:
        actual_totals = {}
        new_operations = [item for item in session.list_operations(account_id)
                          if item.id not in prior_operations]
        for item in new_operations:
            if item.type not in ('OPERATION_TYPE_BUY', 'OPERATION_TYPE_SELL'):
                continue
            check(item.state == 'OPERATION_STATE_EXECUTED' and item.remaining == 0,
                  'broker trade operation is incomplete')
            check(sum(trade.quantity for trade in item.trades) == item.quantity,
                  'broker trade details disagree with operation quantity')
            key = (item.uid, item.type.removeprefix('OPERATION_TYPE_'))
            actual_totals[key] = actual_totals.get(key, D(0)) + D(item.quantity)
        payments = sum((D(item.payment.amount) for item in new_operations
                        if item.payment.currency.lower() == 'rub'), D(0))
        if actual_totals == operation_totals and abs(payments - cash_delta) <= D('.01'):
            break
        check(session.monotonic() < deadline,
              'broker operations disagree with receipts or cash change')
        session.sleep(2)


def app_pass(live: LiveCase, account_id: str, prepared: PreparedStrategy,
             instruments: list[InstrumentInfo], *, sells: bool = False, buys: bool = False,
             no_orders: bool = False, reserve: D = D('.10'), empty: bool = False) -> AppPass:
    # pylint: disable=too-many-arguments,too-many-locals
    """Independent receipts, operation deltas and physical conservation, not plan replay."""
    session, journal = live
    before = session.state(account_id)
    session.records.append({'event': 'app_start', 'time': time.time(),
                            'budget': str(before.budget), 'source': prepared.source_display,
                            'positions': {uid: str(value)
                                          for uid, value in before.quantities.items()},
                            'marks': {uid: str(value) for uid, value in before.marks.items()},
                            'cash': {currency: str(value)
                                     for currency, value in before.available_cash.items()}})
    prior_operations = {item.id for item in session.list_operations(account_id)}
    start = len(journal.events)
    _run_application(account_id, prepared, empty=empty)
    responses, sell_responses, buy_responses = _check_app_events(
        journal.events[start:], sells=sells, buys=buys, no_orders=no_orders, empty=empty)
    expected, operation_totals = _expected_app_positions(
        session, account_id, instruments, before.quantities, responses)
    after = session.wait_positions(account_id, expected)
    cash_delta = after.available_cash.get('rub', D(0)) - before.available_cash.get('rub', D(0))
    _wait_app_operations(session, account_id, prior_operations, operation_totals, cash_delta)
    check(not after.active_orders and after.limits_ready, 'app left unsettled orders or limits')
    check(after.available_cash.get('rub', D(0)) >= 0, 'negative own cash')
    lots = {item.uid: item.lot for item in instruments}
    for uid, quantity in after.quantities.items():
        check(quantity >= 0 and quantity % lots[uid] == 0, 'short or fractional physical lot')
        if uid in before.marks:
            check(abs(after.marks[uid] / before.marks[uid] - 1) <= D('.03'), 'live quote moved >3%')
    if buys:
        tolerance = max(D(2), before.budget * D('.001'))
        check(after.available_cash['rub'] + tolerance >= before.budget * reserve, 'cash floor lost')
    session.records.append({'event': 'app_pass', 'time': time.time(),
                            'before_budget': str(before.budget), 'after_budget': str(after.budget),
                            'before_positions': {key: str(value)
                                                 for key, value in before.quantities.items()},
                            'after_positions': {key: str(value)
                                                for key, value in after.quantities.items()},
                            'sell_orders': len(sell_responses), 'buy_orders': len(buy_responses)})
    return AppPass(responses, after)


@pytest.mark.parametrize('algorithm,source', [('INDEX', 'PAIR'), ('COMPOSITE', 'MIX'),
                                              ('ACCOUNT', 'SOURCE')])
def test_initial_monthly2000_and_noop(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        algorithm: str, source: str) -> None:
    """Initial buys and monthly funding preserve BUY-only rights and stable reruns."""
    session, _ = live
    instruments, account_dir = config_catalog(live, tmp_path, monkeypatch)
    dst = create_account(session, 'initial' + algorithm.lower(), 'dst')
    prepared = prepare_case(session, algorithm, source, instruments, account_dir,
                            'initial' + algorithm.lower())
    session.pay_in(dst, D(30000))
    _, state = app_pass(live, dst, prepared, instruments, buys=True)
    assert set(state.quantities) == {item.uid for item in instruments}
    values = [state.quantities[item.uid] * state.marks[item.uid] for item in instruments]
    assert abs(values[0] / sum(values) - D('.5')) < D('.03'), 'initial weights off target'
    app_pass(live, dst, prepared, instruments, no_orders=True)
    session.pay_in(dst, D(2000))
    app_pass(live, dst, prepared, instruments, buys=True)
    app_pass(live, dst, prepared, instruments, no_orders=True)


@pytest.mark.parametrize('algorithm,source,share,above', [
    ('INDEX', 'PAIR', '.55', False), ('INDEX', 'PAIR', '.80', True),
    ('COMPOSITE', 'MIX', '.55', False), ('COMPOSITE', 'MIX', '.80', True),
    ('ACCOUNT', 'SOURCE', '.55', False), ('ACCOUNT', 'SOURCE', '.80', True)])
def test_staged_holdings_drift(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        algorithm: str, source: str, share: str, above: bool) -> None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """A real holdings skew crosses the leaf or component limit at present quotes."""
    session, _ = live
    instruments, account_dir = config_catalog(live, tmp_path, monkeypatch)
    dst = create_account(session, 'drift' + algorithm.lower() + str(int(above)), 'dst')
    prepared = prepare_case(session, algorithm, source, instruments, account_dir,
                            'drift' + algorithm.lower() + str(int(above)))
    session.pay_in(dst, D(100000))
    for instrument, fraction in zip(instruments, (D(share), 1 - D(share))):
        quote = D(session.quotes[instrument.ticker].price)
        session.buy_setup(dst, instrument, int(D(85000) * fraction / (quote * instrument.lot)))
    state = session.state(dst)
    values = [state.quantities[item.uid] * state.marks[item.uid] for item in instruments]
    observed_share = values[0] / sum(values)
    metric = abs(observed_share - D('.5')) * (2 if algorithm == 'COMPOSITE' else 1)
    limit = D('.20') if algorithm == 'COMPOSITE' else D('.10')
    check(metric > limit + D('.05') if above else metric < limit - D('.02'),
          'setup holdings too close to drift threshold')
    responses, after = app_pass(live, dst, prepared, instruments,
                                sells=above, buys=True)
    if above:
        assert {item['uid'] for item in responses if item['side'] == 'SELL'} == {instruments[0].uid}
        after_values = [after.quantities[item.uid] * after.marks[item.uid] for item in instruments]
        assert abs(after_values[0] / sum(after_values) - D('.5')) < D('.03')
    session.pay_in(dst, D(2000))
    app_pass(live, dst, prepared, instruments, buys=True)


def test_child_own_drift_below_component_limit(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaf may sell its overweight while its sibling's selling remains forbidden."""
    session, _ = live
    instruments, _ = config_catalog(live, tmp_path, monkeypatch, ('SBER', 'OBLG', 'GOLD'))
    dst = create_account(session, 'leafdrift', 'dst')
    session.pay_in(dst, D(100000))
    for instrument, fraction in zip(instruments, (D('.4'), D('.1'), D('.5'))):
        quote = D(session.quotes[instrument.ticker].price)
        session.buy_setup(dst, instrument, int(D(90000) * fraction / (quote * instrument.lot)))
    state = session.state(dst)
    values = [state.quantities[item.uid] * state.marks[item.uid] for item in instruments]
    check(abs((values[0] + values[1]) / sum(values) - D('.5')) * 2 < D('.15'),
          'setup root component drift exceeded below-threshold margin')
    check(abs(values[0] / (values[0] + values[1]) - D('.5')) > D('.25'),
          'setup leaf own drift did not exceed above-threshold margin')
    responses, _ = app_pass(live, dst, prepare_strategy('COMPOSITE', 'INNER'), instruments,
                            sells=True)
    assert {item['uid'] for item in responses if item['side'] == 'SELL'} == {instruments[0].uid}


@pytest.mark.parametrize('algorithm,source', [('INDEX', 'SOLO0'), ('COMPOSITE', 'MIX'),
                                              ('ACCOUNT', 'SOURCE')])
def test_reserve_deficit_insufficient_cash_does_not_sell(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        algorithm: str, source: str) -> None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Cash below reserve cannot authorize protected sales or unfunded purchases."""
    session, _ = live
    instruments, account_dir = config_catalog(live, tmp_path, monkeypatch)
    dst = create_account(session, 'cash' + algorithm.lower(), 'dst')
    prepared = prepare_case(session, algorithm, source, instruments, account_dir,
                            'cash' + algorithm.lower())
    chosen = instruments[:1] if algorithm == 'INDEX' else instruments
    lots = [int(D(40000) / (D(session.quotes[item.ticker].price) * item.lot)) for item in chosen]
    cost = sum((count * D(session.quotes[item.ticker].price) * item.lot
                for item, count in zip(chosen, lots)), D(0))
    session.pay_in(dst, (cost * D('1.05')).quantize(D('.000000001')))
    for instrument, count in zip(chosen, lots):
        session.buy_setup(dst, instrument, count)
    before = session.state(dst)
    assert before.available_cash['rub'] < before.budget * D('.10')
    _, after = app_pass(live, dst, prepared, chosen, no_orders=True)
    assert after.quantities == before.quantities


@pytest.mark.parametrize('algorithm,source', [('INDEX', 'SOLO0'), ('COMPOSITE', 'MIX'),
                                              ('ACCOUNT', 'SOURCE')])
def test_small_budget_cannot_submit_orders(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        algorithm: str, source: str) -> None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """INDEX rejects empty main; ACCOUNT's positive fractional main cannot buy a lot."""
    session, _ = live
    instruments, account_dir = config_catalog(live, tmp_path, monkeypatch)
    dst = create_account(session, 'empty' + algorithm.lower(), 'dst')
    prepared = prepare_case(session, algorithm, source, instruments, account_dir,
                            'empty' + algorithm.lower())
    cost = D(session.quotes[instruments[0].ticker].price) * instruments[0].lot
    session.pay_in(dst, min(D(1), cost / 10).quantize(D('.000000001')))
    app_pass(live, dst, prepared, instruments, empty=algorithm != 'ACCOUNT', no_orders=True)


def test_full_balanced_imoex_oblg_gold(live: LiveCase, monkeypatch: pytest.MonkeyPatch) -> None:
    """The complete shipping BALANCED catalog trades real IMOEX shares, OBLG and GOLD."""
    session, _ = live
    for name in ('IMOEX_CONFIG_PATH', 'INDEX_CONFIG_DIR', 'COMPOSITE_CONFIG_DIR',
                 'ACCOUNT_CONFIG_DIR', 'ACCOUNT_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
    prepared = prepare_strategy('COMPOSITE', 'BALANCED')
    root = Path(__file__).resolve().parents[1] / 'autorepeater' / 'configs'
    session.configs = {name: json.loads((root / path).read_text(encoding='utf-8'))
                       for name, path in [('BALANCED', 'composite/balanced.json'),
                                          ('IMOEX', 'imoex.json'), ('OBLG', 'oblg.json'),
                                          ('GOLD', 'gold.json')]}
    # The strategy snapshot reads the actual full built-in composition, never a substitute.
    from autorepeater.strategies import create_strategy  # pylint: disable=import-outside-toplevel
    strategy = create_strategy(prepared)
    snapshot = strategy.load_snapshot(session.data)
    profile = strategy.allocation_profile(snapshot)
    instruments = [session.data.get_instrument(uid) for uid in profile.exposures]
    dst = create_account(session, 'balanced', 'dst')
    session.pay_in(dst, D(1000000))
    _, state = app_pass(live, dst, prepared, instruments, buys=True,
                        reserve=profile.reserve_fraction)
    gold = session.instrument('GOLD')
    oblg = session.instrument('OBLG')
    assert gold.uid in state.quantities and oblg.uid in state.quantities
    assert set(state.quantities) - {gold.uid, oblg.uid}, 'working IMOEX shares missing'
    app_pass(live, dst, prepared, instruments, no_orders=True,
             reserve=profile.reserve_fraction)
    session.pay_in(dst, D(2000))
    app_pass(live, dst, prepared, instruments, buys=True,
             reserve=profile.reserve_fraction)
