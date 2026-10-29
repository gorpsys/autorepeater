"""RUB/CNY acceptance through the normal Runner on owned disposable sandbox accounts."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_FLOOR
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from t_tech.invest import (
    InstrumentIdType, OperationState, OperationType, OrderDirection,
)

from autorepeater import execution_market, reporting
from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.execution_market import ExecutionMarket
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.fx_diagnostics import amount_fact, requested_pair, scalar_fact, sdk_metadata_facts
from autorepeater.market_diagnostics import (
    emit_market_fact, instrument_facts, measure_stage, safe_symbol,
)
from autorepeater.instrument_selection import prefer_instrument_currency
from autorepeater.order_plan import OrderIntent, OrderPlan
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.purchase_plan import leaves
from autorepeater.runner import RunnerParams
from autorepeater.strategies import prepare_strategy
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.strategy_data import InstrumentInfo, InstrumentType
from autorepeater.strategy_plan import TradeMode
from scripts.sandbox_lifecycle import SandboxFailure, safe_call
from scripts.sandbox_commission_probe import (
    CommissionObservation, CommissionSample, compare_commissions)
from scripts.sandbox_multicurrency import (
    NativeState, TradeFact, cash_value, fees_by_currency, money_components, money_deltas,
    native_midpoint,
    reconcile, require, required_fx_lots,
)
from scripts.sandbox_support import FILL, SandboxSession
from e2e.test_sandbox import LiveCase, _run_application, create_account


PREFERRED_CURRENCY = 'CNY'


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
    buy_caps: dict[str, int]


def _selection_failure(session, request, matches, facts, outcome):
    """Only loaded whitelist facts; no diagnostic RPC or new evidence root fields."""
    print('Sandbox diagnostic: ' + json.dumps({
        'stage': 'catalog', 'scenario': safe_symbol(session.scenario, limit=240),
        'phase': 'instrument_selection', **request,
        'raw_count': len(matches), 'eligible_count': outcome[1], 'reason': outcome[0],
        'candidates': facts, 'omitted_count': max(0, len(matches) - len(facts)),
    }), flush=True)


def _preferred_candidates(session, request, matches, facts, candidates):
    """Explain preference using the bounded facts and the full metadata already read."""
    try:
        candidates = prefer_instrument_currency(candidates, request['preferred_currency'])
    except ValueError:
        _selection_failure(session, request, matches, facts, ('invalid_currency', 'incomplete'))
        raise
    eligible_uids = {item.uid for item in candidates}
    for fact in facts:
        if fact['reason'] == 'eligible' and fact['uid'] not in eligible_uids:
            fact['reason'] = 'nonpreferred_currency'
    return candidates


@measure_stage('catalog.instrument_selection')
def _unique(session: SandboxSession, query: str, *, ticker: str | None = None,
            expected: tuple[InstrumentType, str] | None = None,
            preferred_currency: str = 'rub') -> InstrumentInfo:
    request = {'query': safe_symbol(query),
               'requested_ticker': ('unspecified' if ticker is None
                                    else safe_symbol(ticker, limit=32)),
               'requested_kind': 'unspecified' if expected is None else safe_symbol(expected[0]),
               'requested_currency': ('unspecified' if expected is None
                                      else safe_symbol(expected[1])),
               'requested_class': 'unspecified',
               'preferred_currency': currency_code(preferred_currency)}
    matches = session.data.find_instruments(query)
    candidates, facts = [], []
    for match in matches:
        fact = instrument_facts(match, loaded=False)
        if len(facts) < 20:
            facts.append(fact)
        if ticker is not None and match.ticker != ticker:
            fact['reason'] = 'ticker_mismatch'
            continue
        try:
            item = session.data.get_instrument(match.uid)
        except Exception:
            fact['reason'] = 'metadata_read_failed'
            _selection_failure(session, request, matches, facts,
                               ('metadata_read_failed', 'incomplete'))
            raise
        fact.update(instrument_facts(item))
        flag = fact['api_available']
        fact['reason'] = ('eligible' if flag is True else 'api_unavailable' if flag is False
                          else 'api_availability_' + flag)
        if expected is not None:
            fact['metadata_issues'] = [name + '_mismatch' for name, actual, wanted in (
                ('kind', fact['kind'], expected[0]), ('currency', fact['currency'], expected[1]))
                if actual != wanted]
        candidates.append(item)
    candidates = [item for item in candidates if item.api_trade_available is True]
    candidates = _preferred_candidates(session, request, matches, facts, candidates)
    if len(candidates) != 1:
        reason = ('no_candidates' if not matches else 'no_eligible_candidates'
                  if not candidates else 'ambiguous')
        _selection_failure(session, request, matches, facts, (reason, len(candidates)))
    elif expected is not None and (
            candidates[0].instrument_type, candidates[0].currency.lower()) != expected:
        _selection_failure(session, request, matches, facts,
                           ('metadata_expectation_mismatch', len(candidates)))
    require(len(candidates) == 1, 'multicurrency setup requires unique available instrument')
    return candidates[0]


@measure_stage('catalog.preflight')
def catalog(session: SandboxSession) -> CurrencyCatalog:
    """Complete metadata/books/permission preflight before setup sends any order."""
    sources = session.data.valuation_sources
    session.market = ExecutionMarket(sources.fx, sources.securities)
    rub = _unique(session, 'SBER', ticker='SBER', expected=(InstrumentType.SHARE, 'rub'),
                  preferred_currency=PREFERRED_CURRENCY)
    bond = _unique(session, 'RU000A10CP52', expected=(InstrumentType.BOND, 'cny'),
                   preferred_currency=PREFERRED_CURRENCY)
    currency = _unique(session, 'CNYRUB_TOM', ticker='CNYRUB_TOM',
                       expected=(InstrumentType.CURRENCY, 'rub'))
    with measure_stage('catalog.bond_metadata.request'):
        full = safe_call(session.client.instruments.bond_by,
                         id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=bond.uid).instrument
    emit_market_fact({'event': 'currency_setup_metadata',
                      **{role: {**instrument_facts(item), 'lot': scalar_fact(item.lot)}
                         for role, item in (('rub', rub), ('bond', bond), ('currency', currency))},
                      'bond_full': sdk_metadata_facts(full)}, level=logging.INFO)
    require(full.uid == bond.uid and full.isin == 'RU000A10CP52'
            and full.currency.lower() == 'cny', 'wrong CNY bond identity')
    with requested_pair(currency.uid):
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
        with measure_stage('catalog.status.request'):
            status = safe_call(session.client.market_data.get_trading_status,
                               instrument_id=item.uid)
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
                                'cny' if item.uid == instruments.currency.uid else None,
                                cny_bond=(item.instrument_type == InstrumentType.BOND
                                          and item.currency.lower() == 'cny')))
        session.records.append({
            'event': 'currency_order_money', 'account_id': dst,
            'request_id': response['request_id'], 'order_id': broker.order_id,
            'status': broker.execution_report_status.name,
            'requested': broker.lots_requested, 'executed': broker.lots_executed,
            'trade': asdict(trades[-1]),
            'pieces': broker.lots_executed * item.lot,
            'total_order_amount': amount_fact(broker.total_order_amount),
            'executed_commission': amount_fact(broker.executed_commission),
            'executed_order_price': amount_fact(broker.executed_order_price),
        })
    return trades


def app_run(live: LiveCase, dst: str, prepared: PreparedStrategy,
            instruments: CurrencyCatalog) -> CurrencyPass:
    """Real Runner/SDK; observation wraps reporting and never alters its plan or executor."""
    session, journal = live
    before = native_state(session, dst)
    start = len(journal.events)
    session.records.append({'event': 'currency_app_start'})
    plan, intents = run_observed(dst, prepared, records=session.records)
    events = journal.events[start:]
    responses = [item for item in events if item['event'] == 'response']
    require(len(responses) == sum(item['event'] == 'submit' for item in events)
            == sum(item['event'] == 'confirm' for item in events), 'unchecked app order')
    trades = _trade_facts(session, dst, instruments, responses)
    after = _settled_money(session, dst, before, trades)
    caps = _fresh_buy_caps(session.records)
    session.records.append({'event': 'currency_app_pass', 'trades': [asdict(t) for t in trades],
                            'fees': fees_by_currency(trades), 'fresh_buy_caps': caps})
    _record_plan_targets(session, plan, after)
    check_plan_money(plan)
    require(len(intents) == len(trades), 'missing executed ownership intent')
    return CurrencyPass(plan, before, after, trades, intents, caps)


def _fresh_buy_caps(records: list) -> dict[str, int]:
    """Read own limits at funded BUY stages, not the selected purchase quantities."""
    caps = {}
    for fact in records:
        if fact.get('event') == 'currency_app_start':
            caps.clear()
        if (fact.get('event') == 'currency_execution_funding' and not fact['exchanges']
                and fact['purchases']):
            uid = fact['purchases'][0]['uid']
            caps[uid] = next(rule['buy_max_lots'] for rule in fact['rules'] if rule['uid'] == uid)
    return caps


def _record_plan_targets(session: SandboxSession, plan: OrderPlan, after: NativeState) -> None:
    """Preserve calculated limits beside actual pieces before any target assertion."""
    funding = plan.funding
    session.records.append({
        'event': 'currency_plan_targets',
        'budget': str(plan.strategy.budget), 'cash_floor': str(plan.strategy.cash_floor),
        'initial_cash': {code: str(amount)
                         for code, amount in plan.snapshot.available_cash.items()},
        'actual_cash': {code: str(amount) for code, amount in after.money.items()},
        'protected_cash': {code: str(amount) for code, amount in funding.cash.protected.items()},
        'securities': [{
            'uid': uid, 'lot': plan.rules[uid].lot,
            'currency': plan.rules[uid].currency,
            'target_quantity': str(quantity),
            'permitted_quantity': str((quantity / plan.rules[uid].lot).to_integral_value(
                rounding=ROUND_FLOOR) * plan.rules[uid].lot),
            'actual_quantity': str(after.quantities.get(uid, D(0))),
            'mark_rub': str(plan.marks[uid]),
            'native_price': str(funding.native_prices[uid].amount),
            'initial_buy_lots': plan.rules[uid].buy_max_lots,
            'initial_buy_money': str(plan.rules[uid].buy_money_amount),
        } for uid, quantity in plan.strategy.target.quantities.items()],
        'owners': [{
            'path': list(path), 'budget': str(node.budget), 'cash_floor': str(node.cash_floor),
            'targets': {uid: str(quantity) for uid, quantity in node.target.quantities.items()},
            'held': {uid: str(quantity) for uid, quantity in plan.ownership[path].items()},
            'cash': {code: str(amount) for code, amount in funding.cash.balances[path].items()},
            'reserve': {code: str(amount)
                        for code, amount in funding.reserve_shares[path].items()},
        } for path, node in leaves(plan.strategy).items()],
    })


def _funding_fact(execution, funding) -> dict:
    """Loaded funding and ledger scalars only; never transport or SDK objects."""
    def pools(values):
        return [{'path': list(path), 'money': {code: str(amount) for code, amount in money.items()}}
                for path, money in values.items()]

    return {
        'event': 'currency_execution_funding',
        'ledger_cash': pools(execution.ledger.cash.balances),
        'security_limits_rub': [{'path': list(path), 'amount': str(amount)}
                                for path, amount in execution.ledger.limits.items()],
        'rules': [{'uid': uid, 'currency': rule.currency, 'buy_max_lots': rule.buy_max_lots,
                   'buy_money_amount': str(rule.buy_money_amount)}
                  for uid, rule in execution.own.items()],
        'native_prices': {uid: str(price.amount) for uid, price in funding.native_prices.items()},
        'purchases': [{'uid': purchase.intent.uid, 'lots': purchase.intent.lots,
                       'lot': purchase.intent.lot_size, 'native_amount': str(
                           purchase.native_amount.amount)} for purchase in funding.purchases],
        'exchanges': [
            {'uid': intent.uid, 'side': intent.side, 'lots': intent.lots,
             'base_contributions': [{'path': list(path), 'amount': str(amount)}
                                    for path, amount in intent.base_contributions.items()],
             'debit_limits': pools(intent.debit_limits),
             'estimated_debits': pools(intent.debits)} for intent in funding.exchanges],
    }


def run_observed(dst: str, prepared: PreparedStrategy, *, records: list | None = None
                 ) -> tuple[OrderPlan, list[OrderIntent]]:
    """Observe real execution ownership while calling the original SDK executor unchanged."""
    original = TInvestOrderExecutor.submit_order
    original_funding = execution_market._ExecutionPass._funding  # pylint: disable=protected-access
    original_exchange = execution_market._exchange_cash  # pylint: disable=protected-access

    def observe_funding(execution):
        funding = original_funding(execution)
        if records is not None:
            records.append(_funding_fact(execution, funding))
        return funding

    def observe_exchange(ledger, intent, receipt, fx):
        outcome = original_exchange(ledger, intent, receipt, fx)
        if records is not None:
            amounts = receipt.amounts
            total = None if amounts is None or amounts.total is None else str(amounts.total.amount)
            fee = (None if amounts is None or amounts.executed_commission is None
                   else str(amounts.executed_commission.amount))
            records.append({'event': 'currency_exchange_attribution', 'accepted': outcome,
                            'uid': intent.uid, 'receipt_total': total, 'receipt_commission': fee})
        return outcome

    with patch('autorepeater.repeater.reporting.print_rebalance_plan',
               wraps=reporting.print_rebalance_plan) as report, patch.object(
                   TInvestOrderExecutor, 'submit_order', autospec=True,
                   side_effect=original) as submitted, patch.object(
                       execution_market._ExecutionPass, '_funding',  # pylint: disable=protected-access
                       autospec=True, side_effect=observe_funding), patch(
                           'autorepeater.execution_market._exchange_cash',
                           side_effect=observe_exchange):
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
    started = session.monotonic()
    deadline = started + 30
    poll = 0
    while True:
        after = native_state(session, dst)
        poll += 1
        session.records.append({
            'event': 'currency_settlement_poll', 'account_id': dst, 'poll': poll,
            'observed_utc': datetime.now(timezone.utc).isoformat(),
            'elapsed_seconds': session.monotonic() - started,
            'components': money_components(before, trades),
            'money': money_deltas(before, after, trades), 'blocked': dict(after.blocked),
        })
        try:
            reconcile(before, after, trades)
            break
        except SandboxFailure:
            if session.monotonic() >= deadline:
                raise
            session.sleep(2)
    return after


def _expected_reserve(node, plan: OrderPlan) -> dict[str, D]:
    """Independent reserve oracle: target weights, then halve foreign shares."""
    weights = {}
    for uid, quantity in node.target.quantities.items():
        code = plan.rules[uid].currency.lower()
        weights[code] = weights.get(code, D(0)) + quantity * plan.marks[uid]
    total = sum(weights.values(), D(0))
    expected = {code: node.cash_floor * weight / total
                for code, weight in weights.items()}
    for code in list(expected):
        if code != 'rub':
            expected[code] /= 2
            expected['rub'] = expected.get('rub', D(0)) + expected[code]
    return expected


def check_plan_money(plan: OrderPlan) -> None:
    """Check the same saved native/FX snapshot, leaf reserve and exact owner sums."""
    fx, funding = plan.snapshot.fx_snapshot, plan.funding
    for uid, price in funding.native_prices.items():
        require(plan.marks[uid] == price.amount * fx.rate(price.currency),
                'plan RUB midpoint inconsistent with native/FX snapshot')
    for path, node in leaves(plan.strategy).items():
        amounts = funding.reserve_shares[path]
        expected = _expected_reserve(node, plan)
        for code, amount in amounts.items():
            require(abs(amount * fx.rate(code) - expected[code]) <= D('.000000001'),
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
    monkeypatch.setenv('FX_PAIR_UIDS', json.dumps(
        {instruments.pair.base_currency: instruments.pair.uid}))
    sources = session.data.valuation_sources
    session.market = ExecutionMarket(sources.fx, sources.securities,
                                     fx_pair_uids=RunnerParams.from_environment().fx_pair_uids)
    records = reference_records(session, instruments)
    directories = {name: tmp_path / name.lower() for name in ('INDEX', 'ACCOUNT', 'COMPOSITE')}
    for name, directory in directories.items():
        directory.mkdir()
        monkeypatch.setenv(name + '_CONFIG_DIR', str(directory))
    for name in ('IMOEX_CONFIG_PATH', 'ACCOUNT_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
    base = {'reserve': '0.15', 'max_lot_weight_error': '0.05', 'min_position_value': '0',
            'preferred_currency': PREFERRED_CURRENCY,
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
        if uid in result.buy_caps:
            target = min(target, result.before.quantities.get(uid, D(0))
                         + D(result.buy_caps[uid] * lot))
        if result.plan.strategy.decision.mode == TradeMode.BUY_ONLY:
            target = max(target, result.before.quantities.get(uid, D(0)))
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
    repeated = app_run(live, dst, prepare_strategy('INDEX', 'RUB'), instruments)
    require(all(trade.uid == instruments.rub.uid and trade.base_currency is None
                for trade in repeated.trades),
            'RUB repeat converts residual CNY to restore reserve')
    assert_targets(repeated)


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


def _probe_setup_operation_ids(operations, setup_uid):
    """Use the bounded full account history to link even late UID-less FX fees."""
    if setup_uid is None:
        return set()
    buys = {item.id for item in operations if item.instrument_uid == setup_uid
            and item.operation_type == OperationType.OPERATION_TYPE_BUY}
    return buys | {item.id for item in operations
                   if item.operation_type == OperationType.OPERATION_TYPE_BROKER_FEE
                   and (item.instrument_uid == setup_uid
                        or (not item.instrument_uid and item.parent_operation_id in buys))}


def _probe_history(session, dst, seen, bond, pieces, *, setup_uid=None):
    """Each executed BUY/payment and fee ID is counted once on this disposable account."""
    # pylint: disable=too-many-arguments,too-many-locals
    response = safe_call(session.client.operations.get_operations, account_id=dst)
    require(len(response.operations) <= 100, 'unexpected commission probe history size')
    require(len({operation.id for operation in response.operations}) == len(response.operations),
            'invalid or duplicate probe operation ID')
    require(setup_uid != bond.uid, 'probe setup and bond identities overlap')
    setup_ids = _probe_setup_operation_ids(response.operations, setup_uid)
    consumed, pending, facts, purchases, fees = set(), [], [], [], []
    quantity = 0
    for operation in response.operations:
        require(isinstance(operation.id, str) and bool(operation.id),
                'invalid or duplicate probe operation ID')
        require(isinstance(operation.state, OperationState)
                and isinstance(operation.operation_type, OperationType),
                'invalid probe history enum')
        if (operation.id in seen or operation.id in setup_ids or operation.operation_type not in (
                    OperationType.OPERATION_TYPE_BUY, OperationType.OPERATION_TYPE_BROKER_FEE)):
            continue
        facts.append({'id': safe_symbol(operation.id, limit=128),
                      'uid': safe_symbol(operation.instrument_uid),
                      'type': operation.operation_type.name, 'state': operation.state.name,
                      'quantity': operation.quantity, 'payment': amount_fact(operation.payment)})
        if operation.state != OperationState.OPERATION_STATE_EXECUTED:
            pending.append(safe_symbol(operation.id, limit=128))
            continue
        consumed.add(operation.id)
        require(all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    for value in (operation.quantity, operation.quantity_rest)),
                'invalid commission probe history quantities')
        payment = MoneyAmount.from_units_nano(
            operation.payment.currency, operation.payment.units, operation.payment.nano)
        require(payment.currency == 'cny' and payment.amount <= 0,
                'invalid commission probe operation payment')
        if operation.operation_type == OperationType.OPERATION_TYPE_BUY:
            require(operation.instrument_uid == bond.uid
                    and isinstance(operation.quantity, int)
                    and not isinstance(operation.quantity, bool)
                    and operation.quantity > 0 and operation.quantity_rest == 0,
                    'invalid commission probe history purchase')
            quantity += operation.quantity
            purchases.append(-payment.amount)
        else:
            require(operation.instrument_uid in ('', bond.uid),
                    'invalid commission probe history fee identity')
            fees.append(-payment.amount)
    complete = quantity == pieces and bool(purchases) and bool(fees) and not pending
    session.records.append({'event': 'cny_probe_history', 'account_id': dst,
                            'observed_utc': datetime.now(timezone.utc).isoformat(),
                            'complete': complete, 'operations': facts, 'pending_ids': pending,
                            'excluded_setup_ids': sorted(safe_symbol(item, limit=128)
                                                         for item in setup_ids),
                            'observed_principal': sum(purchases, D(0)),
                            'observed_fee': sum(fees, D(0))})
    return consumed, sum(purchases, D(0)) if quantity == pieces and purchases else None, (
        sum(fees, D(0)) if complete else None)


def _probe_fill_prices(session, dst, response, bond, lots):
    """Missing native per-fill prices limit inference, without inventing trade prices."""
    state = safe_call(session.client.orders.get_order_state,
                      account_id=dst, order_id=response['order_id'])
    session.check_order(state, bond.uid, lots, OrderDirection.ORDER_DIRECTION_BUY,
                        response['order_id'])
    require(state.execution_report_status == FILL, 'commission probe order not FILL')
    facts, prices, quantity = [], [], 0
    for stage in state.stages:
        require(isinstance(stage.quantity, int) and not isinstance(stage.quantity, bool)
                and stage.quantity > 0, 'invalid commission probe fill quantity')
        price = MoneyAmount.from_units_nano(
            stage.price.currency, stage.price.units, stage.price.nano)
        require(price.currency == 'cny' and price.amount > 0, 'invalid probe native fill price')
        quantity += stage.quantity
        prices.append(price.amount)
        facts.append({'quantity': stage.quantity, 'price': amount_fact(stage.price)})
    complete = quantity == lots * bond.lot
    session.records.append({'event': 'cny_probe_fills', 'account_id': dst,
                            'order_id': response['order_id'], 'complete': complete, 'fills': facts})
    return tuple(prices) if complete else ()


def _probe_step(session, dst, instruments, lots, seen):
    """A single checked BESTPRICE BUY; observe raw state, history and native cash separately."""
    before_time = datetime.now(timezone.utc).isoformat()
    before = native_state(session, dst)
    rule = session.execution.get_trade_rules(dst, [instruments.bond.uid])[instruments.bond.uid]
    require(rule.currency.lower() == 'cny' and rule.lot == instruments.bond.lot
            and rule.api_trade_available and rule.bestprice_order_available
            and rule.buy_max_lots >= lots, 'probe lacks own nonmargin trading permission')
    start = len(session.records)
    session.buy_setup(dst, instruments.bond, lots)
    response = [item for item in session.records[start:] if item['event'] == 'setup_response'][-1]
    trade = _trade_facts(session, dst, instruments, [{
        'request_id': response['request_id'], 'order_id': response['order_id'],
        'uid': instruments.bond.uid,
        'side': 'BUY', 'requested': lots}])[0]
    require(trade.total > 0 and trade.commission >= 0
            and trade.total + trade.commission <= min(before.money['cny'], rule.buy_money_amount),
            'probe receipt exceeds owned native budget or money cap')
    fill_prices = _probe_fill_prices(session, dst, response, instruments.bond, lots)
    deadline = session.monotonic() + 30
    while True:
        history = _probe_history(session, dst, seen, instruments.bond,
                                 D(lots * instruments.bond.lot), setup_uid=instruments.currency.uid)
        after = native_state(session, dst)
        if history[2] is not None or session.monotonic() >= deadline:
            break
        session.sleep(2)
    require(after.money.get('rub', D(0)) == before.money.get('rub', D(0))
            and D(0) <= after.money['cny'] < before.money['cny'],
            'probe cash changed outside owned CNY budget')
    session.records.append({
        'event': 'cny_probe_step', 'account_id': dst, 'order_id': response['order_id'],
        'request_id': response['request_id'], 'uid': instruments.bond.uid, 'lots': lots,
        'before_utc': before_time, 'after_utc': datetime.now(timezone.utc).isoformat(),
        'before': asdict(before), 'after': asdict(after), 'own_money': rule.buy_money_amount,
        'principal': trade.total, 'reported_fee': trade.commission,
        'history_principal': history[1], 'history_fee': history[2]})
    return CommissionSample(lots, trade.total, trade.commission,
                            before.money['cny'] - after.money['cny'],
                            history[1], history[2], CommissionObservation(
                                before.money['cny'], after.money['cny'], fill_prices)), history[0]


def _fund_cny_probe(session, scenario, instruments, fx_lots):
    """RUB-only funding and checked FX settlement precede all probe measurements."""
    dst = create_account(session, scenario, 'dst')
    empty = native_state(session, dst)
    require(not empty.quantities and all(value == 0 for value in empty.money.values()),
            'commission probe account is not empty')
    session.pay_in(dst, D(1000000))
    session.wait_positions(dst, {}, minimum_cash={'rub': D(1000000)})
    before = native_state(session, dst)
    require(before.money.get('rub') == D(1000000) and before.money.get('cny', D(0)) == 0
            and not before.quantities, 'commission probe RUB funding mismatch')
    quote = session.data.valuation_sources.fx.get_quote(instruments.pair)
    quote.validate_fresh(datetime.now(timezone.utc))
    # This fixture buffer is a budget guard, not a broker commission/price contract.
    reserved_cost = quote.lot_quote.quote_amount('BUY', fx_lots).amount * D('1.02')
    rule = session.execution.get_trade_rules(
        dst, [instruments.currency.uid])[instruments.currency.uid]
    require(rule.currency.lower() == 'rub' and rule.lot == instruments.pair.lot_size
            and rule.api_trade_available and rule.bestprice_order_available
            and rule.buy_max_lots >= fx_lots, 'probe FX lacks own nonmargin trading permission')
    require(reserved_cost <= min(before.money['rub'], rule.buy_money_amount),
            'probe FX exceeds owned RUB budget with commission reserve')
    start = len(session.records)
    session.buy_setup(dst, instruments.currency, fx_lots, base_currency='cny')
    response = [item for item in session.records[start:] if item['event'] == 'setup_response'][-1]
    trade = _trade_facts(session, dst, instruments, [{
        'request_id': response['request_id'], 'order_id': response['order_id'],
        'uid': instruments.currency.uid, 'side': 'BUY', 'requested': fx_lots}])[0]
    require(trade.total > 0 and trade.commission >= 0
            and trade.total + trade.commission <= min(before.money['rub'], rule.buy_money_amount),
            'probe FX receipt exceeds owned RUB budget or money cap')
    funded = _settled_money(session, dst, before, [trade])
    require(funded.money.get('cny') == D(fx_lots * instruments.pair.lot_size)
            and not funded.quantities, 'commission probe FX funding mismatch')
    session.records.append({'event': 'cny_probe_funding', 'account_id': dst,
                            'fx_lots': fx_lots, 'trade': asdict(trade), 'state': asdict(funded)})
    return dst


def _verify_probe_sequence(session, accounts, baselines):
    """A retry, reused request or wrong account cannot masquerade as split execution."""
    steps = [item for item in session.records if item['event'] == 'cny_probe_step']
    require([item['lots'] for item in steps] == [2, 1, 1]
            and [item['account_id'] for item in steps] == [accounts[0], accounts[1], accounts[1]]
            and len({item['request_id'] for item in steps}) == 3
            and len({(item['account_id'], item['order_id']) for item in steps}) == 3,
            'commission probe order sequence or identity mismatch')
    require(all(step[phase]['money'].get('rub', D(0))
                == baselines[step['account_id']].money.get('rub', D(0))
                for step in steps for phase in ('before', 'after'))
            and all(step['before']['money']['cny'] == baselines[step['account_id']].money['cny']
                    for step in steps[:2]), 'commission probe baseline changed')


def _probe_baselines(session, accounts, funding):
    """Both preparations must finish before either account enters the comparison."""
    baselines = {dst: native_state(session, dst) for dst in accounts}
    require(all(state.money.get('cny') == funding and not state.quantities
                for state in baselines.values()), 'commission probe unequal initial CNY balances')
    for dst, state in baselines.items():
        session.records.append({'event': 'cny_probe_baseline', 'account_id': dst,
                                'observed_utc': datetime.now(timezone.utc).isoformat(),
                                'state': asdict(state)})
    return baselines


def test_cny_commission_one_order_vs_split(live: LiveCase) -> None:
    """Controlled bond diagnostic after equal CNY acquisition through settled setup FX."""
    session, _ = live
    instruments = catalog(session)
    sources = session.data.valuation_sources
    session.market = ExecutionMarket(sources.fx, sources.securities,
                                     fx_pair_uids={'cny': instruments.pair.uid})
    fx_lots = required_fx_lots(
        native_midpoint(sources.securities.get_security_quote(instruments.bond.uid))
        * D(instruments.bond.lot * 4), D(0), D(0), instruments.pair.lot_size)
    accounts = []
    try:
        for scenario in ('cnycombined', 'cnysplit'):
            accounts.append(_fund_cny_probe(session, scenario, instruments, fx_lots))
        baselines = _probe_baselines(session, accounts, D(fx_lots * instruments.pair.lot_size))
        combined, _ = _probe_step(session, accounts[0], instruments, 2, set())
        first, seen = _probe_step(session, accounts[1], instruments, 1, set())
        second, _ = _probe_step(session, accounts[1], instruments, 1, seen)
        _verify_probe_sequence(session, accounts, baselines)
        require(all(native_state(session, dst).quantities == {
            instruments.bond.uid: D(2 * instruments.bond.lot)} for dst in accounts),
            'commission probe final quantities mismatch')
        comparison = compare_commissions((combined, first, second))
        session.records.append({'event': 'cny_commission_probe',
                                'samples': [asdict(item) for item in (combined, first, second)],
                                'comparison': comparison})
        print('Sandbox CNY commission probe: ' + comparison['verdict'], flush=True)
    finally:
        owned = tuple(session.lifecycle.created)
        session.lifecycle.cleanup()
        remaining = safe_call(session.client.users.get_accounts).accounts
        require(not any(item.id in owned for item in remaining),
                'commission probe cleanup unconfirmed')
        session.records.append({'event': 'cny_probe_cleanup', 'closed_accounts': len(owned),
                                'confirmed_absent': True})
