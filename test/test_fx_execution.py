"""Confirmed exchanges fund bounded buys through the neutral runtime, offline only."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import logging
from test.test_fx_funding import NOW, market
from test.test_order_plan import destination, leaf, parent, rules
from unittest.mock import create_autospec

import pytest

from autorepeater.cash_facts import MoneyAmount, OrderAmounts, SecurityQuote, SecurityQuoteUnit
from autorepeater.cash_pools import CashPools
from autorepeater.execution import ExecutionReceipt, OrderExecutionError, OrderExecutor
from autorepeater.execution_data import ExecutionData
from autorepeater.execution_market import ExecutionMarket, _Ledger, _credit_sales, _purchase_cash
from autorepeater.fx_data import FxData
from autorepeater.order_plan import build_order_plan
from autorepeater.order_plan import OrderIntent
from autorepeater.strategy_plan import exact_product, exact_sum
from autorepeater.valuation import SecurityData


def runtime():
    """Strict ports share a deterministic clock; no SDK connection exists."""
    fx = create_autospec(FxData, instance=True, spec_set=True)
    books = create_autospec(SecurityData, instance=True, spec_set=True)
    snapshot = market()
    fx.list_pairs.return_value = [quote.pair for quote in snapshot.quotes.values()]
    fx.get_quote.side_effect = lambda pair: snapshot.quotes[pair.base_currency]
    fx.get_trade_rules.side_effect = lambda account, pair: rules(pair.uid, lot=pair.lot_size)[
        pair.uid]
    native = {'C': ('cny', D(60)), 'U': ('usd', D('.1'))}
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        native.get(uid, ('rub', D(450)))[0], native.get(uid, ('rub', D(450)))[1],
        native.get(uid, ('rub', D(450)))[1], SecurityQuoteUnit.MONEY_PER_PIECE)
    return ExecutionMarket(fx, books, clock=lambda: NOW), fx, books


def test_explicit_unavailable_pair_can_value_cash_without_trading_or_retargeting():
    """Valuation identity is independent of permissions and of caller mapping mutations."""
    _, fx, books = runtime()
    quote = market().quotes['cny']
    chosen = replace(quote.pair, api_trade_available=False, buy_available=False,
                     sell_available=False)
    other = replace(quote.pair, uid='other')
    fx.list_pairs.return_value = [other, chosen]
    fx.get_quote.side_effect = None
    fx.get_quote.return_value = replace(quote, pair=chosen)
    selections = {'cny': chosen.uid}
    execution_market = ExecutionMarket(fx, books, clock=lambda: NOW, fx_pair_uids=selections)
    selections['cny'] = other.uid
    value = execution_market.valuation()
    assert value.to_rub(MoneyAmount('cny', D(100))) == MoneyAmount('rub', D(1200))
    fx.get_quote.assert_called_once_with(chosen)
    fx.get_trade_rules.assert_not_called()


def receipt(intent, total=None, fee='0', currency='rub', status='FILL'):
    """Order fields are explicit bounds; exact receipt.cash remains empty."""
    result = ExecutionReceipt(intent.uid, intent.side, 'broker-' + intent.uid, status,
                              intent.lots, intent.lots if status == 'FILL' else 0, {})
    if total is not None:
        result = replace(result, amounts=OrderAmounts(
            result.uid, result.side, result.order_id, intent.lots,
            MoneyAmount(currency, D(total)), MoneyAmount(currency, D(fee))))
    return result


def setup_plan(tree=None, cash=None, own=None, **options):
    """Start from the same pure FundingPlan that the real repeater consumes."""
    execution_market, fx, books = runtime()
    value = execution_market.valuation()
    value.to_rub(MoneyAmount('cny', D(1)))
    tree = tree or leaf(target={'C': D(1)}, budget='1000', floor='100')
    snapshot = replace(destination(options.get('holdings')), budget=tree.budget,
                       available_cash={'rub': D(1000)} if cash is None else cash,
                       fx_snapshot=value.fx)
    owners = options.get('owners', {(): dict(snapshot.quantities)})
    own = rules('C', currency='cny', buy=0) if own is None else own
    marks = {uid: value.to_rub(value.security(uid)).amount for uid in own}
    inputs = execution_market.funding_inputs('dst', value, own)
    if options.get('pools') is not None:
        inputs = replace(inputs, cash=options['pools'])
    snapshot = replace(snapshot, fx_snapshot=value.fx)
    plan = build_order_plan(tree, snapshot, owners, marks, own, funding=inputs)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    return execution_market, value, plan, data, executor, fx, books


def test_fx_fill_refreshes_real_native_caps_before_dependent_buy():
    """Initial cap0 is conditional; FILL alone cannot authorize the paper."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    fresh = replace(plan.snapshot, available_cash={'rub': D(219), 'cny': D(65)})
    data.get_destination.return_value = fresh
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '780' if order.uid == 'cny-rub' else '60',
        '1' if order.uid == 'cny-rub' else '.5',
        'rub' if order.uid == 'cny-rub' else 'cny')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub', 'C']
    assert [args.args[1].lots for args in executor.submit_order.call_args_list] == [65, 1]
    assert all(not item.cash for item in result)
    data.get_destination.assert_called_with('dst', valuation=value)
    data.get_trade_rules.assert_called_with('dst', ['C'])


@pytest.mark.parametrize('status', ['NEW', 'PARTIALLYFILL'])
def test_pending_fx_is_polled_by_broker_id_without_second_submission(status, monkeypatch):
    """FX BUY has bounded confirmation polling just like FX SELL."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    exchange = plan.funding.exchanges[0]
    executor.submit_order.return_value = receipt(exchange, status=status)
    executor.get_order_state.side_effect = OrderExecutionError('read failed')
    monkeypatch.setattr('autorepeater.execution.sleep', lambda delay: None)
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 30)
    with pytest.raises(OrderExecutionError, match='read failed'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    executor.get_order_state.assert_called_once_with('dst', 'broker-cny-rub')
    data.get_destination.assert_not_called()


@pytest.mark.parametrize('change', [{'uid': 'wrong'}, {'side': 'SELL'},
                                    {'lots_requested': 70}, {'lots_executed': 1},
                                    {'status': 'REJECTED'}, {'order_id': ''}])
def test_invalid_fx_receipt_stops_pass_without_dependent_submit(change):
    """No intent/request fallback repairs an inconsistent broker response."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    executor.submit_order.return_value = replace(receipt(plan.funding.exchanges[0]), **change)
    with pytest.raises(OrderExecutionError):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    data.get_destination.assert_not_called()


def test_closed_currency_preflight_prevents_other_composite_rub_sale():
    """Even a zero-cap conditional foreign demand blocks the first RUB effect."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='500', sell=True),
                   leaf((1,), {'C': D(1)}, budget='1000', floor='100')))
    broker, value, plan, data, executor, fx, _ = setup_plan(
        tree, own={**rules('R'), **rules('C', currency='cny', buy=0)},
        holdings={'R': D(2)}, owners={(0,): {'R': D(2)}, (1,): {}})
    fx.get_trade_rules.side_effect = lambda account, pair: replace(
        rules(pair.uid)[pair.uid], bestprice_order_available=False)
    assert not broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()


@pytest.mark.parametrize('currency', ['rub', 'cny'])
@pytest.mark.parametrize('principal', [D(450), D(456), D(444)])
def test_two_buys_can_spend_commission_reserve_without_second_floor(currency, principal):
    """1000/100 reserve: 450+5 then450+5 remains legal at545."""
    broker, fx, books = runtime()
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        currency, D(450) / (12 if currency == 'cny' else 1),
        D(450) / (12 if currency == 'cny' else 1), SecurityQuoteUnit.MONEY_PER_PIECE)
    value = broker.valuation()
    value.to_rub(MoneyAmount(currency, D(1)))
    tree = leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100')
    rate = value.fx.rate(currency)
    snapshot = replace(destination(), budget=D(1000), available_cash={currency: D(1000)/rate},
                       fx_snapshot=value.fx)
    own = rules('A', 'B', currency=currency)
    plan = build_order_plan(tree, snapshot, {(): {}}, dict.fromkeys(own, D(450)), own,
                            funding=broker.funding_inputs('dst', value, own))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = replace(
        snapshot, quantities={'A': D(1)}, available_quantities={'A': D(1)},
        available_cash={currency: (D(1000) - principal - D(5))/rate})
    data.get_trade_rules.return_value = {
        uid: replace(rule, buy_money_amount=(D(1000) - principal - D(5))/rate)
        for uid, rule in own.items()}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, str(principal/rate), str(D(5)/rate), currency)
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['A', 'B']
    assert executor.submit_order.call_count == 2
    if currency == 'rub':
        fx.get_trade_rules.assert_not_called()


@pytest.mark.parametrize('currency', ['rub', 'cny'])
@pytest.mark.parametrize('principal', ['89', '91'])
def test_security_capacity_uses_fixed_mark_not_receipt_or_refreshed_fx(currency, principal):
    """A shared fill consumes immutable RUB marks even when native price/FX move."""
    intent = OrderIntent('C', 'BUY', 1, 1, {(0,): D('.25'), (1,): D('.75')})
    mark = D(90) * (12 if currency == 'cny' else 1)
    ledger = _Ledger(
        CashPools({(0,): {currency: D(30)}, (1,): {currency: D(90)}}, {currency: D(1000)}),
        {}, {(0,): mark / 4, (1,): mark * D('.75')})
    fill = receipt(intent, principal, '1', currency)
    assert _purchase_cash(ledger, intent, fill, {'C': mark}, currency)
    assert ledger.limits == {(0,): D(0), (1,): D(0)}
    debit = D(principal) + 1
    assert ledger.cash.amount((0,), currency) == D(30) - debit / 4
    assert ledger.cash.amount((1,), currency) == D(90) - debit * D('.75')
    assert ledger.cash.protected == {currency: D(1000)}


@pytest.mark.parametrize('currency', ['rub', 'cny'])
@pytest.mark.parametrize('principal,fee', [('101', '0'), ('99', '2')])
def test_security_receipt_cannot_borrow_neighbor_or_unallocated_cash(currency, principal, fee):
    """Native principal plus fee must fit every original owner's cash, including reserve."""
    intent = OrderIntent('C', 'BUY', 1, 1, {(0,): D('.5'), (1,): D('.5')})
    ledger = _Ledger(
        CashPools({(0,): {currency: D(50)}, (1,): {currency: D(500)}}, {currency: D(1000)}),
        {}, {(0,): D(500), (1,): D(500)})
    before = ledger.cash
    assert not _purchase_cash(ledger, intent, receipt(intent, principal, fee, currency),
                              {'C': D(900)}, currency)
    assert ledger.cash == before
    assert ledger.limits == {(0,): D(500), (1,): D(500)}


@pytest.mark.parametrize('currency', ['rub', 'cny'])
def test_security_capacity_cannot_be_enlarged_by_a_cheaper_fill(currency):
    """Cash is sufficient, but a filled lot cannot consume more than the fixed ceiling."""
    intent = OrderIntent('C', 'BUY', 1, 1, {(): D(1)})
    ledger = _Ledger(CashPools({(): {currency: D(100)}}), {}, {(): D(89)})
    before = ledger.cash
    assert not _purchase_cash(ledger, intent, receipt(intent, '80', '1', currency),
                              {'C': D(90)}, currency)
    assert ledger.cash == before
    assert ledger.limits == {(): D(89)}


@pytest.mark.parametrize('currency', ['rub', 'cny'])
def test_security_partial_fill_stops_before_cash_attribution_or_neighbor(currency):
    """A partial BUY never spends capacity as if the requested pieces were filled."""
    uid = 'C' if currency == 'cny' else 'A'
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={uid: D(2), 'B': D(1)}, budget='2000', floor='200'),
        cash={currency: D(2000)}, own={**rules(uid, currency=currency), **rules('B')})
    executor.submit_order.side_effect = lambda account, order: replace(
        receipt(order, '450', '1', currency, status='PARTIALLYFILL'), lots_executed=1)
    with pytest.raises(OrderExecutionError, match='not fully confirmed'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    data.get_destination.assert_not_called()


def test_purchase_failure_logs_owner_bounds_and_remaining_uids(caplog):
    """Loaded facts explain why a preceding receipt stops the next GOLD purchase."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'A': D(1), 'GOLD': D(1)}, budget='1000', floor='100'),
        own=rules('A', 'GOLD'))
    executor.submit_order.side_effect = lambda account, order: receipt(order, '995', '6')
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        assert len(broker.execute('dst', plan, data, executor, value)) == 1
    assert 'reason=owner_native_cash_exceeded' in caplog.text
    assert 'capacity_rub=900.0' in caplog.text
    assert 'planning_cost_rub=450.0' in caplog.text
    assert 'principal_native=995.0' in caplog.text
    assert 'commission_native=6.0' in caplog.text
    assert 'debit_upper_native=1001.0' in caplog.text
    assert 'owner_cash_native=1000.0' in caplog.text
    assert "remaining_buy_uids=['GOLD']" in caplog.text
    assert 'terminal_reason=purchase_cash_rejected' in caplog.text
    data.get_destination.assert_not_called()


def _declining_plan(change, lot, holding, *, fx_required=False):
    """Keep original marks beside independently refreshed native books and FX."""
    broker, fx, books = runtime()
    currency = 'rub' if change == 'rub-price' else 'cny'
    rate = D(1) if currency == 'rub' else D(12)
    native = [D(450) / lot / rate]
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        currency if uid == 'A' else 'rub', native[0] if uid == 'A' else D(1),
        native[0] if uid == 'A' else D(1), SecurityQuoteUnit.MONEY_PER_PIECE)
    value = broker.valuation()
    own = {**rules('A', currency=currency, lot=lot, buy=0 if fx_required else 100000000),
           **rules('H')}
    inputs = broker.funding_inputs('dst', value, own)
    plan = build_order_plan(
        leaf(target={'A': D(2 * lot)}, budget='1000', floor='100'),
        replace(destination({'H': holding}), budget=D(1000),
                available_cash={'rub': D(999)} if fx_required else {currency: D(999) / rate},
                fx_snapshot=value.fx),
        {(): {'H': holding}}, {'A': D(450) / lot, 'H': D(1)}, own, funding=inputs)
    assert plan.funding.purchases[0].intent.lots == 1
    if change == 'fx-only':
        old = value.fx.quotes['cny']
        fx.get_quote.side_effect = lambda pair: replace(
            old, bid=D('11.8') if fx_required else D('11.96'),
            ask=D('11.8') if fx_required else D('11.96'))
    else:
        native[0] = (D(449) if currency == 'rub' else D(444) if fx_required
                     else D('448.8')) / lot / rate
    return broker, value, plan, native[0], currency


@pytest.mark.parametrize('change', ['rub-price', 'cny-price', 'fx-only'])
@pytest.mark.parametrize('lot', [1, 10])
@pytest.mark.parametrize('holding', [D(1), D('1e-80')])
def test_refreshed_decline_cannot_increase_fixed_mark_lots_before_submit(change, lot, holding):
    """Fresh two-lot affordability cannot enlarge the original one-lot capacity."""
    broker, value, plan, native, currency = _declining_plan(change, lot, holding)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.side_effect = AssertionError('no second funding step needed')
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    ceiling = exact_sum((D(900), holding.copy_negate()))

    def submit(_account, intent):
        assert exact_product(intent.pieces[()], plan.marks['A']) <= ceiling
        return receipt(intent, str(exact_product(D(intent.lots * lot), native)), '0', currency)

    executor.submit_order.side_effect = submit
    result = broker.execute('dst', plan, data, executor, value)
    assert len(result) == 1
    executor.submit_order.assert_called_once()
    assert executor.submit_order.call_args.args[1].lots == 1
    assert plan.marks == {'A': D(450) / lot, 'H': D(1)}
    data.get_destination.assert_not_called()


@pytest.mark.parametrize('change', ['cny-price', 'fx-only'])
def test_fx_exchange_funds_only_fixed_capacity_quantity_and_its_reserve(change):
    """Do not exchange for two lots when the immutable capacity permits just one."""
    broker, value, plan, native, currency = _declining_plan(change, 1, D(1), fx_required=True)
    assert currency == 'cny'
    assert plan.funding.exchanges[0].lots == 42
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(495), 'cny': D(42)})
    data.get_trade_rules.return_value = {
        **rules('A', currency='cny', buy=2), **rules('H')}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)

    def submit(_account, intent):
        if intent.uid == 'cny-rub':
            assert intent.lots == 42
            return receipt(intent, '504')
        assert intent.uid == 'A' and intent.lots == 1
        return receipt(intent, str(native), '0', 'cny')

    executor.submit_order.side_effect = submit
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.lots_executed) for item in result] == [('cny-rub', 42), ('A', 1)]
    assert executor.submit_order.call_count == 2
    assert data.get_destination.call_count == 1


def test_transport_uncertainty_never_retries_fx_or_reads_balances():
    """A failed PostOrder may already have traded; no second request is safe."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    executor.submit_order.side_effect = OrderExecutionError('unknown', request_id='request')
    with pytest.raises(OrderExecutionError, match='unknown'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    executor.get_order_state.assert_not_called()
    data.get_destination.assert_not_called()


@pytest.mark.parametrize('status', ['NEW', 'PARTIALLYFILL'])
def test_fx_confirmation_timeout_does_not_repeat_or_cancel(status):
    """The existing bounded window applies to either currency direction."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    executor.submit_order.return_value = receipt(plan.funding.exchanges[0], status=status)
    with pytest.raises(OrderExecutionError, match='not fully confirmed'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    executor.get_order_state.assert_not_called()
    data.get_destination.assert_not_called()


def test_fx_poll_rejects_changed_broker_id(monkeypatch):
    """The poll identifies the broker order, never the original client request."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    exchange = plan.funding.exchanges[0]
    executor.submit_order.return_value = receipt(exchange, status='NEW')
    executor.get_order_state.return_value = replace(receipt(exchange), order_id='other')
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 30)
    monkeypatch.setattr('autorepeater.execution.sleep', lambda delay: None)
    with pytest.raises(OrderExecutionError, match='ID mismatch'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()


@pytest.mark.parametrize('change', [{'buy_max_lots': 0}, {'buy_money_amount': D(59)},
                                    {'api_trade_available': False},
                                    {'bestprice_order_available': False}])
def test_fresh_native_caps_after_fx_can_defer_without_rollback(change):
    """A fill supplies money, not a fabricated future GetMaxLots permission."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = {'C': replace(rules('C', currency='cny')['C'], **change)}
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub']
    executor.submit_order.assert_called_once()


def test_delayed_fx_cash_waits_only_with_reads(monkeypatch):
    """FILL then insufficient money then available cash, with newly read own limits."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    empty = replace(plan.snapshot, available_cash={'rub': D(172), 'cny': D(0)})
    settled = replace(empty, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_destination.side_effect = [empty, settled]
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '828' if order.uid == 'cny-rub' else '60',
        currency='rub' if order.uid == 'cny-rub' else 'cny')
    delays = []
    monkeypatch.setattr('autorepeater.execution_market.sleep', delays.append)
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub', 'C']
    assert delays == [2]
    assert data.get_destination.call_count == 2
    assert data.get_trade_rules.call_count == 2


def test_fx_cash_timeout_defers_dependent_buy(monkeypatch):
    """No calendar wait for TOM; actual availability determines this timeout."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(0)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    monkeypatch.setattr('autorepeater.execution_market.SETTLEMENT_TIMEOUT', 0)
    assert len(broker.execute('dst', plan, data, executor, value)) == 1
    executor.submit_order.assert_called_once()


def test_incomplete_fx_cash_never_becomes_exact_receipt_or_isolated_credit():
    """A correctly filled exchange with incomplete money defers dependent funding."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    executor.submit_order.side_effect = lambda account, order: receipt(order)
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    result = broker.execute('dst', plan, data, executor, value)
    assert len(result) == 1 and result[0].cash == {}
    executor.submit_order.assert_called_once()
    data.get_destination.assert_called_once_with('dst', valuation=value)
    data.get_trade_rules.assert_called_once_with('dst', ['C'])


def test_reverse_exchange_finances_only_rub_paper():
    """One necessary exchange funds the paper and reserve; there is no reserve-only FX."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'R': D(1)}, budget='1200', floor='100'),
        cash={'cny': D(100)}, own=rules('R', buy=0))
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(551), 'cny': D(54)})
    data.get_trade_rules.return_value = rules('R', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '552' if order.uid == 'cny-rub' else '450', '1' if order.side == 'SELL' else '5')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('cny-rub', 'SELL'), ('R', 'BUY')]
    assert executor.submit_order.call_args_list[0].args[1].lots == 46
    assert executor.submit_order.call_count == 2


def test_rate_increase_after_fx_does_not_expand_original_security_ceiling():
    """RUB value may rise but the original target and900 RUB ceiling remain fixed."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    original = value.fx.quotes['cny']
    fx.get_quote.side_effect = [original, replace(original, bid=D(16), ask=D(16)),
                                replace(original, bid=D(16), ask=D(16))]
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub']
    executor.submit_order.assert_called_once()


def test_shared_later_sale_cannot_launder_earlier_unknown_isolated_proceeds():
    """One common seller does not establish ownership of another seller's money (#9)."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='1000', sell=True),
                   leaf((1,), {'C': D(1)}, budget='1000', sell=True)))
    # The root's unassigned sale can reach every leaf; the children cannot.
    tree = replace(tree, unassigned={'B': D(1)})
    broker, value, plan, _, _, _, _ = setup_plan(
        tree, cash={}, holdings={'A': D(1), 'B': D(1)},
        owners={(0,): {'A': D(1)}, (1,): {}, (): {'B': D(1)}},
        own={**rules('A', 'B', 'R'), **rules('C', currency='cny')})
    assert [intent.uid for intent in plan.sells] == ['A', 'B']
    ledger = _Ledger(CashPools({(0,): {}, (1,): {}}),
                     {(0,): {'A': D(0)}, (1,): {}, (): {'B': D(0)}}, {})
    snapshot = replace(plan.snapshot, available_cash={'rub': D(1000)})
    assert _credit_sales(plan, ledger, [receipt(intent) for intent in plan.sells], snapshot)
    assert ledger.cash.total('rub') == 0
    assert value.fx is plan.snapshot.fx_snapshot and broker is not None


def test_confirmed_security_sell_then_fx_then_buy_uses_fresh_balances():
    """Single-owner confirmed availability can fund FX without operation history."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'C': D(1)}, budget='1500', floor='150', sell=True),
        cash={'rub': D(150)}, own={**rules('R'), **rules('C', currency='cny', buy=0)},
        holdings={'R': D(3)})
    sold = replace(plan.snapshot, quantities={'R': D(0)}, available_quantities={'R': D(0)},
                   available_cash={'rub': D(1499)})

    def fresh(*_args, **_kwargs):
        orders = [call.args[1] for call in executor.submit_order.call_args_list]
        if len(orders) == 1:
            return sold
        lots = orders[-1].lots
        return replace(sold, available_cash={'rub': D(1499-lots*12-1), 'cny': D(lots)})
    data.get_destination.side_effect = fresh
    data.get_trade_rules.side_effect = [
        {**rules('R'), **rules('C', currency='cny', buy=0)},
        {**rules('R'), **rules('C', currency='cny', buy=1)}]
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, str(order.lots*450) if order.uid == 'R' else str(order.lots*12)
        if order.uid == 'cny-rub' else '60', '1',
        'cny' if order.uid == 'C' else 'rub')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [
        ('R', 'SELL'), ('cny-rub', 'BUY'), ('C', 'BUY')]
    assert executor.submit_order.call_args_list[1].args[1].lots == 67
    assert data.get_trade_rules.call_count == 2


@pytest.mark.parametrize('selling', [False, True])
def test_closed_currency_blocks_sale_without_fx_or_buy_from_native_balance(selling):
    """V3 is about paper settlement, not just the existence of a planned exchange."""
    kwargs = ({'tree': leaf(target={'R': D(1)}, budget='1000', sell=True),
               'holdings': {'C': D(1)}, 'cash': {},
               'own': {**rules('R', buy=0), **rules('C', currency='cny')}}
              if selling else {'cash': {'cny': D(80)}, 'own': rules('C', currency='cny')})
    broker, value, plan, data, executor, fx, _ = setup_plan(**kwargs)
    assert not plan.funding.exchanges
    fx.get_trade_rules.side_effect = lambda account, pair: replace(
        rules(pair.uid)[pair.uid], api_trade_available=False)
    assert not broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()


def test_foreign_noop_does_not_require_open_currency_trading():
    """Foreign capital still has a valuation rate, but an empty queue grants no FX."""
    broker, value, plan, data, executor, fx, _ = setup_plan(
        holdings={'C': D(1)}, cash={'cny': D(20)}, own=rules('C', currency='cny'))
    assert not plan.sells and not plan.funding.demands
    fx.get_trade_rules.reset_mock()
    fx.get_trade_rules.side_effect = AssertionError('no-op must not read currency trading')
    assert not broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()
    fx.get_trade_rules.assert_not_called()


def test_closed_currency_after_sale_stops_remaining_plan_without_rollback():
    """A market that closes after a confirmed effect defers all remaining orders."""
    broker, value, plan, data, executor, fx, _ = setup_plan(
        leaf(target={'C': D(1)}, budget='1500', floor='150', sell=True),
        cash={'rub': D(150)}, own={**rules('R'), **rules('C', currency='cny', buy=0)},
        holdings={'R': D(3)})
    data.get_destination.return_value = replace(
        plan.snapshot, quantities={}, available_quantities={}, available_cash={'rub': D(1499)})
    data.get_trade_rules.return_value = plan.rules
    executor.submit_order.side_effect = lambda account, order: receipt(order, '1350', '1')
    fx.get_trade_rules.side_effect = lambda account, pair: replace(
        rules(pair.uid)[pair.uid], api_trade_available=not executor.submit_order.called)
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('R', 'SELL')]


@pytest.mark.parametrize('change', [{'limits_ready': False}, {'active_orders': ('active',)},
                                    {'quantities': {'other': D(1)}}])
def test_post_fx_blocking_or_position_drift_defers_without_retry(change, monkeypatch):
    """A currency fill never relaxes the old physical/readiness guards."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)}, **change)
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    monkeypatch.setattr('autorepeater.execution_market.SETTLEMENT_TIMEOUT', 0)
    assert len(broker.execute('dst', plan, data, executor, value)) == 1
    executor.submit_order.assert_called_once()


def test_reverse_rub_surplus_cannot_fund_new_foreign_demand_in_same_pass():
    """RUB surplus remains cash even when it would cover a new USD lot."""
    tree = leaf(target={'R': D(1), 'U': D(1)}, budget='1200', floor='100')
    broker, value, plan, data, executor, _, books = setup_plan(
        tree, cash={'cny': D(100)}, own={**rules('R', buy=0), **rules('U', currency='usd', buy=0)})
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        'usd' if uid == 'U' else 'rub', D('.1') if uid == 'U' else D(450),
        D('.1') if uid == 'U' else D(450), SecurityQuoteUnit.MONEY_PER_PIECE)
    value.refresh_books()
    inputs = broker.funding_inputs('dst', value, plan.rules)
    snapshot = replace(plan.snapshot, fx_snapshot=value.fx)
    plan = build_order_plan(tree, snapshot, {(): {}}, {'R': D(450), 'U': D(9)}, plan.rules,
                            funding=inputs)
    data.get_destination.return_value = replace(
        snapshot, available_cash={'rub': D(552), 'cny': D(54)})
    data.get_trade_rules.return_value = {**rules('R', buy=1), **rules('U', currency='usd', buy=0)}
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '552' if order.uid == 'cny-rub' else '450')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('cny-rub', 'SELL'), ('R', 'BUY')]


def test_wrong_buy_monetary_currency_defers_following_orders_without_guessing_fx():
    """Identity may be valid while monetary dimensions cannot authorize a debit."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100'), own=rules('A', 'B'))
    executor.submit_order.side_effect = lambda account, order: receipt(order, '450', currency='usd')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['A']
    executor.submit_order.assert_called_once()


@pytest.mark.parametrize('status', ['NEW', 'PARTIALLYFILL'])
def test_pending_fx_fill_then_buy_uses_polled_facts_and_one_request(status, monkeypatch):
    """Successful delayed confirmation also keeps the physical submission count fixed."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    exchange = plan.funding.exchanges[0]
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: (
        receipt(order, status=status) if order.uid == 'cny-rub'
        else receipt(order, '60', currency='cny'))
    executor.get_order_state.return_value = receipt(exchange, '828')
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 30)
    monkeypatch.setattr('autorepeater.execution.sleep', lambda delay: None)
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub', 'C']
    executor.get_order_state.assert_called_once_with('dst', 'broker-cny-rub')
    assert executor.submit_order.call_count == 2


def test_composite_preflight_unions_two_foreign_leaves_before_rub_buy():
    """A closed USD sibling vetoes even a separately funded first RUB purchase."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='1000'),
                   leaf((1,), {'C': D(1)}, budget='1000', floor='100'),
                   leaf((2,), {'U': D(1)}, budget='1000', floor='100')))
    broker, value, plan, data, executor, fx, _ = setup_plan(
        tree, cash={'rub': D(3000)}, owners={(0,): {}, (1,): {}, (2,): {}},
        own={**rules('R'), **rules('C', currency='cny', buy=0),
             **rules('U', currency='usd', buy=0)})
    fx.get_trade_rules.side_effect = lambda account, pair: replace(
        rules(pair.uid)[pair.uid], bestprice_order_available=pair.base_currency != 'usd')
    assert not broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()
    assert {call.args[1].base_currency for call in fx.get_trade_rules.call_args_list} == {
        'cny', 'usd'}


def test_unknown_scoped_sale_defers_neighbor_but_keeps_preexisting_owner_money():
    """Unknown money restricts its dependent flow; it does not authorize the neighbor (#9)."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='1000', sell=True),
                   leaf((1,), {'C': D(1)}, budget='1000', floor='100')))
    broker, value, plan, data, executor, _, _ = setup_plan(
        tree, cash={'rub': D(1000)}, holdings={'A': D(1)},
        owners={(0,): {'A': D(1)}, (1,): {}},
        own={**rules('A', 'R'), **rules('C', currency='cny', buy=0)},
        pools=CashPools({(0,): {'rub': D(550)}, (1,): {}}, {'rub': D(450)}))
    data.get_destination.return_value = replace(
        plan.snapshot, quantities={}, available_quantities={}, available_cash={'rub': D(1449)})
    data.get_trade_rules.return_value = plan.rules
    executor.submit_order.side_effect = lambda account, order: receipt(order, '450', '1')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('A', 'SELL'), ('R', 'BUY')]
    assert executor.submit_order.call_args_list[1].args[1].pieces == {(0,): D(1)}
    assert all(not item.cash for item in result)


@pytest.mark.parametrize('change', [{'limits_ready': False}, {'active_orders': ('pending',)}])
def test_initial_unreadiness_never_submits_or_reads_fx_execution(change):
    """The established readiness barrier precedes even fresh FX execution reads."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    plan = replace(plan, snapshot=replace(plan.snapshot, **change))
    fx.get_quote.reset_mock()
    assert not broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()
    fx.get_quote.assert_not_called()


@pytest.mark.parametrize('stage', ['valuation', 'execution'])
def test_changed_fx_pair_identity_rejects_before_effect(stage):
    """A numerically identical quote cannot replace the chosen route UID/lot contract."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    old = value.fx.quotes['cny']
    fx.get_quote.side_effect = lambda pair: replace(old, pair=replace(old.pair, uid='other'))
    with pytest.raises(ValueError, match='identity|pair changed'):
        if stage == 'execution':
            broker.execute('dst', plan, data, executor, value)
        else:
            broker.valuation().to_rub(MoneyAmount('cny', D(1)))
    executor.submit_order.assert_not_called()


@pytest.mark.parametrize('total,fee', [('1001', '0'), ('828', '173'), ('0', '0')])
def test_fx_cash_bound_cannot_spend_more_than_owner_or_synthesize_free_currency(total, fee):
    """Even with a FILL, bad money facts defer dependencies and force a fresh read."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(0), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(order, total, fee)
    result = broker.execute('dst', plan, data, executor, value)
    assert len(result) == 1
    executor.submit_order.assert_called_once()
    data.get_destination.assert_called_once_with('dst', valuation=value)


@pytest.mark.parametrize('cash,cap', [('449', '1000'), ('545', '449')])
def test_second_buy_remains_bounded_by_actual_cash_and_own_money(cash, cap):
    """Removing the repeated floor does not relax real funding/caps."""
    broker, value, plan, data, executor, _, _ = setup_plan(
        leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100'), own=rules('A', 'B'))
    data.get_destination.return_value = replace(
        plan.snapshot, quantities={'A': D(1)}, available_quantities={'A': D(1)},
        available_cash={'rub': D(cash)})
    data.get_trade_rules.return_value = {
        uid: replace(rule, buy_money_amount=D(cap)) for uid, rule in plan.rules.items()}
    executor.submit_order.side_effect = lambda account, order: receipt(order, '450', '5')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['A']


def test_quote_expiring_during_caps_read_is_refreshed_before_first_effect():
    """The 180-second limit is checked at submission, not merely at book receipt."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    clock = [NOW]
    broker.clock = lambda: clock[0]
    old = value.fx.quotes['cny']
    fx.get_quote.side_effect = lambda pair: replace(
        old, market_time=clock[0], received_time=clock[0])

    def delayed_caps(_account, pair):
        clock[0] = NOW + timedelta(seconds=181)
        return rules(pair.uid)[pair.uid]

    fx.get_trade_rules.side_effect = delayed_caps
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '828' if order.uid == 'cny-rub' else '60',
        currency='rub' if order.uid == 'cny-rub' else 'cny')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub', 'C']
    assert fx.get_quote.call_count == 4  # discovery, initial, expired refresh, after FX


def test_invalid_financial_plan_rejected_before_any_execution_read_or_effect():
    """Direct execution retains the validation barrier of the original executor."""
    broker, value, plan, data, executor, fx, _ = setup_plan()
    plan = replace(plan, strategy=replace(plan.strategy, cash_floor=D(1001)))
    fx.get_quote.reset_mock()
    with pytest.raises(ValueError):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_not_called()
    fx.get_quote.assert_not_called()


def test_shared_fx_has_exact_owner_units_and_single_dependent_order():
    """The money exchange never changes the fixed fractional security owners."""
    tree = parent((leaf((0,), {'C': D('.5')}, budget='500', floor='50'),
                   leaf((1,), {'C': D('.5')}, budget='500', floor='50')))
    broker, value, plan, data, executor, _, _ = setup_plan(
        tree, owners={(0,): {}, (1,): {}}, own=rules('C', currency='cny', buy=0))
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(219), 'cny': D(65)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=1)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '780' if order.uid == 'cny-rub' else '60', '1',
        'rub' if order.uid == 'cny-rub' else 'cny')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['cny-rub', 'C']
    exchange, purchase = [call.args[1] for call in executor.submit_order.call_args_list]
    assert exchange.pieces == {(0,): D('32.5'), (1,): D('32.5')}
    assert purchase.pieces == {(0,): D('.5'), (1,): D('.5')}
    assert plan.ownership == {(0,): {}, (1,): {}}


def test_rub_settlement_ignores_closed_foreign_trading_even_with_usd_valuation():
    """A nominal/reference FX rate cannot become a foreign settlement requirement."""
    broker, value, plan, data, executor, fx, _ = setup_plan(
        leaf(target={'R': D(1)}, budget='1000', floor='100'), own=rules('R'))
    value.to_rub(MoneyAmount('usd', D(1)))
    plan = replace(plan, snapshot=replace(plan.snapshot, fx_snapshot=value.fx))
    fx.get_trade_rules.side_effect = lambda account, pair: replace(
        rules(pair.uid)[pair.uid], api_trade_available=False, bestprice_order_available=False)
    executor.submit_order.side_effect = lambda account, order: receipt(order, '450', '5')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('R', 'BUY')]
    assert value.fx.rate('usd') == D(90)
