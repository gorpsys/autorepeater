"""RUB/CNY acceptance through the normal Runner on owned disposable sandbox accounts."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_FLOOR
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from t_tech.invest import InstrumentIdType, OrderDirection

from autorepeater import reporting
from autorepeater.cash_facts import MoneyAmount
from autorepeater.execution_market import ExecutionMarket
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.order_plan import OrderIntent, OrderPlan
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.purchase_plan import leaves
from autorepeater.strategies import prepare_strategy
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.strategy_data import InstrumentInfo, InstrumentType
from scripts.sandbox_lifecycle import SandboxFailure, safe_call
from scripts.sandbox_multicurrency import (
    NativeState, TradeFact, cash_value, fees_by_currency, native_midpoint,
    reconcile, require, required_fx_lots,
)
from scripts.sandbox_support import FILL, SandboxSession
from e2e.test_sandbox import LiveCase, _run_application, create_account


@dataclass(frozen=True)
class CurrencyCatalog:
    """Current checked identities, never hardcoded UID or stale diagnostic prices."""
    rub: InstrumentInfo
    bond: InstrumentInfo
    currency: InstrumentInfo
    pair: FxPair
    quote: FxQuote

    @property
    def securities(self) -> tuple[InstrumentInfo, ...]:
        """Currency units are deliberately outside the security universe."""
        return self.rub, self.bond


@dataclass(frozen=True)
class CurrencyPass:
    """The real planner's immutable plan beside separately checked broker effects."""
    plan: OrderPlan
    before: NativeState
    after: NativeState
    trades: list[TradeFact]
    intents: list[OrderIntent]


def _unique(session: SandboxSession, query: str, *, ticker: str | None = None) -> InstrumentInfo:
    matches = session.data.find_instruments(query)
    candidates = [session.data.get_instrument(item.uid) for item in matches
                  if ticker is None or item.ticker == ticker]
    candidates = [item for item in candidates if item.api_trade_available]
    require(len(candidates) == 1, 'multicurrency setup requires unique available instrument')
    return candidates[0]


def catalog(session: SandboxSession) -> CurrencyCatalog:
    """Complete metadata/books/permission preflight before setup sends any order."""
    sources = session.data.valuation_sources
    session.market = ExecutionMarket(sources.fx, sources.securities)
    rub = _unique(session, 'SBER', ticker='SBER')
    bond = _unique(session, 'RU000A105104')
    currency = _unique(session, 'CNYRUB_TOM', ticker='CNYRUB_TOM')
    full = safe_call(session.client.instruments.bond_by,
                     id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=bond.uid).instrument
    require(full.uid == bond.uid and full.isin == 'RU000A105104'
            and full.currency.lower() == 'cny', 'wrong RUSAL bond identity')
    pairs = [pair for pair in sources.fx.list_pairs() if pair.uid == currency.uid]
    require(len(pairs) == 1, 'missing CNYRUB_TOM full currency metadata')
    pair = pairs[0]
    require(pair.base_currency == 'cny' and pair.quote_currency == 'rub'
            and pair.api_trade_available and pair.buy_available and pair.sell_available
            and currency.lot == pair.lot_size, 'invalid direct CNY/RUB metadata')
    require(rub.currency.lower() == 'rub' and bond.currency.lower() == 'cny'
            and rub.instrument_type == InstrumentType.SHARE
            and bond.instrument_type == InstrumentType.BOND
            and currency.instrument_type == InstrumentType.CURRENCY,
            'invalid settlement currency or instrument category')
    for item in (rub, bond, currency):
        require(isinstance(item.lot, int) and not isinstance(item.lot, bool) and item.lot > 0,
                'invalid multicurrency setup lot')
        status = safe_call(session.client.market_data.get_trading_status, instrument_id=item.uid)
        require(status.instrument_uid == item.uid and status.api_trade_available_flag is True
                and status.bestprice_order_available_flag is True,
                'multicurrency trading unavailable before setup')
    quote = sources.fx.get_quote(pair)
    quote.validate_fresh(datetime.now(timezone.utc))
    for item in (rub, bond):
        security = sources.securities.get_security_quote(item.uid)
        require(security.currency == item.currency.lower(), 'setup quote currency mismatch')
        require(native_midpoint(security) == security.estimate_per_piece().amount,
                'native security midpoint dimension mismatch')
    session.records.append({'event': 'currency_preflight', 'pair': asdict(pair),
                            'rub_uid': rub.uid, 'bond_uid': bond.uid,
                            'market_time': quote.market_time.isoformat()})
    return CurrencyCatalog(rub, bond, currency, pair, quote)


def native_state(session: SandboxSession, account_id: str) -> NativeState:
    """Check actual native availability and blocks independently of portfolio valuation."""
    state = session.state(account_id)
    positions = safe_call(session.client.operations.get_positions, account_id=account_id)
    money, blocked = {}, {}
    for target, values in ((money, positions.money), (blocked, positions.blocked)):
        for amount in values:
            fact = MoneyAmount.from_units_nano(amount.currency, amount.units, amount.nano)
            require(fact.currency in ('rub', 'cny') and fact.currency not in target,
                    'invalid or duplicate sandbox money identity')
            target[fact.currency] = fact.amount
    require(state.limits_ready and not state.active_orders, 'multicurrency account not ready')
    require(all(value == 0 for value in blocked.values()), 'blocked sandbox native money')
    require(all(state.available_cash.get(code, D(0)) == value for code, value in money.items()),
            'native availability disagrees with positions')
    actual = {item.instrument_uid: D(item.balance) for item in positions.securities if item.balance}
    require(actual == state.quantities, 'physical securities disagree with portfolio pieces')
    result = NativeState(actual, money, blocked)
    session.records.append({'event': 'native_state', 'account_id': account_id, **asdict(result)})
    return result


def setup_buy(session: SandboxSession, dst: str, item: InstrumentInfo, lots: int, *,
              base_currency: str | None = None) -> None:
    """Full current own limits precede every explicit fixture mutation."""
    rules = session.execution.get_trade_rules(dst, [item.uid])[item.uid]
    require(rules.api_trade_available and rules.bestprice_order_available
            and rules.buy_max_lots >= lots and rules.lot == item.lot,
            'setup lacks own nonmargin trading limit')
    session.buy_setup(dst, item, lots, base_currency=base_currency)


def _trade_facts(session: SandboxSession, dst: str, instruments: CurrencyCatalog,
                 responses: list[dict[str, object]]) -> list[TradeFact]:
    items = {item.uid: item for item in (*instruments.securities, instruments.currency)}
    trades = []
    require(len({item['order_id'] for item in responses}) == len(responses)
            and len({item['request_id'] for item in responses}) == len(responses),
            'duplicate app order identity')
    for response in responses:
        require(response['uid'] in items, 'unexpected multicurrency app instrument')
        item = items[response['uid']]
        broker = safe_call(session.client.orders.get_order_state,
                           account_id=dst, order_id=response['order_id'])
        side = response['side']
        direction = (OrderDirection.ORDER_DIRECTION_BUY if side == 'BUY'
                     else OrderDirection.ORDER_DIRECTION_SELL)
        session.check_order(broker, item.uid, response['requested'],
                            direction, response['order_id'])
        require(broker.execution_report_status == FILL, 'multicurrency app order not FILL')
        total = MoneyAmount.from_units_nano(broker.total_order_amount.currency,
                                            broker.total_order_amount.units,
                                            broker.total_order_amount.nano)
        fee = MoneyAmount.from_units_nano(broker.executed_commission.currency,
                                          broker.executed_commission.units,
                                          broker.executed_commission.nano)
        require(total.currency == fee.currency == item.currency.lower(),
                'sandbox total/commission settlement units mismatch')
        trades.append(TradeFact(item.uid, side, broker.lots_executed, item.lot, total.currency,
                                total.amount, fee.amount,
                                'cny' if item.uid == instruments.currency.uid else None))
    return trades


def app_run(live: LiveCase, dst: str, prepared: PreparedStrategy,
            instruments: CurrencyCatalog) -> CurrencyPass:
    """Real Runner/SDK; observation wraps reporting and never alters its plan or executor."""
    session, journal = live
    before = native_state(session, dst)
    start = len(journal.events)
    plan, intents = run_observed(dst, prepared)
    events = journal.events[start:]
    responses = [item for item in events if item['event'] == 'response']
    require(len(responses) == sum(item['event'] == 'submit' for item in events)
            == sum(item['event'] == 'confirm' for item in events), 'unchecked app order')
    trades = _trade_facts(session, dst, instruments, responses)
    after = _settled_money(session, dst, before, trades)
    session.records.append({'event': 'currency_app_pass', 'trades': [asdict(t) for t in trades],
                            'fees': fees_by_currency(trades)})
    check_plan_money(plan)
    require(len(intents) == len(trades), 'missing executed ownership intent')
    return CurrencyPass(plan, before, after, trades, intents)


def run_observed(dst: str, prepared: PreparedStrategy) -> tuple[OrderPlan, list[OrderIntent]]:
    """Observe real execution ownership while calling the original SDK executor unchanged."""
    original = TInvestOrderExecutor.submit_order
    with patch('autorepeater.repeater.reporting.print_rebalance_plan',
               wraps=reporting.print_rebalance_plan) as report, patch.object(
                   TInvestOrderExecutor, 'submit_order', autospec=True,
                   side_effect=original) as submitted:
        _run_application(dst, prepared, empty=False)
    require(report.call_count == 1, 'expected one normal application calculation')
    return report.call_args.args[0], [call.args[2] for call in submitted.call_args_list]


def _settled_money(session: SandboxSession, dst: str, before: NativeState,
                   trades: list[TradeFact]) -> NativeState:
    expected = dict(before.quantities)
    for trade in trades:
        if trade.base_currency is None:
            expected[trade.uid] = expected.get(trade.uid, D(0)) + D(
                trade.lots * trade.lot_size) * (1 if trade.side == 'BUY' else -1)
    session.wait_positions(dst, {uid: value for uid, value in expected.items() if value})
    deadline = session.monotonic() + 30
    while True:
        after = native_state(session, dst)
        try:
            reconcile(before, after, trades)
            break
        except SandboxFailure:
            if session.monotonic() >= deadline:
                raise
            session.sleep(2)
    return after


def check_plan_money(plan: OrderPlan) -> None:
    """Check the same saved native/FX snapshot, leaf reserve and exact owner sums."""
    fx, funding = plan.snapshot.fx_snapshot, plan.funding
    for uid, price in funding.native_prices.items():
        require(plan.marks[uid] == price.amount * fx.rate(price.currency),
                'plan RUB midpoint inconsistent with native/FX snapshot')
    for path, node in leaves(plan.strategy).items():
        amounts = funding.reserve_shares[path]
        weights = {}
        for uid, quantity in node.target.quantities.items():
            code = plan.rules[uid].currency.lower()
            weights[code] = weights.get(code, D(0)) + quantity * plan.marks[uid]
        total = sum(weights.values(), D(0))
        for code, amount in amounts.items():
            expected = node.cash_floor * weights[code] / total
            require(abs(amount * fx.rate(code) - expected) <= D('.000000001'),
                    'leaf reserve allocated with wrong currency weights')
        require(cash_value(dict(amounts), {'rub': D(1), **{
            code: fx.rate(code) for code in fx.quotes}}) <= node.cash_floor,
                'leaf reserve duplicated or includes unallocated parent money')
    for exchange in funding.exchanges:
        require(sum(exchange.base_contributions.values(), D(0))
                == D(exchange.lots * exchange.lot_size), 'FX owner contributions mismatch')
        require(set(exchange.base_contributions) <= set(leaves(plan.strategy)),
                'FX uses nonleaf or protected owner')


def configure(live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
              instruments: CurrencyCatalog) -> Path:
    """Synthetic reference snapshot is explicit about native and capitalization currencies."""
    session, _ = live
    records = reference_records(session, instruments)
    directories = {name: tmp_path / name.lower() for name in ('INDEX', 'ACCOUNT', 'COMPOSITE')}
    for name, directory in directories.items():
        directory.mkdir()
        monkeypatch.setenv(name + '_CONFIG_DIR', str(directory))
    for name in ('IMOEX_CONFIG_PATH', 'ACCOUNT_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
    base = {'reserve': '0.15', 'max_lot_weight_error': '0.05', 'min_position_value': '0',
            'reference_capitalization_currency': 'rub',
            'allocation_drift_limits': [{'budget_from': '0', 'budget_to': None,
                                        'upper_inclusive': False, 'limit': '0.10'}]}
    for name, constituents in (('CROSS', records), ('RUB', records[:1]), ('CNY', records[1:])):
        config = dict(base, name=name, instruments=constituents)
        (directories['INDEX'] / (name + '.json')).write_text(
            json.dumps(config), encoding='utf-8')
        session.configs[name] = config
    for name, sources, weights in (
            ('MIX', ('RUB', 'CNY'), ('0.5', '0.5')),
            ('OWNERS', ('CNY', 'CNY'), ('0.4', '0.4')),
            ('ISOLATED', ('CROSS', 'CNY'), ('0.5', '0.5'))):
        config = {'name': name, 'component_drift_limit': '0.50', 'components': [
            {'algoritm': 'INDEX', 'src': source, 'weight': weight}
            for source, weight in zip(sources, weights)]}
        (directories['COMPOSITE'] / (name + '.json')).write_text(
            json.dumps(config), encoding='utf-8')
        session.configs[name] = config
    return directories['ACCOUNT']


def reference_records(session: SandboxSession, instruments: CurrencyCatalog) -> list[dict]:
    """Store explicit units of this synthetic acceptance snapshot."""
    records = []
    rate = instruments.quote.lot_quote.midpoint_per_unit
    for item in instruments.securities:
        price = native_midpoint(session.data.valuation_sources.securities.get_security_quote(
            item.uid))
        records.append({'ticker': item.ticker, 'reference_price': str(price),
                        'reference_price_currency': item.currency,
                        'reference_fx_rate': str(rate if item.currency == 'cny' else D(1)),
                        'effective_quantity': '1', 'free_float': '1', 'weight_limit': '1',
                        'reference_weight': '50', 'reference_index_capitalization': '1'})
    return records


def funded_account(session: SandboxSession, name: str) -> str:
    """All accounts belong to this test's lifecycle and finalizers."""
    dst = create_account(session, name, 'dst')
    session.pay_in(dst, D(1000000))
    return dst


def assert_targets(result: CurrencyPass) -> None:
    """Whole permitted target lots, without tolerating arbitrary missing purchases."""
    for uid, quantity in result.plan.strategy.target.quantities.items():
        lot = result.plan.rules[uid].lot
        target = (quantity / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
        require(result.after.quantities.get(uid, D(0)) == target,
                'permitted target lots not reached')


@pytest.mark.parametrize('algorithm,source', [
    ('INDEX', 'CROSS'), ('COMPOSITE', 'MIX'), ('ACCOUNT', 'MIXED')])
def test_rub_cny_normal_application(live: LiveCase, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch,
                                    algorithm: str, source: str) -> None:
    """Forward FX funds a mixed portfolio; ACCOUNT source remains strictly read-only."""
    session = live[0]
    instruments = catalog(session)
    account_dir = configure(live, tmp_path, monkeypatch, instruments)
    source_id, original = source_setup(session, algorithm, instruments, account_dir)
    dst = funded_account(session, 'mixed' + algorithm.lower())
    prepared = prepare_strategy(algorithm, source)
    result = app_run(live, dst, prepared, instruments)
    fx = [trade for trade in result.trades if trade.base_currency]
    require(len(fx) == 1 and fx[0].side == 'BUY', 'expected one forward exchange')
    require(set(result.after.quantities) == {item.uid for item in instruments.securities},
            'mixed target missing security or contains FX units')
    assert_targets(result)
    again = app_run(live, dst, prepared, instruments)
    require(not again.trades, 'stable mixed portfolio submits needless orders or reserve FX')
    if source_id is not None:
        require(native_state(session, source_id) == original,
                'ACCOUNT source money/positions changed')
        check_source_ratio(result, original, instruments)


def source_setup(session: SandboxSession, algorithm: str, instruments: CurrencyCatalog,
                 account_dir: Path) -> tuple[str | None, NativeState | None]:
    """Only ACCOUNT creates its own mixed source; all others retain the same catalog."""
    if algorithm != 'ACCOUNT':
        return None, None
    source_id = funded_account(session, 'mixedsource')
    fx_lots = int(D(400000) / instruments.quote.lot_quote.quote_amount('BUY', 1).amount)
    setup_buy(session, source_id, instruments.currency, fx_lots, base_currency='cny')
    for item in instruments.securities:
        price = native_midpoint(session.data.valuation_sources.securities.get_security_quote(
            item.uid))
        budget = D(300000) / (instruments.quote.lot_quote.midpoint_per_unit
                              if item.currency == 'cny' else D(1))
        setup_buy(session, source_id, item, int(budget / (price * item.lot)))
    config = {'name': 'MIXED', 'source_account_id': source_id, 'reserve': '0.15',
              'allocation_drift_limit': '0.10'}
    (account_dir / 'mixed.json').write_text(json.dumps(config), encoding='utf-8')
    session.configs['MIXED'] = config
    return source_id, native_state(session, source_id)


def check_source_ratio(result: CurrencyPass, original: NativeState,
                       instruments: CurrencyCatalog) -> None:
    """A shared RUB valuation preserves the source's physical mixed composition ratio."""
    ratios = [result.plan.strategy.target.quantities[item.uid]
              / original.quantities[item.uid] for item in instruments.securities]
    require(abs(ratios[0] - ratios[1]) <= D('.000000001'),
            'ACCOUNT mixed weights do not preserve source physical ratio')


def test_shared_cny_owners_and_unallocated_cash(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared deficit uses one FX with exact contributions; unallocated 20% is protected."""
    session, _ = live
    instruments = catalog(session)
    configure(live, tmp_path, monkeypatch, instruments)
    dst = funded_account(session, 'sharedcny')
    result = app_run(live, dst, prepare_strategy('COMPOSITE', 'OWNERS'), instruments)
    exchange, = result.plan.funding.exchanges
    require(set(exchange.base_contributions) == {(0,), (1,)}, 'shared FX lost one owner')
    require(result.plan.funding.cash.protected['rub'] == D(200000), 'unallocated cash lost')
    require(result.after.money['rub'] >= D(200000), 'unallocated cash funded child or commission')
    require(len([trade for trade in result.trades if trade.base_currency]) == 1,
            'same currency deficit bought more than once')
    assert_targets(result)
    require(not app_run(live, dst, prepare_strategy('COMPOSITE', 'OWNERS'), instruments).trades,
            'shared owners repeat settled FX or security')


@pytest.mark.parametrize('kind', ['sufficient', 'partial'])
def test_existing_cny_before_lot_rounding(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Previously bought CNY and FX lot surplus are counted before sizing a new exchange."""
    session, _ = live
    instruments = catalog(session)
    configure(live, tmp_path, monkeypatch, instruments)
    dst = funded_account(session, 'existing' + kind)
    lots = max(1, int(
        D(950000 if kind == 'sufficient' else 300000)
        / instruments.quote.lot_quote.quote_amount('BUY', 1).amount))
    setup_buy(session, dst, instruments.currency, lots, base_currency='cny')
    result = app_run(live, dst, prepare_strategy('INDEX', 'CNY'), instruments)
    fx = [trade for trade in result.trades if trade.base_currency]
    if kind == 'sufficient':
        require(not fx and not result.plan.funding.exchanges, 'sufficient CNY triggers FX')
    else:
        exchange, = result.plan.funding.exchanges
        purchase = result.plan.funding.security_debits.amount((), 'cny')
        reserve = result.plan.funding.reserve_shares[()]['cny']
        available = result.plan.funding.cash.amount((), 'cny')
        expected = required_fx_lots(purchase, reserve, available, instruments.pair.lot_size)
        require(len(fx) == 1 and fx[0].lots == exchange.lots == expected,
                'partial CNY deficit or rounding wrong')
        require(exchange.lots < required_fx_lots(
            purchase, reserve, D(0), instruments.pair.lot_size),
                'existing CNY ignored')
    assert_targets(result)
    require(not app_run(live, dst, prepare_strategy('INDEX', 'CNY'), instruments).trades,
            'commission or currency lot surplus causes needless reserve restore')


def test_reverse_exchange_only_for_rub_purchase(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A funded CNY portfolio is sold and exchanged only to reach the new RUB target."""
    session, _ = live
    instruments = catalog(session)
    configure(live, tmp_path, monkeypatch, instruments)
    dst = funded_account(session, 'reversecny')
    first = app_run(live, dst, prepare_strategy('INDEX', 'CNY'), instruments)
    assert_targets(first)
    result = app_run(live, dst, prepare_strategy('INDEX', 'RUB'), instruments)
    categories = [(trade.uid, trade.side) for trade in result.trades]
    require(categories == [
        (instruments.bond.uid, 'SELL'), (instruments.currency.uid, 'SELL'),
        (instruments.rub.uid, 'BUY')],
            'expected security SELL -> FX -> RUB BUY')
    assert_targets(result)
    require(result.after.money.get('cny', D(0)) > 0, 'reverse FX liquidated all residual currency')
    require(not app_run(live, dst, prepare_strategy('INDEX', 'RUB'), instruments).trades,
            'RUB no-op converts residual CNY to restore reserve')


def test_isolated_sale_does_not_fund_sibling(
        live: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Below root drift limit the mixed leaf's SELL cash stays outside its CNY sibling."""
    session, _ = live
    instruments = catalog(session)
    configure(live, tmp_path, monkeypatch, instruments)
    dst = funded_account(session, 'isolatedcny')
    # Both leaves already have capital, but only the mixed leaf has internal RUB overweight.
    fx_cost = instruments.quote.lot_quote.quote_amount('BUY', 1).amount
    setup_buy(session, dst, instruments.currency, int(D(600000) / fx_cost), base_currency='cny')
    price = native_midpoint(session.data.valuation_sources.securities.get_security_quote(
        instruments.bond.uid))
    setup_buy(session, dst, instruments.bond, int(D(400000) / (
        instruments.quote.lot_quote.midpoint_per_unit * price * instruments.bond.lot)))
    rub_price = native_midpoint(session.data.valuation_sources.securities.get_security_quote(
        instruments.rub.uid))
    setup_buy(session, dst, instruments.rub, int(D(350000) / (rub_price * instruments.rub.lot)))
    result = app_run(live, dst, prepare_strategy('COMPOSITE', 'ISOLATED'), instruments)
    require(not result.plan.strategy.decision.redistribution_allowed,
            'isolated setup accidentally grants root redistribution')
    require(result.plan.sells and all(set(sale.pieces) == {(0,)} for sale in result.plan.sells),
            'isolated setup did not create internal sale')
    require(any(t.uid == instruments.rub.uid and t.side == 'SELL' for t in result.trades),
            'isolated internal sale missing')
    # Initial native cash is the only known money; unknown SDK SELL proceeds add no permission.
    require(not any(t.uid == instruments.currency.uid and t.side == 'SELL' for t in result.trades),
            'isolated sale created an unauthorized reverse FX')
    for purchase in result.plan.funding.purchases:
        for path, pieces in purchase.intent.pieces.items():
            price = result.plan.funding.native_prices[purchase.intent.uid]
            require(pieces * price.amount <= result.plan.funding.cash.amount(path, price.currency)
                    or bool(purchase.fx_dependencies), 'sibling uses unknown isolated proceeds')
    check_isolated_execution_money(result)


def check_isolated_execution_money(result: CurrencyPass) -> None:
    """Actual execution ownership cannot spend a sibling's unknown sale credits."""
    spent = {}
    for intent, trade in zip(result.intents, result.trades):
        if intent.side != 'BUY':
            continue
        pieces = sum(intent.pieces.values(), D(0))
        for path, owned in intent.pieces.items():
            key = path, trade.currency
            spent[key] = spent.get(key, D(0)) + (trade.total + trade.commission) * owned / pieces
    for (path, code), debit in spent.items():
        initial = result.plan.funding.cash.amount(path, code)
        for intent, trade in zip(result.intents, result.trades):
            if trade.base_currency == code and trade.side == 'BUY':
                initial += intent.pieces.get(path, D(0))
        require(debit <= initial, 'actual BUY consumed unproven isolated SELL proceeds')
