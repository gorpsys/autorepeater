"""Purchases follow absolute monetary reduction, with physical and scoped caps."""
from dataclasses import replace
from decimal import Decimal as D
from random import Random

from test.test_order_plan import destination, leaf, parent, rules

import pytest

from autorepeater.order_plan import build_order_plan


def test_equal_underweights_and_one_instrument():
    """equal underweights and one instrument."""
    result = build_order_plan(leaf(target={'A': D(50), 'B': D(50)}),
                              destination({'A': D(40), 'B': D(40)}, cash='20'),
                              {(): {'A': D(40), 'B': D(40)}},
                              {'A': D(1), 'B': D(1)}, rules('A', 'B'))
    assert [(item.uid, item.lots) for item in result.buys] == [('A', 10), ('B', 10)]


def test_fractional_owners_can_fund_one_lot():
    """fractional owners can fund one lot."""
    tree = parent((leaf((0,), {'X': D('7.5')}, budget='7.5'),
                   leaf((1,), {'X': D('2.5')}, budget='2.5')))
    result = build_order_plan(tree, destination(cash='10'), {(0,): {}, (1,): {}},
                              {'X': D(1)}, rules('X', lot=10))
    assert result.buys[0].pieces == {(0,): D('7.5'), (1,): D('2.5')}


def test_fractional_target_never_rounds_purchase_up():
    """fractional target never rounds purchase up."""
    result = build_order_plan(leaf(target={'X': D('19.9')}), destination(), {(): {}},
                              {'X': D(1)}, rules('X', lot=10))
    assert result.buys[0].lots == 1


def test_absolute_priority_not_largest_relative_error():
    """absolute priority not largest relative error."""
    result = build_order_plan(leaf(target={'A': D(1), 'B': D(100)}, budget='1000'),
                              destination(cash='10'), {(): {}}, {'A': D(10), 'B': D(1)},
                              rules('A', 'B'))
    assert [(item.uid, item.lots) for item in result.buys] == [('A', 1)]


def test_floor_only_once_and_cash_own_bound_intersection():
    """floor only once and cash own bound intersection."""
    result = build_order_plan(leaf(floor='10'), destination(cash='30'), {(): {}},
                              {'X': D(1)}, {'X': replace(rules('X')['X'], buy_money_amount=D(15))})
    assert result.buys[0].lots == 5


@pytest.mark.parametrize('change', [{'api_trade_available': False},
                                  {'bestprice_order_available': False}, {'buy_max_lots': 0},
                                  {'currency': 'usd'}])
def test_unavailable_lot_or_currency_defers(change):
    """unavailable lot or currency defers."""
    result = build_order_plan(leaf(), destination(), {(): {}}, {'X': D(1)},
                              {'X': replace(rules('X')['X'], **change)})
    assert not result.buys


def test_protected_aggregate_overweight_cannot_be_bought_again():
    """protected aggregate overweight cannot be bought again."""
    tree = parent((leaf((0,), {'X': D(1)}), leaf((1,), {'X': D(9)})))
    result = build_order_plan(tree, destination({'X': D(12)}),
                              {(0,): {'X': D(12)}, (1,): {}}, {'X': D(1)}, rules('X'))
    assert not result.buys


def test_scoped_budget_and_no_sale_estimate():
    """scoped budget and no sale estimate."""
    tree = parent((leaf((0,), {'A': D(10)}, budget='10', sell=True),
                   leaf((1,), {'B': D(10)}, budget='10')))
    result = build_order_plan(tree, destination({'A': D(20)}, cash='0'),
                              {(0,): {'A': D(20)}, (1,): {}},
                              {'A': D(1), 'B': D(1)}, rules('A', 'B'))
    assert result.sells and not result.buys


def test_large_quantity_is_batched():
    """large quantity is batched."""
    result = build_order_plan(leaf(target={'X': D('10000000')}, budget='10000000'),
                              destination(cash='10000000'), {(): {}}, {'X': D(1)}, rules('X'))
    assert result.buys[0].lots == 10000000


def test_spent_cash_does_not_hide_cheaper_remaining_candidate():
    """spent cash does not hide cheaper remaining candidate."""
    result = build_order_plan(leaf(target={'A': D(1), 'B': D(2), 'C': D(2)}),
                              destination(cash='11'), {(): {}},
                              {'A': D(7), 'B': D(5), 'C': D(2)}, rules('A', 'B', 'C'))
    assert [(item.uid, item.lots) for item in result.buys] == [('A', 1), ('C', 2)]


def test_nearly_one_lot_cash_cannot_round_up():
    """nearly one lot cash cannot round up."""
    result = build_order_plan(leaf(target={'X': D(1)}, budget='2'),
                              destination(cash='0.99999999999999999999999999999'),
                              {(): {}}, {'X': D(1)}, rules('X'))
    assert not result.buys


def test_unequal_prices_and_three_fractional_owners_conserve_lot_money():
    """unequal prices and three fractional owners conserve lot money."""
    tree = parent(tuple(leaf((index,), {'X': D(quantity)}, budget=budget)
                        for index, quantity, budget in [(0, '0.3', '0.9'),
                                                       (1, '0.2', '0.6'),
                                                       (2, '0.5', '1.5')]))
    result = build_order_plan(tree, destination(cash='3'),
                              {(0,): {}, (1,): {}, (2,): {}}, {'X': D(3)}, rules('X'))
    assert result.buys[0].pieces == {(0,): D('.3'), (1,): D('.2'), (2,): D('.5')}


def test_native_cash_absent_does_not_authorize_buy():
    """native cash absent does not authorize buy."""
    result = build_order_plan(leaf(), replace(destination(), available_cash={'usd': D(100)}),
                              {(): {}}, {'X': D(1)}, rules('X'))
    assert not result.buys


def test_small_unrelated_own_money_cap_does_not_hide_executable_lot():
    """small unrelated own money cap does not hide executable lot."""
    limits = rules('A', 'B')
    limits['A'] = replace(limits['A'], buy_money_amount=D(1))
    limits['B'] = replace(limits['B'], buy_money_amount=D(100))
    result = build_order_plan(leaf(target={'A': D(1), 'B': D(2)}), destination(cash='100'),
                              {(): {}}, {'A': D(2), 'B': D(20)}, limits)
    assert [(item.uid, item.lots) for item in result.buys] == [('B', 2)]


def test_each_own_money_bound_accounts_for_prior_purchases():
    """each own money bound accounts for prior purchases."""
    limits = rules('A', 'B')
    limits['A'] = replace(limits['A'], buy_money_amount=D(100))
    limits['B'] = replace(limits['B'], buy_money_amount=D(15))
    result = build_order_plan(leaf(target={'A': D(1), 'B': D(20)}), destination(cash='100'),
                              {(): {}}, {'A': D(10), 'B': D(1)}, limits)
    assert [(item.uid, item.lots) for item in result.buys] == [('A', 1), ('B', 5)]


def test_batched_plan_matches_independent_one_lot_oracle():  # pylint: disable=too-many-locals
    """Fixed marks make the winning priority constant until a cap is exhausted."""
    random = Random(601)
    for _ in range(100):
        uids = ('A', 'B', 'C', 'D')
        prices = {uid: D(random.randint(1, 10)) for uid in uids}
        targets = {uid: D(random.randint(1, 20)) for uid in uids}
        quantities = {uid: D(random.randint(0, 5)) for uid in uids}
        cash, floor = D(random.randint(0, 80)), D(random.randint(0, 5))
        lots = {uid: random.randint(1, 5) for uid in uids}
        cap = random.randint(0, 10)
        value = sum(quantities[uid] * prices[uid] for uid in uids)
        tree = leaf(target=targets, budget=str(value + cash + 10), floor=str(floor))
        result = build_order_plan(tree, destination(quantities, cash=str(cash)),
                                  {(): quantities}, prices,
                                  {uid: rules(uid, lot=lots[uid], buy=cap)[uid] for uid in uids})
        expected = {}
        remaining = max(D(0), cash - floor)
        current = dict(quantities)
        while True:
            candidates = [uid for uid in uids
                          if current[uid] + lots[uid] <= targets[uid]
                          and expected.get(uid, 0) < cap and prices[uid] * lots[uid] <= remaining]
            if not candidates:
                break
            chosen = min(candidates, key=lambda uid, marks=prices, sizes=lots:
                         (-marks[uid] * sizes[uid], uid))
            current[chosen] += lots[chosen]
            expected[chosen] = expected.get(chosen, 0) + 1
            remaining -= prices[chosen] * lots[chosen]
        assert {item.uid: item.lots for item in result.buys} == expected


def test_priority_uses_full_lot_value_with_different_lot_sizes():
    """B reduces monetary underweight by 20 despite a smaller per-piece price."""
    result = build_order_plan(leaf(target={'A': D(10), 'B': D(100)}, budget='300'),
                              destination(cash='20'), {(): {}}, {'A': D(10), 'B': D(2)},
                              {**rules('A'), **rules('B', lot=10)})
    assert [(item.uid, item.lots) for item in result.buys] == [('B', 1)]
