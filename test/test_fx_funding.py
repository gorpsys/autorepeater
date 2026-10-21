"""Pure native cash, conditional purchases and attributed direct FX funding."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from test.test_order_plan import destination, leaf, parent, rules

import pytest
from autorepeater import fx_funding
from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit
from autorepeater.cash_pools import (
    CashPools, CashProceeds, cash_headroom, credit_cash_pools, initial_cash_pools,
)
from autorepeater.fx import FxSnapshot
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.fx_funding import FundingInputs
from autorepeater.order_plan import build_order_plan
from autorepeater.strategy_plan import exact_product, exact_sum


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def market(lot=1):
    """Twelve RUB per CNY, ninety RUB per USD; explicit market units."""
    quotes = {}
    for currency, price in [('cny', '12'), ('usd', '90')]:
        pair = FxPair(currency + '-rub', currency, 'rub', D(1), lot, True, True, True)
        quotes[currency] = FxQuote(pair, D(price), D(price), NOW, NOW)
    return FxSnapshot(quotes, NOW)


def funding(tree=None, cash=None, own=None, prices=None, *, inputs=None, lot=1):
    # pylint: disable=too-many-arguments
    """Build through the existing order planner, without SDK/executor mutations."""
    tree = tree or leaf(target={'C': D(1)}, budget='1000', floor='100')
    cash = {'rub': D(1000)} if cash is None else cash
    own = rules('C', currency='cny', buy=0) if own is None else own
    snapshot = replace(destination(), available_cash=cash, fx_snapshot=market(lot))
    if inputs is None:
        inputs = FundingInputs(fx_rules=rules('cny-rub', 'usd-rub', lot=lot))
    prices = prices or {'C': D(720)}
    native = {uid: MoneyAmount(rule.currency, prices[uid] / snapshot.fx_snapshot.rate(
        rule.currency))
              for uid, rule in own.items() if uid in prices}
    inputs = replace(inputs, native_prices=native)
    ownership = {node.path: {} for node in terminals(tree)}
    return build_order_plan(tree, snapshot, ownership, prices, own,
                            funding=inputs)


def terminals(tree):
    """Leaf occurrences only; internal money is protected by the planner."""
    if not tree.children:
        return (tree,)
    return tuple(node for child in tree.children for node in terminals(child))


def test_zero_current_native_cap_retains_conditional_buy_and_actual_pools():
    """No CNY means GetMaxLots=0 today, not absence of a target after FX."""
    result = funding()
    assert not result.buys
    assert result.funding.spending_limits == {(): D(900)}
    assert result.funding.cash.balances[()] == {'rub': D(1000)}
    assert result.funding.demands[0].uid == 'C'
    purchase = result.funding.purchases[0]
    assert not purchase.executable_now
    assert purchase.fx_dependencies == ('cny-rub',)
    exchange = result.funding.exchanges[0]
    assert (exchange.uid, exchange.side, exchange.lots) == ('cny-rub', 'BUY', 65)
    assert exchange.base_contributions == {(): D(65)}
    assert exchange.debits[()]['rub'] == D(780)
    assert exchange.debit_limits[()]['rub'] == D(1000)


def test_foreign_reserve_keeps_half_in_rub_without_increasing_total():
    """Both the RUB FX fee and the native security fee use one existing reserve."""
    result = funding().funding
    assert result.reserve_shares[()]['rub'] == D(50)
    assert result.reserve_shares[()]['cny'] == D('4.166666666666666666666666666')
    assert result.projected_cash.amount((), 'rub') >= 50


def test_shared_fx_leaves_each_owner_room_for_observed_commission():
    """Live regression: one shared lot must not fill an owner's whole RUB budget."""
    tree = parent(tuple(leaf((i,), {'C': D(27)}, budget='400000', floor='60000')
                        for i in range(2)))
    tree = replace(tree, budget=D(1000000), cash_floor=D(320000))
    pair = FxPair('cny-rub', 'cny', 'rub', D(1), 1000, True, True, True)
    fx = FxSnapshot({'cny': FxQuote(pair, D('12.2695'), D('12.2695'), NOW, NOW)}, NOW)
    native = D('1023.887')
    snapshot = replace(destination(), budget=tree.budget,
                       available_cash={'rub': tree.budget}, fx_snapshot=fx)
    plan = build_order_plan(
        tree, snapshot, {(0,): {}, (1,): {}}, {'C': native * fx.rate('cny')},
        rules('C', currency='cny', buy=0), funding=FundingInputs(
            native_prices={'C': MoneyAmount('cny', native)},
            fx_rules=rules('cny-rub', lot=1000)))
    exchange, = plan.funding.exchanges
    assert plan.funding.cash.protected == {'rub': D(200000)}
    for path, money in exchange.debits.items():
        principal = money['rub']
        assert principal + principal * D('.0005') <= exchange.debit_limits[path]['rub']
        assert plan.funding.projected_cash.amount(path, 'rub') >= D(30000)


def test_exact_fx_capacity_correction_preserves_other_owners(monkeypatch):
    """A rounded capacity correction cannot replace the other owners' principal pools."""
    # pylint: disable=protected-access
    cash = CashPools({(0,): {'rub': D(12)}, (1,): {'rub': D(12)}})
    principal = CashPools({(0,): {'cny': D(1)}, (1,): {'cny': D(1)}})
    context = fx_funding._Context(
        market(), FundingInputs(fx_rules=rules('cny-rub')), cash,
        CashPools({(0,): {}, (1,): {}}), {}, {}, {}, CashPools({}))
    monkeypatch.setattr(fx_funding, '_base_affordable',
                        lambda *_args, **_kwargs: D(1).next_minus())
    balances = {path: dict(money) for path, money in cash.balances.items()}
    exchanges = []
    assert fx_funding._forward(context, principal, principal, (), balances, exchanges)
    assert len(exchanges) == 1
    exchange = exchanges[0]
    assert exchange.base_contributions == {(0,): D(1), (1,): D(1)}
    assert balances == {(0,): {'rub': D(0), 'cny': D(1)},
                        (1,): {'rub': D(0), 'cny': D(1)}}


@pytest.mark.parametrize('amount', ['60.5', '69', '80'])
def test_existing_currency_including_lot_surplus_pays_fees_without_restock(amount):
    """Enough principal+fee needs no FX even below the nominal reserve share."""
    inputs = FundingInputs(fees={'C': {'cny': D('.5')}})
    result = funding(cash={'cny': D(amount)}, own=rules('C', currency='cny'), inputs=inputs).funding
    assert not result.exchanges
    assert result.purchases[0].executable_now
    assert result.security_debits.amount((), 'cny') == D('60.5')
    assert result.projected_cash.amount((), 'cny') == D(amount) - D('60.5')


@pytest.mark.parametrize('existing,lots', [('0', 7), ('8.5', 6), ('60', 1)])
def test_partial_balance_subtracted_before_rounding_with_leaf_buffer(existing, lots):
    """Only a real purchase fee deficit triggers the last small exchange."""
    inputs = FundingInputs(fx_rules=rules('cny-rub', lot=10), fees={'C': {'cny': D(1)}})
    result = funding(cash={'rub': D(1000), 'cny': D(existing)}, inputs=inputs, lot=10).funding
    assert result.exchanges[0].lots == lots
    assert result.exchanges[0].base_contributions == {(): D(lots * 10)}


def test_large_fx_lot_cannot_break_owner_cash_ceiling():
    """Even one target lot is deferred when the smallest FX lot costs too much."""
    result = funding(lot=1000).funding
    assert result.demands and not result.purchases and not result.exchanges
    assert result.projected_cash == result.cash


def test_reserve_once_per_owner_currency_for_two_securities():
    """Two buys use one 100-RUB reserve share, one exchange and its lot surplus."""
    tree = leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100')
    result = funding(tree, own=rules('A', 'B', currency='cny', buy=0),
                     prices={'A': D(360), 'B': D(360)}, lot=10).funding
    assert len(result.purchases) == 2
    assert len(result.exchanges) == 1
    assert result.exchanges[0].lots == 7
    assert result.exchanges[0].dependent_uids == ('A', 'B')
    assert result.projected_cash.amount((), 'cny') == D(10)


def test_mixed_leaf_reserve_split_by_target_rub_value():
    """RUB keeps50 plus half the foreign50; the other25 remains a CNY allowance."""
    tree = leaf(target={'R': D(1), 'C': D(1)}, budget='1000', floor='100')
    result = funding(tree, own={**rules('R'), **rules('C', currency='cny', buy=0)},
                     prices={'R': D(450), 'C': D(450)}).funding
    assert result.reserve_shares[()]['rub'] == D(75)
    assert result.reserve_shares[()]['cny'] == D('2.083333333333333333333333333')
    assert len(result.exchanges) == 1
    assert result.exchanges[0].lots == 40
    assert [buy.intent.uid for buy in result.purchases] == ['C', 'R']


def test_all_reserve_may_remain_in_cny_and_fee_may_reduce_it():
    """Final projected reserve90 RUB is valid after a10 RUB fee, no reverse FX."""
    tree = leaf(target={'C': D(1)}, budget='1000', floor='100')
    result = funding(tree, cash={'cny': D('83.33333333333333333333333333')},
                     own=rules('C', currency='cny'), prices={'C': D(900)},
                     inputs=FundingInputs(fees={'C': {'cny': D('0.833333333333333333333333333')}}))
    assert not result.funding.exchanges
    assert result.funding.purchases
    assert result.funding.projected_cash.amount((), 'rub') == 0
    assert result.funding.projected_cash.amount((), 'cny') * 12 < D(100)


def test_rub_purchase_can_sell_only_necessary_native_cash():
    """Reverse FX funds a real RUB security; lot excess remains money."""
    tree = leaf(target={'R': D(1)}, budget='1200', floor='100')
    result = funding(tree, cash={'cny': D(100)}, own=rules('R', buy=0),
                     prices={'R': D(450)}, lot=10).funding
    exchange = result.exchanges[0]
    assert (exchange.side, exchange.lots) == ('SELL', 4)
    assert exchange.debits == {(): {'cny': D(40)}}
    assert exchange.credits == {(): {'rub': D(480)}}
    assert result.projected_cash.amount((), 'rub') == D(30)
    assert result.projected_cash.amount((), 'cny') == D(60)
    assert not result.purchases[0].executable_now


def test_no_reverse_exchange_without_rub_security_or_for_rub_fee_alone():
    """A RUB fee on a CNY paper cannot authorize selling CNY into RUB."""
    inputs = FundingInputs(fx_rules=rules('cny-rub'), fees={'C': {'rub': D(5)}})
    result = funding(cash={'cny': D(80)}, own=rules('C', currency='cny'), inputs=inputs).funding
    assert not result.purchases and not result.exchanges


def test_no_cny_rub_usd_bridge_or_spending_reverse_lot_surplus_on_fx():
    """A legitimate RUB buy does not grant the leftover30 RUB to a USD route."""
    tree = leaf(target={'R': D(1), 'U': D(1)}, budget='1200', floor='100')
    result = funding(tree, cash={'cny': D(100)},
                     own={**rules('R', buy=0), **rules('U', currency='usd', buy=0)},
                     prices={'R': D(450), 'U': D(9)}, lot=10).funding
    assert [buy.intent.uid for buy in result.purchases] == ['R']
    assert [(item.uid, item.side) for item in result.exchanges] == [('cny-rub', 'SELL')]
    assert result.projected_cash.amount((), 'rub') == D(30)


def test_native_demand_nets_before_reverse_exchange():
    """Known CNY buying demand protects its funding before a higher-priority RUB buy."""
    tree = leaf(target={'R': D(1), 'C': D(1)}, budget='1200', floor='100')
    result = funding(tree, cash={'cny': D(50)},
                     own={**rules('R', buy=0), **rules('C', currency='cny')},
                     prices={'R': D(450), 'C': D(360)}, lot=10).funding
    assert [buy.intent.uid for buy in result.purchases] == ['C']
    assert not result.exchanges


def test_absolute_monetary_priority_survives_mixed_currency_funding():
    """One900 RUB purchase wins over the much larger relative gap of ten12 RUB lots."""
    tree = leaf(target={'R': D(1), 'C': D(10)}, budget='1200', floor='100')
    result = funding(tree, cash={'rub': D(1000)},
                     own={**rules('R'), **rules('C', currency='cny', buy=0)},
                     prices={'R': D(900), 'C': D(12)}).funding
    assert result.purchases[0].intent.uid == 'R'
    assert result.purchases[0].intent.lots == 1


@pytest.mark.parametrize('nested', [False, True])
def test_composite_unallocated_cash_excluded_from_leaf_reserve_and_fx(nested):
    """2000 parent/1000 leaf: the CNY buffer is100, never the parent floor1100."""
    path = (0, 0) if nested else (0,)
    child = leaf(path, {'C': D(1)}, budget='1000', floor='100')
    if nested:
        inner = replace(parent((replace(child, path=(0,)),)), path=(0,), children=(child,),
                        budget=D(1500), cash_floor=D(600))
        tree = replace(parent((inner,)), budget=D(2000), cash_floor=D(1100))
    else:
        tree = replace(parent((child,)), budget=D(2000), cash_floor=D(1100))
    result = funding(tree, cash={'rub': D(1880), 'cny': D(10)},
                     inputs=FundingInputs(fx_rules=rules('cny-rub', lot=10),
                                          fees={'C': {'cny': D(1)}}), lot=10).funding
    assert result.reserve_shares[path]['rub'] == D(50)
    assert result.reserve_shares[path]['cny'] == D('4.166666666666666666666666666')
    assert result.exchanges[0].lots == 6
    assert result.cash.protected['rub'] == D(1000)
    assert result.projected_cash.protected == result.cash.protected
    assert result.security_debits.amount(path, 'cny') == D(61)


def test_two_buy_only_siblings_same_uid_merge_with_exact_owner_contributions():
    """Two half-paper owners can fund one security lot and one shared FX lot."""
    tree = parent((leaf((0,), {'C': D('.5')}, budget='500', floor='50'),
                   leaf((1,), {'C': D('.5')}, budget='500', floor='50')))
    result = funding(tree, own=rules('C', currency='cny', buy=0), lot=10).funding
    assert result.purchases[0].intent.pieces == {(0,): D('.5'), (1,): D('.5')}
    exchange = result.exchanges[0]
    assert exchange.base_contributions == {(0,): D(35), (1,): D(35)}
    assert sum(values['rub'] for values in exchange.debits.values()) == D(840)
    assert sum(exchange.base_contributions.values()) == D(exchange.lots * exchange.lot_size)


def test_owner_cash_cannot_finance_another_buy_only_sibling():
    """Global1000 RUB is insufficient authority when the target owner has zero."""
    tree = parent((leaf((0,), {'C': D(1)}, budget='500', floor='50'),
                   leaf((1,), {'R': D(1)}, budget='500', floor='50')))
    inputs = FundingInputs(cash=CashPools({(0,): {}, (1,): {'rub': D(500)}}, {'rub': D(500)}),
                           fx_rules=rules('cny-rub'))
    result = funding(tree, own={**rules('C', currency='cny', buy=0), **rules('R')},
                     prices={'C': D(360), 'R': D(450)}, inputs=inputs).funding
    assert [item.intent.uid for item in result.purchases] == ['R']
    assert not result.exchanges


@pytest.mark.parametrize('changes', [{'api_trade_available': False},
                                     {'bestprice_order_available': False}, {'buy_max_lots': 0},
                                     {'buy_money_amount': D(779)}])
def test_fx_own_permissions_and_caps_still_constrain_funding(changes):
    """No future own FX cap is fabricated to finance a security."""
    fx_rules = {'cny-rub': replace(rules('cny-rub')['cny-rub'], **changes)}
    result = funding(inputs=FundingInputs(fx_rules=fx_rules)).funding
    assert result.demands and not result.purchases and not result.exchanges


def test_fx_fee_and_security_fee_are_estimates_paid_by_original_owner():
    """Per-lot exchange fees and security fees debit only their attributed path."""
    result = funding(inputs=FundingInputs(fx_rules=rules('cny-rub'),
                                          fees={'C': {'cny': D(1)}},
                                          fx_fees={'cny-rub': {'rub': D('.1')}})).funding
    assert result.exchanges[0].debits[()]['rub'] == D('786.5')
    assert result.security_debits.amount((), 'cny') == D(61)
    assert result.projected_cash.amount((), 'rub') == D('213.5')
    assert result.exchanges[0].estimated_fees == {(): {'rub': D('6.5')}}
    assert result.estimated_security_fees.amount((), 'cny') == D(1)


def test_pools_snapshot_and_funding_inputs_copy_and_freeze_native_money():
    """The caller cannot mutate a plan's native balance observations."""
    original = {(): {'cny': D(80)}}
    cash = CashPools(original)
    original[()]['cny'] = D(0)
    assert cash.amount((), 'cny') == D(80)
    with pytest.raises(TypeError):
        cash.balances[()]['cny'] = D(1)
    with pytest.raises(ValueError):
        CashPools({(): {'cny': D(-1)}})


@pytest.mark.parametrize('shared,known,expected', [
    (False, False, '0'), (False, True, '0'), (True, False, '5'), (True, True, '5')])
def test_proceeds_keep_shared_and_scoped_permissions(shared, known, expected):
    """Unknown isolated proceeds stay deferred; shared availability is only an upper bound."""
    tree = parent((leaf((0,), {'C': D(1)}, budget='120'),
                   leaf((1,), {'C': D(1)}, budget='120')), redistribute=shared)
    initial = CashPools({(0,): {}, (1,): {}})
    credit = CashProceeds({(0,): D(1)}, {'cny': D(10)} if known else None)
    result = credit_cash_pools(tree, initial, {'cny': D(10)}, {(0,): D(120), (1,): D(120)},
                               market(), [credit])
    assert result.amount((1,), 'cny') == D(expected)
    assert credit.cash == ({'cny': D(10)} if known else None)
    assert result.total('cny') <= D(10)


def test_reverse_fx_lot_rounding_after_combining_exact_owner_needs():
    """Two150 RUB deficits need three120 RUB lots, not two lots per owner."""
    tree = parent((leaf((0,), {'R': D('.5')}, budget='300'),
                   leaf((1,), {'R': D('.5')}, budget='300')))
    result = funding(tree, cash={'cny': D(50)}, own=rules('R', buy=0),
                     prices={'R': D(300)}, lot=10).funding
    exchange = result.exchanges[0]
    assert exchange.lots == 3
    assert exchange.base_contributions == {(0,): D(15), (1,): D(15)}
    assert exchange.credits == {(0,): {'rub': D(180)}, (1,): {'rub': D(180)}}


def test_reverse_fx_fee_uses_net_proceeds_to_size_exchange():
    """Two240 RUB gross lots with twenty RUB fees each cannot pay a450 RUB paper."""
    tree = leaf(target={'R': D(1)}, budget='1200')
    result = funding(tree, cash={'cny': D(100)}, own=rules('R', buy=0),
                     prices={'R': D(450)}, lot=20,
                     inputs=FundingInputs(fx_rules=rules('cny-rub', lot=20),
                                          fx_fees={'cny-rub': {'rub': D(20)}})).funding
    assert result.exchanges[0].lots == 3
    assert result.projected_cash.amount((), 'rub') == D(210)


def test_base_currency_fx_fee_comes_from_credit_not_an_extra_foreign_bridge():
    """Reserve covers CNY execution fees as native money, with no second FX route."""
    inputs = FundingInputs(fx_rules=rules('cny-rub'), fx_fees={'cny-rub': {'cny': D('.1')}})
    result = funding(inputs=inputs).funding
    assert len(result.exchanges) == 1
    assert result.purchases
    assert result.projected_cash.amount((), 'cny') >= 0


def test_two_pure_buys_pay_fee_from_reserve_without_repeated_floor():
    """1000/100 reserve, two450 purchases with fee5 leave90 RUB."""
    tree = leaf(target={'A': D(1), 'B': D(1)}, budget='1000', floor='100')
    result = funding(tree, own=rules('A', 'B'), prices={'A': D(450), 'B': D(450)},
                     inputs=FundingInputs(fees={'A': {'rub': D(5)}, 'B': {'rub': D(5)}})).funding
    assert [buy.intent.uid for buy in result.purchases] == ['A', 'B']
    assert result.projected_cash.amount((), 'rub') == D(90)
    assert not result.exchanges


@pytest.mark.parametrize('changes', [{'limits_ready': False}, {'active_orders': ('active',)}])
def test_funding_readiness_blocks_all_projected_effects_but_retains_demand(changes):
    """Currency intentions cannot bypass the existing account readiness gate."""
    snapshot = replace(destination(), fx_snapshot=market(), **changes)
    result = build_order_plan(leaf(), snapshot, {(): {}}, {'X': D(1)}, rules('X'),
                              funding=FundingInputs()).funding
    assert result.demands and not result.purchases and not result.exchanges


def test_native_prices_cannot_be_guessed_from_broker_rub_marks():
    """Explicit native money/FX consistency is required at the funding boundary."""
    tree = leaf(target={'C': D(1)}, budget='1000')
    snapshot = replace(destination(), fx_snapshot=market())
    own = rules('C', currency='cny')
    with pytest.raises(ValueError, match='native security'):
        build_order_plan(tree, snapshot, {(): {}}, {'C': D(720)}, own, funding=FundingInputs())
    with pytest.raises(ValueError, match='disagree'):
        build_order_plan(tree, snapshot, {(): {}}, {'C': D(720)}, own,
                         funding=FundingInputs(native_prices={'C': MoneyAmount('cny', D(61))}))
    with pytest.raises(ValueError, match='snapshot'):
        build_order_plan(tree, destination(), {(): {}}, {'C': D(720)}, own,
                         funding=FundingInputs())


def test_native_caps_and_exact_actual_cash_still_limit_immediate_purchases():
    """A native target is not automatically executable because FX rates exist."""
    result = funding(cash={'cny': D(60)}, own=rules('C', currency='cny', buy=0)).funding
    assert result.demands and not result.purchases and not result.exchanges
    own = {'C': replace(rules('C', currency='cny')['C'], buy_money_amount=D(59))}
    assert not funding(cash={'cny': D(60)}, own=own).funding.purchases


def test_protected_and_unassigned_cash_is_not_a_leaf_commission_pool():
    """Actual balances conserve all native currencies while excess capital is protected."""
    tree = replace(parent((leaf((0,), {'C': D(1)}, budget='1000', floor='100'),)),
                   budget=D(2000), cash_floor=D(1100))
    cash = initial_cash_pools(tree, {(0,): {}}, {'C': D(720)},
                              {'rub': D(1880), 'cny': D(10)}, market())
    assert cash_headroom(tree, {(0,): {}}, {'C': D(720)}) == {(0,): D(1000)}
    assert cash.total('cny') == D(10) and cash.total('rub') == D(1880)
    assert cash.protected['rub'] == D(1000)


@pytest.mark.parametrize('cash,match', [
    (CashPools({(1,): {'rub': D(1)}}), 'leaf'),
    (CashPools({(): {'rub': D(1001)}}), 'actual')])
def test_invalid_money_ownership_never_enters_funding(cash, match):
    """Neither fabricated balances nor arbitrary paths authorize trading."""
    with pytest.raises(ValueError, match=match):
        funding(inputs=FundingInputs(cash=cash))


def test_incomplete_or_malformed_proceeds_never_become_exact_cash():
    """Fresh money does not identify an isolated seller; malformed attribution fails."""
    tree = leaf()
    with pytest.raises(ValueError, match='ownership'):
        CashProceeds({}, None)
    with pytest.raises(ValueError, match='owner'):
        credit_cash_pools(tree, CashPools({(): {}}), {'rub': D(10)}, {(): D(100)},
                          market(), [CashProceeds({(1,): D(1)}, {'rub': D(10)})])
    with pytest.raises(ValueError, match='debit'):
        credit_cash_pools(tree, CashPools({(): {'rub': D(11)}}), {'rub': D(10)},
                          {(): D(100)}, market(), [])
    with pytest.raises(ValueError, match='duplicate'):
        CashPools({(): {'rub': D(1), 'RUB': D(1)}})


def test_same_uid_funded_sibling_not_hidden_by_unfunded_earlier_path():
    """Path tie-breaks cannot consume a lot's ownership from a penniless sibling."""
    tree = parent((leaf((0,), {'C': D(1)}, budget='500'),
                   leaf((1,), {'C': D(1)}, budget='500')))
    cash = CashPools({(0,): {}, (1,): {'cny': D(40)}})
    result = funding(tree, cash={'cny': D(40)}, own=rules('C', currency='cny'),
                     prices={'C': D(360)}, inputs=FundingInputs(cash=cash)).funding
    assert len(result.purchases) == 1
    assert result.purchases[0].intent.pieces == {(1,): D(1)}


def test_fx_lot_cannot_spend_beyond_assigned_owner_budget_even_if_account_has_cash():
    """An over-attributed starting pool cannot smuggle unallocated cash into FX."""
    cash = CashPools({(): {'rub': D(1200)}})
    snapshot = replace(destination(cash='1200'), fx_snapshot=market(100))
    inputs = FundingInputs(cash=cash, native_prices={'C': MoneyAmount('cny', D(10))},
                           fx_rules=rules('cny-rub', lot=100))
    with pytest.raises(ValueError, match='assigned budget'):
        build_order_plan(leaf(target={'C': D(1)}, budget='1000', floor='100'),
                         snapshot, {(): {}}, {'C': D(120)}, rules('C', currency='cny', buy=0),
                         funding=inputs)


def test_native_reserve_shares_never_exceed_existing_total_rub_reserve():
    """Recurring Decimal division is directed down at allowance boundaries, no epsilon."""
    tree = leaf(target={'R': D(1), 'C': D(1)}, budget='1000', floor='100')
    result = funding(tree, own={**rules('R'), **rules('C', currency='cny', buy=0)},
                     prices={'R': D(450), 'C': D(450)}).funding
    shares = result.reserve_shares[()]
    assert exact_sum((shares['rub'], exact_product(shares['cny'], D(12)))) <= D(100)


def test_initial_native_cash_matches_sibling_needs_before_assigning_protected_surplus():
    """Existing USD funds its USD owner before another currency occupies that allowance."""
    tree = replace(parent((leaf((0,), {'C': D(1)}, budget='500', floor='50'),
                           leaf((1,), {'U': D(1)}, budget='500', floor='50'))),
                   budget=D(2000), cash_floor=D(1100))
    result = funding(tree, cash={'cny': D(90), 'usd': D(10)},
                     own={**rules('C', currency='cny'), **rules('U', currency='usd')},
                     prices={'C': D(360), 'U': D(360)}).funding
    assert {item.intent.uid for item in result.purchases} == {'C', 'U'}
    assert not result.exchanges
    assert result.cash.amount((1,), 'usd') >= D(4)


@pytest.mark.parametrize('price', [MoneyAmount('cny', D(-1)), '60'])
def test_malformed_native_midpoint_is_rejected_before_funding(price):
    """A guessed or signed trade price cannot cross the pure planning boundary."""
    with pytest.raises(ValueError, match='midpoint'):
        FundingInputs(native_prices={'C': price})


def test_missing_settlement_currency_cannot_reassign_the_leaf_reserve():
    """Missing one paper's currency must not give its reserve share to another paper."""
    tree = leaf(target={'C': D(1), 'R': D(1)}, budget='1000', floor='100')
    with pytest.raises(ValueError, match='settlement currency'):
        funding(tree, prices={'C': D(360), 'R': D(450)})


@pytest.mark.parametrize('side,fee', [('BUY', {'cny': D(1)}), ('SELL', {'rub': D(12)})])
def test_fee_exhausting_fx_lot_credit_cannot_finance_a_security(side, fee):
    """Zero net FX lot proceeds do not create a financing route."""
    tree = leaf(target={'R': D(1)} if side == 'SELL' else {'C': D(1)}, budget='1000')
    inputs = FundingInputs(fx_rules=rules('cny-rub'), fx_fees={'cny-rub': fee})
    own = rules('R', buy=0) if side == 'SELL' else rules('C', currency='cny', buy=0)
    cash = {'cny': D(50)} if side == 'SELL' else {'rub': D(1000)}
    prices = {'R': D(450)} if side == 'SELL' else {'C': D(720)}
    result = funding(tree, cash, own, prices, inputs=inputs).funding
    assert not result.purchases and not result.exchanges


def test_fx_fee_also_respects_current_own_money_cap():
    """A780 RUB quote plus6.5 fee exceeds an own cap of785, with enough account cash."""
    own = {'cny-rub': replace(rules('cny-rub')['cny-rub'], buy_money_amount=D(785))}
    inputs = FundingInputs(fx_rules=own, fx_fees={'cny-rub': {'rub': D('.1')}})
    assert not funding(inputs=inputs).funding.purchases


def test_minimum_fx_lot_cannot_spend_rub_earmarked_for_another_paper():
    """A600 RUB FX lot plus450 RUB security cannot be financed by1000 RUB."""
    tree = leaf(target={'C': D(1), 'R': D(1)}, budget='1000', floor='100')
    result = funding(tree, own={**rules('R'), **rules('C', currency='cny', buy=0)},
                     prices={'C': D(450), 'R': D(450)}, lot=50).funding
    assert [buy.intent.uid for buy in result.purchases] == ['C']
    assert result.projected_cash.amount((), 'rub') == D(400)


@pytest.mark.parametrize('fees,fx_fees', [
    ({'C': {'usd': D(1)}}, {}), ({}, {'cny-rub': {'usd': D(1)}})])
def test_foreign_fee_cannot_create_another_foreign_exchange(fees, fx_fees):
    """CNY paper/FX fees cannot authorize an unrelated USD exchange."""
    result = funding(inputs=FundingInputs(fx_rules=rules('cny-rub', 'usd-rub'),
                                          fees=fees, fx_fees=fx_fees)).funding
    assert not result.purchases and not result.exchanges


def test_closed_reverse_fx_or_insufficient_own_sell_cap_defers_rub_buy():
    """Reverse FX uses the actual own sell cap, even though it sells no security."""
    tree = leaf(target={'R': D(1)}, budget='1200')
    result = funding(tree, cash={'cny': D(100)}, own=rules('R', buy=0),
                     prices={'R': D(450)},
                     inputs=FundingInputs(fx_rules=rules('cny-rub', sell=0))).funding
    assert result.demands and not result.purchases


def test_bond_native_midpoint_with_aci_is_consumed_once():
    """Clean58 CNY plus2 CNY ACI yields60 CNY funding, never62 or another FX factor."""
    quote = SecurityQuote('cny', D(58), D(58), SecurityQuoteUnit.PERCENT_NOMINAL,
                          MoneyAmount('cny', D(100)), MoneyAmount('cny', D(2)))
    native = quote.estimate_per_piece()
    snapshot = replace(destination(), available_cash={'cny': D(61)}, fx_snapshot=market())
    result = build_order_plan(leaf(target={'C': D(1)}, budget='1000', floor='100'), snapshot,
                              {(): {}}, {'C': D(720)}, rules('C', currency='cny'),
                              funding=FundingInputs(native_prices={'C': native})).funding
    assert result.security_debits.amount((), 'cny') == D(60)
    assert result.projected_cash.amount((), 'cny') == D(1)
    assert not result.exchanges


def test_fully_spent_ceiling_cannot_expose_a_second_demand_as_an_order():
    """Hypothetical demands may coexist; accepted purchases share one actual budget."""
    tree = leaf(target={'R': D(1), 'S': D(1)}, budget='900')
    result = funding(tree, cash={'rub': D(900)}, own=rules('R', 'S', 'unused'),
                     prices={'R': D(900), 'S': D(720)}).funding
    assert len(result.demands) == 2
    assert [buy.intent.uid for buy in result.purchases] == ['R']


@pytest.mark.parametrize('known', [False, True])
def test_nested_buy_only_barrier_keeps_sale_currency_inside_its_owner(known):
    """A redistributing root cannot override a nested BUY_ONLY transfer barrier."""
    nested_leaf = leaf((0, 0), {'C': D(1)}, budget='120')
    nested = replace(parent((replace(nested_leaf, path=(0,)),)), path=(0,),
                     children=(nested_leaf,))
    tree = parent((nested, leaf((1,), {'C': D(1)}, budget='120')), redistribute=True)
    initial = CashPools({(0, 0): {}, (1,): {}})
    proceeds = CashProceeds({(0, 0): D(1)}, {'cny': D(10)} if known else None)
    result = credit_cash_pools(tree, initial, {'cny': D(10)},
                               {(0, 0): D(120), (1,): D(120)}, market(), [proceeds])
    assert result.amount((1,), 'cny') == 0
    assert result.amount((0, 0), 'cny') == (D(10) if known else D(0))


def test_exact_proceeds_do_not_expand_a_zero_remaining_owner_budget():
    """Confirmed money still cannot cross the owner's spending ceiling."""
    result = credit_cash_pools(leaf(), CashPools({(): {}}), {'rub': D(10)},
                               {(): D(0)}, market(), [CashProceeds({(): D(1)}, {'rub': D(10)})])
    assert not result.total('rub')


def test_combined_fx_lot_excess_goes_only_to_owners_with_source_capacity():
    """A460 RUB owner cannot fund a proportional share above460 to complete one FX lot."""
    tree = parent((leaf((0,), {'C': D(1)}, budget='460', floor='100'),
                   leaf((1,), {'C': D(1)}, budget='540')))
    cash = CashPools({(0,): {'rub': D(460)}, (1,): {'rub': D(540)}})
    result = funding(tree, own=rules('C', currency='cny', buy=0), prices={'C': D(360)}, lot=10,
                     inputs=FundingInputs(cash=cash, fx_rules=rules('cny-rub', lot=10))).funding
    exchange = result.exchanges[0]
    assert exchange.lots == 7
    assert exact_sum(exchange.base_contributions.values()) == D(70)
    assert exchange.debits[(0,)]['rub'] <= D(460)
    assert exchange.debits[(1,)]['rub'] <= D(540)


def test_zero_target_does_not_take_a_currency_reserve_share():
    """A retained zero UID needs no funding and cannot dilute the positive CNY target."""
    tree = leaf(target={'X': D(0), 'C': D(1)}, budget='1000', floor='100')
    snapshot = replace(destination(cash='1000'), fx_snapshot=market())
    result = build_order_plan(tree, snapshot, {(): {}}, {'X': D(0), 'C': D(720)},
                              {**rules('X'), **rules('C', currency='cny', buy=0)},
                              funding=FundingInputs(native_prices={'C': MoneyAmount('cny', D(60))},
                                                    fx_rules=rules('cny-rub'))).funding
    assert result.reserve_shares[()] == {
        'rub': D(50), 'cny': D('4.166666666666666666666666666')}
    assert [item.intent.uid for item in result.purchases] == ['C']


def test_partial_conditional_batch_crosses_current_native_cap_without_guessing_future_cap():
    """With current cap0,15 CNY+60 RUB may conditionally fund20 CNY, not100."""
    tree = leaf(target={'C': D(100)}, budget='1200')
    result = funding(tree, cash={'cny': D(15), 'rub': D(60)},
                     own=rules('C', currency='cny', buy=0), prices={'C': D(12)}).funding
    assert result.purchases[0].intent.lots == 20
    assert not result.purchases[0].executable_now
    assert result.exchanges[0].lots == 5
