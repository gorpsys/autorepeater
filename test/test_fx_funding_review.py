"""Cold-review regressions through pure planning and the real execution loop."""
from dataclasses import replace
from decimal import Decimal as D, getcontext, localcontext
from test.test_composite_strategy import make_composite
from test.test_fx_execution import receipt, runtime
from test.test_fx_funding import NOW, market
from test.test_index_currency import document
from test.test_order_plan import destination, leaf, parent, rules
from unittest.mock import create_autospec, patch

import pytest

from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit
from autorepeater.cash_pools import CashPools, split_cash
from autorepeater.execution import OrderExecutionError, OrderExecutor
from autorepeater.execution_data import ExecutionData
from autorepeater.execution_market import _Ledger, _purchase_cash
from autorepeater.fx import FxSnapshot
from autorepeater.fx_funding import FundingInputs, FxPolicy
from autorepeater.index_config import validate_index_config
from autorepeater.index_strategy import IndexStrategy
from autorepeater.order_plan import build_order_plan
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_data import (
    InstrumentInfo, InstrumentMatch, InstrumentType, PortfolioEntry, PortfolioSnapshot,
    PriceQuote, StrategyData,
)
from autorepeater.strategy_plan import exact_product, exact_sum


def direct_market(cny_lot=10):
    """Each CNY lot has ten units; USD lots have one, with exact direct quotes."""
    quotes = dict(market().quotes)
    cny = quotes['cny']
    quotes['cny'] = replace(cny, pair=replace(cny.pair, lot_size=cny_lot))
    return FxSnapshot(quotes, NOW)


def reverse_case(cny='20', usd='3', *, inputs=None, tree=None, own=None):
    """450 RUB needs both native sources unless one source can cover it alone."""
    tree = tree or leaf(target={'R': D(1)}, budget='2000')
    snapshot = replace(destination(), budget=tree.budget,
                       available_cash={'cny': D(cny), 'usd': D(usd)},
                       fx_snapshot=direct_market())
    inputs = inputs or FundingInputs(fx_rules={
        **rules('cny-rub', lot=10), **rules('usd-rub')})
    own = own or rules('R', buy=0)
    owners = ({node.path: {} for node in tree.children} if tree.children else {(): {}})
    return build_order_plan(tree, snapshot, owners, {'R': D(450)}, own, funding=inputs)


def reverse_runtime(plan, fee='0'):
    """Actual ExecutionMarket, with independent strict read/submit ports."""
    broker, fx, books = runtime()
    fx.list_pairs.return_value = [quote.pair for quote in plan.snapshot.fx_snapshot.quotes.values()]
    fx.get_quote.side_effect = lambda pair: plan.snapshot.fx_snapshot.quotes[pair.base_currency]
    fx.get_trade_rules.side_effect = lambda account, pair: rules(pair.uid, lot=pair.lot_size)[
        pair.uid]
    value = broker.valuation()
    for code in ('cny', 'usd'):
        value.to_rub(MoneyAmount(code, D(1)))
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    cash = dict(plan.snapshot.available_cash)
    events = []

    def fresh(*_args, **_kwargs):
        events.append('balance')
        return replace(plan.snapshot, available_cash=dict(cash))

    def caps(*_args):
        events.append('security-caps')
        return rules('R', buy=int(cash.get('rub', D(0)) // 450))

    def submit(_account, intent):
        events.append(intent.uid)
        if intent.side == 'SELL':
            code = intent.uid[:3]
            amount = D(intent.lots * intent.lot_size)
            assert amount <= cash[code]
            cash[code] -= amount
            total = exact_product(amount, value.fx.rate(code))
            cash['rub'] = exact_sum((cash.get('rub', D(0)), total, D(fee).copy_negate()))
        else:
            total = D(450)
            assert cash['rub'] >= exact_sum((total, D(fee)))
            cash['rub'] -= exact_sum((total, D(fee)))
        return receipt(intent, str(total), fee)

    data.get_destination.side_effect = fresh
    data.get_trade_rules.side_effect = caps
    executor.submit_order.side_effect = submit
    return broker, value, data, executor, fx, books, cash, events


def test_two_reverse_sources_are_planned_with_exact_native_lots():
    """20 CNY gives240; three USD gives270, together funding the450 RUB paper."""
    plan = reverse_case()
    assert [(item.uid, item.side, item.lots) for item in plan.funding.exchanges] == [
        ('cny-rub', 'SELL', 2), ('usd-rub', 'SELL', 3)]
    assert plan.funding.purchases[0].intent.lots == 1
    assert plan.funding.projected_cash.amount((), 'rub') == D(60)


def test_runtime_allows_second_reverse_source_before_dependent_rub_buy():
    """First reverse proceeds forbid foreign BUY, but cannot forbid another native SELL."""
    plan = reverse_case()
    broker, value, data, executor, _, _, cash, events = reverse_runtime(plan)
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [
        ('cny-rub', 'SELL'), ('usd-rub', 'SELL'), ('R', 'BUY')]
    assert cash == {'cny': D(0), 'usd': D(0), 'rub': D(60)}
    assert events == ['cny-rub', 'balance', 'security-caps',
                      'usd-rub', 'balance', 'security-caps', 'R']
    assert all(not item.cash for item in result)


@pytest.mark.parametrize('cny', ['5', '25', '30'])
def test_single_sufficient_source_wins_over_alphabetical_partial_source(cny):
    """Neither fractional source lots nor alphabetic order justify two FX operations."""
    plan = reverse_case(cny, '10')
    assert [(item.uid, item.side, item.lots) for item in plan.funding.exchanges] == [
        ('usd-rub', 'SELL', 5)]
    assert plan.funding.purchases[0].intent.lots == 1
    assert plan.funding.projected_cash.amount((), 'cny') == D(cny)


def test_runtime_uses_only_sufficient_usd_source_leaving_cny_untouched():
    """25 CNY must not be submitted as30, nor sold ahead of a sufficient USD source."""
    plan = reverse_case('25', '10')
    broker, value, data, executor, _, _, cash, _ = reverse_runtime(plan)
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('usd-rub', 'SELL'), ('R', 'BUY')]
    assert cash == {'cny': D(25), 'usd': D(5), 'rub': D(0)}


def exact_owner_case(*, actual_shortfall=D(0), fee=D(0), currency='rub'):
    """Finite decimal attribution carries a real sub-28-digit residual."""
    amounts = [D('50.000000000000000000000000001'), D('49.999999999999999999999999999')]
    pieces = [D('0.50000000000000000000000000001'),
              D('0.49999999999999999999999999999')]
    rate = market().rate(currency)
    costs = [exact_sum((amount, exact_product(part, fee)))
             for amount, part in zip(amounts, pieces)]
    tree = parent(tuple(leaf((i,), {'R': part}, budget=str(exact_product(amount, rate)))
                        for i, (part, amount) in enumerate(zip(pieces, costs))))
    money = {path: {currency: exact_sum((amount, actual_shortfall.copy_negate()))}
             for path, amount in zip(((0,), (1,)), costs)}
    snapshot = replace(destination(), budget=tree.budget, fx_snapshot=market(),
                       available_cash={currency: exact_sum(
                           item[currency] for item in money.values())})
    price = D(100)
    mark = exact_product(price, snapshot.fx_snapshot.rate(currency))
    inputs = FundingInputs(cash=CashPools(money), native_prices={'R': MoneyAmount(currency, price)},
                           fees={'R': {currency: fee}})
    plan = build_order_plan(tree, snapshot, {(0,): {}, (1,): {}}, {'R': mark},
                            rules('R', currency=currency), funding=inputs)
    return plan, dict(zip(((0,), (1,)), pieces))


def test_owned_exact_capacity_is_not_redivided_at_precision28():
    """50 +/-1e-27 of owned RUB still funds one physical100 RUB lot exactly."""
    plan, pieces = exact_owner_case()
    assert plan.buys[0].pieces == pieces  # Previous RUB planner already proves capacity.
    purchase, = plan.funding.purchases
    assert purchase.intent.pieces == pieces
    assert exact_sum(purchase.intent.pieces.values()) == 1
    for path, quantity in pieces.items():
        assert exact_product(quantity, D(100)) == plan.funding.cash.amount(path, 'rub')


def test_exact_capacity_runs_through_real_executor_without_fx():
    """The physical BUY and owner contributions survive the runtime recalculation."""
    plan, pieces = exact_owner_case()
    broker, _, books = runtime()
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        'rub', D(100), D(100), SecurityQuoteUnit.MONEY_PER_PIECE)
    value = broker.valuation()
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda account, order: receipt(order, '100')
    result = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in result] == ['R']
    assert executor.submit_order.call_args.args[1].pieces == pieces


def test_composite_index_runtime_preserves_fully_funded_shared_lot():
    """Actual COMPOSITE/INDEX, including fixed attribution of an existing shared UID."""
    payload = document()
    payload['instruments'] = [dict(payload['instruments'][0], ticker='R')]
    config = validate_index_config(payload)
    strategy = make_composite([IndexStrategy(config), IndexStrategy(config)], weights=[
        '0.3333333333333333333333333333', '0.6666666666666666666666666667'])
    strategy_data = create_autospec(StrategyData, instance=True, spec_set=True)
    strategy_data.find_instruments.return_value = [InstrumentMatch(
        'R', 'R', 'R', InstrumentType.SHARE, 'TQBR')]
    strategy_data.get_instrument.return_value = InstrumentInfo(
        'R', 'R', 'R', InstrumentType.SHARE, 'TQBR', 1, 'rub', True)
    strategy_data.get_last_prices.return_value = [PriceQuote('R', D(100), None)]
    broker, _, books = runtime()
    books.get_security_quote.side_effect = lambda uid: SecurityQuote(
        'rub', D(100), D(100), SecurityQuoteUnit.MONEY_PER_PIECE)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    initial = replace(destination({'R': D('.1')}), portfolio=PortfolioSnapshot((
        PortfolioEntry('R', InstrumentType.SHARE, 'rub', D(100), D('.1'), ''),)),
                       budget=D(310), available_cash={'rub': D(290)})
    data.get_destination.side_effect = lambda account, *, valuation: replace(
        initial, fx_snapshot=valuation.fx)
    data.get_trade_rules.return_value = rules('R')
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda account, order: receipt(order, str(order.lots * 100))
    with patch('autorepeater.repeater.reporting.print_rebalance_plan') as report:
        result = AutoRepeater(strategy, strategy_data, data, executor, market=broker).sync_accounts(
            'dst')
    plan = report.call_args.args[0]
    assert plan.strategy.target.quantities == {'R': D(3)}
    assert plan.buys[0].lots == 2
    assert [item.uid for item in result] == ['R']
    intent = executor.submit_order.call_args.args[1]
    assert intent.lots == 2
    assert exact_sum(intent.pieces.values()) == 2
    for path, pieces in intent.pieces.items():
        assert exact_product(pieces, D(100)) <= plan.funding.cash.amount(path, 'rub')
        assert exact_product(pieces, D(100)) <= plan.funding.spending_limits[path]


@pytest.mark.parametrize('currency', ['rub', 'cny'])
@pytest.mark.parametrize('fee', [D(0), D(2)])
def test_exact_owner_units_with_fees_and_native_cash(currency, fee):
    """Finite fee contributions must not erase dust or debit another owner's reserve."""
    plan, pieces = exact_owner_case(fee=fee, currency=currency)
    purchase, = plan.funding.purchases
    assert purchase.intent.pieces == pieces
    for path, quantity in pieces.items():
        assert plan.funding.security_debits.amount(path, currency) == exact_product(
            quantity, exact_sum((D(100), fee)))
        assert plan.funding.projected_cash.amount(path, currency) == 0


@pytest.mark.parametrize('currency', ['rub', 'cny'])
def test_a_real_subprecision_cash_shortfall_is_not_corrected_up(currency):
    """Higher arithmetic precision never manufactures missing actual money."""
    plan, _ = exact_owner_case(actual_shortfall=D('1e-28'), currency=currency)
    assert plan.funding.demands
    assert not plan.funding.purchases and not plan.funding.exchanges


def test_exact_receipt_bound_debits_the_same_owners_without_fabricating_cash():
    """Verified money and the original RUB ceilings use the identical finite split."""
    plan, _ = exact_owner_case()
    intent = plan.buys[0]
    ledger = _Ledger(plan.funding.cash, {(0,): {}, (1,): {}}, dict(plan.funding.spending_limits))
    fact = receipt(intent, '100')
    assert _purchase_cash(ledger, intent, fact, plan.snapshot.fx_snapshot, 'rub')
    assert ledger.cash.total('rub') == 0
    assert all(amount == 0 for amount in ledger.limits.values())
    assert fact.cash == {}


@pytest.mark.parametrize('status', ['REJECTED', 'PARTIALLYFILL', 'uncertain'])
def test_first_reverse_failure_never_resubmits_or_unlocks_other_orders(status, monkeypatch):
    """Two intended sources are not permission to trade after an uncertain first order."""
    plan = reverse_case()
    broker, value, data, executor, _, _, _, _ = reverse_runtime(plan)
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 0)
    if status == 'uncertain':
        executor.submit_order.side_effect = OrderExecutionError('unknown outcome')
    else:
        executor.submit_order.side_effect = lambda account, order: replace(
            receipt(order, status=status), lots_executed=1 if status == 'PARTIALLYFILL' else 0)
    with pytest.raises(OrderExecutionError):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    executor.get_order_state.assert_not_called()
    data.get_destination.assert_not_called()


@pytest.mark.parametrize('block', ['cash', 'margin', 'readiness', 'closed'])
def test_second_reverse_source_requires_fresh_own_money_caps_and_readiness(block, monkeypatch):
    """The first FX does not authorize another source from stale, blocked or margin facts."""
    plan = reverse_case()
    broker, value, data, executor, fx, _, _, events = reverse_runtime(plan)
    monkeypatch.setattr('autorepeater.execution_market.SETTLEMENT_TIMEOUT', 0)
    if block in ('cash', 'readiness'):
        data.get_destination.side_effect = lambda *_args, **_kwargs: replace(
            plan.snapshot, available_cash={'cny': D(0), 'usd': D(0 if block == 'cash' else 3),
                                           'rub': D(240)}, limits_ready=block != 'readiness')
    else:
        fx.get_trade_rules.side_effect = lambda account, pair: replace(
            rules(pair.uid, lot=pair.lot_size)[pair.uid],
            sell_max_lots=0 if block == 'margin' and pair.base_currency == 'usd'
            and 'cny-rub' in events else 1000,
            api_trade_available=not (block == 'closed' and 'cny-rub' in events))
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('cny-rub', 'SELL')]
    executor.submit_order.assert_called_once()


def test_unavailable_first_source_can_be_skipped_without_using_margin():
    """A stale/disabled CNY cap cannot hide a sufficient owned USD source."""
    plan = reverse_case('25', '10', inputs=FundingInputs(fx_rules={
        **rules('cny-rub', lot=10, sell=0), **rules('usd-rub')}))
    assert [(item.uid, item.lots) for item in plan.funding.exchanges] == [('usd-rub', 5)]


def test_fractional_native_source_can_combine_only_executable_lots():
    """25 CNY and3 USD sell20+3, retaining the five untradeable CNY units."""
    plan = reverse_case('25', '3')
    assert [(item.uid, item.lots) for item in plan.funding.exchanges] == [
        ('cny-rub', 2), ('usd-rub', 3)]
    assert plan.funding.projected_cash.amount((), 'cny') == 5
    assert plan.funding.projected_cash.amount((), 'rub') == 60


def test_reverse_sources_cannot_borrow_a_buy_only_siblings_native_cash():
    """Account-wide USD would suffice, but belongs exclusively to the other leaf."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='1000'),
                   leaf((1,), {'R': D(1)}, budget='1000')))
    inputs = FundingInputs(cash=CashPools({(0,): {'cny': D(25)}, (1,): {'usd': D(10)}}),
                           fx_rules={**rules('cny-rub', lot=10), **rules('usd-rub')})
    result = reverse_case('25', '10', inputs=inputs, tree=tree).funding
    assert not result.purchases and not result.exchanges


def test_insufficient_combined_executable_native_money_defers_everything():
    """Raw CNY25+USD2 values480 RUB, but the executable lots yield only420."""
    result = reverse_case('25', '2').funding
    assert result.demands
    assert not result.purchases and not result.exchanges


def test_direction_policy_retains_native_sells_but_forbids_bridge_and_repeats():
    """Planner and runtime share one explicit pass-local FX permission transition."""
    policy = FxPolicy().after_fill('cny-rub', 'SELL')
    assert policy.permits('usd-rub', 'SELL')
    assert not policy.permits('usd-rub', 'BUY')
    assert not policy.permits('cny-rub', 'SELL')
    assert not policy.permits('cny-rub', 'BUY')


def test_rounding_fix_does_not_change_the_global_decimal_context():
    """The calculation's local precision is independent of caller context."""
    with localcontext(prec=19):
        plan, pieces = exact_owner_case()
        assert plan.funding.purchases[0].intent.pieces == pieces
        assert getcontext().prec == 19


def test_two_reverse_fills_and_buy_can_pay_fees_from_existing_reserve():
    """510 total/floor60: three fees leave57, with neither a second floor nor FX restock."""
    plan = reverse_case(tree=leaf(target={'R': D(1)}, budget='510', floor='60'))
    broker, value, data, executor, _, _, cash, _ = reverse_runtime(plan, fee='1')
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [
        ('cny-rub', 'SELL'), ('usd-rub', 'SELL'), ('R', 'BUY')]
    assert cash == {'cny': D(0), 'usd': D(0), 'rub': D(57)}
    assert len(result) == executor.submit_order.call_count == 3


def test_first_reverse_fill_without_monetary_bound_defers_all_dependencies():
    """Fresh global RUB cannot substitute for missing owner credit facts."""
    plan = reverse_case()
    broker, value, data, executor, _, _, _, _ = reverse_runtime(plan)
    executor.submit_order.side_effect = lambda account, order: receipt(order)
    data.get_destination.side_effect = lambda *_args, **_kwargs: replace(
        plan.snapshot, available_cash={'cny': D(0), 'usd': D(3), 'rub': D(240)})
    result = broker.execute('dst', plan, data, executor, value)
    assert [(item.uid, item.side) for item in result] == [('cny-rub', 'SELL')]
    assert result[0].cash == {}
    executor.submit_order.assert_called_once()


def test_first_reverse_partial_poll_failure_preserves_id_and_stops_dependencies(monkeypatch):
    """A real one-of-two-lot partial is polled by broker ID, never resubmitted."""
    plan = reverse_case()
    broker, value, data, executor, _, _, _, _ = reverse_runtime(plan)
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 30)
    monkeypatch.setattr('autorepeater.execution.sleep', lambda delay: None)
    executor.submit_order.side_effect = lambda account, order: replace(
        receipt(order, status='PARTIALLYFILL'), lots_executed=1)
    executor.get_order_state.side_effect = OrderExecutionError('poll failed')
    with pytest.raises(OrderExecutionError, match='poll failed'):
        broker.execute('dst', plan, data, executor, value)
    executor.submit_order.assert_called_once()
    executor.get_order_state.assert_called_once_with('dst', 'broker-cny-rub')
    data.get_destination.assert_not_called()


def test_native_demand_protects_its_currency_before_reverse_source_search():
    """Five USD units belong to the USD paper, even though selling them could buy RUB."""
    tree = leaf(target={'R': D(1), 'U': D(1)}, budget='1000')
    own = {**rules('R', buy=0), **rules('U', currency='usd')}
    snapshot = replace(destination(), budget=D(1000),
                       available_cash={'cny': D(25), 'usd': D(5)}, fx_snapshot=direct_market())
    inputs = FundingInputs(native_prices={'U': MoneyAmount('usd', D(5))},
                           fx_rules={**rules('cny-rub', lot=10), **rules('usd-rub')})
    result = build_order_plan(tree, snapshot, {(): {}}, {'R': D(450), 'U': D(450)},
                              own, funding=inputs).funding
    assert [item.intent.uid for item in result.purchases] == ['U']
    assert not result.exchanges
    assert result.projected_cash.amount((), 'cny') == 25


def test_owned_usd_cannot_pay_for_an_isolated_siblings_rub_shortage():
    """USD paper remains executable; its owner's money cannot rescue another leaf."""
    tree = parent((leaf((0,), {'R': D(1)}, budget='1000'),
                   leaf((1,), {'U': D(1)}, budget='1000')))
    own = {**rules('R', buy=0), **rules('U', currency='usd')}
    snapshot = replace(destination(), budget=D(2000),
                       available_cash={'cny': D(25), 'usd': D(10)}, fx_snapshot=direct_market())
    inputs = FundingInputs(native_prices={'U': MoneyAmount('usd', D(10))},
                           cash=CashPools({(0,): {'cny': D(25)}, (1,): {'usd': D(10)}}),
                           fx_rules={**rules('cny-rub', lot=10), **rules('usd-rub')})
    result = build_order_plan(tree, snapshot, {(0,): {}, (1,): {}},
                              {'R': D(450), 'U': D(900)}, own, funding=inputs).funding
    assert [item.intent.uid for item in result.purchases] == ['U']
    assert result.purchases[0].intent.pieces == {(1,): D(1)}
    assert not result.exchanges


def test_three_exact_owners_keep_even_the_smallest_positive_contribution():
    """A1e-70 owner is still a real owner, with exact cash and physical lot total."""
    pieces = [D('.5'), exact_sum((D('.5'), D('-1e-70'))), D('1e-70')]
    amounts = [exact_product(piece, D(100)) for piece in pieces]
    tree = parent(tuple(leaf((index,), {'R': piece}, budget=str(amount))
                        for index, (piece, amount) in enumerate(zip(pieces, amounts))))
    inputs = FundingInputs(cash=CashPools({(index,): {'rub': amount}
                                          for index, amount in enumerate(amounts)}))
    snapshot = replace(destination(), fx_snapshot=market())
    result = build_order_plan(tree, snapshot, dict.fromkeys(((0,), (1,), (2,)), {}),
                              {'R': D(100)}, rules('R'), funding=inputs).funding
    purchase, = result.purchases
    assert purchase.intent.pieces == {(index,): piece for index, piece in enumerate(pieces)}
    assert split_cash(D(100), pieces) == tuple(amounts)
    assert exact_sum(purchase.intent.pieces.values()) == 1


def test_repeating_direct_fx_ratio_keeps_lots_fees_and_owned_rub_ceiling():
    """13 RUB per3 CNY needs repeating division, but no extra budget or foreign bridge."""
    quotes = dict(direct_market().quotes)
    quotes['cny'] = replace(quotes['cny'], pair=replace(quotes['cny'].pair, nominal=D(3)),
                            bid=D(13), ask=D(13))
    snapshot = replace(destination(), budget=D(500), available_cash={'cny': D(50), 'usd': D(3)},
                       fx_snapshot=FxSnapshot(quotes, NOW))
    inputs = FundingInputs(fx_rules={**rules('cny-rub', lot=10), **rules('usd-rub')},
                           fx_fees={'cny-rub': {'rub': D(1)}, 'usd-rub': {'rub': D(1)}})
    result = build_order_plan(leaf(target={'R': D(1)}, budget='500', floor='50'), snapshot,
                              {(): {}}, {'R': D(450)}, rules('R', buy=0), funding=inputs).funding
    assert [(item.uid, item.lots) for item in result.exchanges] == [
        ('cny-rub', 5), ('usd-rub', 3)]
    assert result.purchases[0].intent.lots == 1
    assert result.projected_cash.amount((), 'rub') > 0
    assert result.projected_cash.amount((), 'rub') < 50
    assert result.projected_cash.amount((), 'cny') == 0
    assert result.projected_cash.amount((), 'usd') == 0


def test_reverse_policy_blocks_an_otherwise_affordable_foreign_fx():
    """A real RUB credit can afford a USD lot but cannot grant a forward direction."""
    tree = leaf(target={'U': D(1)}, budget='1000')
    snapshot = replace(destination(cash='1000'), budget=D(1000), fx_snapshot=market())
    inputs = FundingInputs(native_prices={'U': MoneyAmount('usd', D('.1'))},
                           fx_rules=rules('usd-rub'),
                           fx_policy=FxPolicy().after_fill('cny-rub', 'SELL'))
    result = build_order_plan(tree, snapshot, {(): {}}, {'U': D(9)},
                              rules('U', currency='usd', buy=0), funding=inputs).funding
    assert result.demands
    assert not result.purchases and not result.exchanges


def test_runtime_reverse_lot_surplus_cannot_bridge_even_when_a_usd_lot_is_affordable():
    """CNY lot100 yields1200; after RUB450, the750 surplus exceeds USD lot90."""
    broker, fx, _ = runtime()
    quotes = direct_market(100)
    fx.list_pairs.return_value = [quote.pair for quote in quotes.quotes.values()]
    fx.get_quote.side_effect = lambda pair: quotes.quotes[pair.base_currency]
    fx.get_trade_rules.side_effect = lambda account, pair: rules(pair.uid, lot=pair.lot_size)[
        pair.uid]
    value = broker.valuation()
    value.to_rub(MoneyAmount('cny', D(1)))
    tree = leaf(target={'R': D(1), 'U': D(1)}, budget='1200')
    own = {**rules('R', buy=0), **rules('U', currency='usd', buy=0)}
    inputs = broker.funding_inputs('dst', value, own)
    snapshot = replace(destination(cash='0'), budget=tree.budget,
                       available_cash={'cny': D(100)}, fx_snapshot=value.fx)
    plan = build_order_plan(tree, snapshot, {(): {}}, {'R': D(450), 'U': D(9)},
                            own, funding=inputs)
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_destination.return_value = replace(snapshot, available_cash={'rub': D(1200)})
    data.get_trade_rules.return_value = {**rules('R'), **rules('U', currency='usd')}
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    executor.submit_order.side_effect = lambda account, order: receipt(
        order, '1200' if order.side == 'SELL' else '450')
    result = broker.execute('dst', plan, data, executor, value)
    assert value.fx.quotes['usd'].lot_quote.quote_amount('BUY', 1).amount == 90
    assert D(1200 - 450) > 90
    assert [(item.uid, item.side) for item in result] == [('cny-rub', 'SELL'), ('R', 'BUY')]
    assert executor.submit_order.call_count == 2
