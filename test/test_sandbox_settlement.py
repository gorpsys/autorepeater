"""Observed CNY bond settlement changes the oracle, never raw broker money."""
from dataclasses import asdict, replace
from decimal import Decimal as D

import pytest

from scripts.sandbox_lifecycle import SandboxFailure
from scripts.sandbox_multicurrency import (
    NativeState, TradeFact, money_components, money_deltas, reconcile,
)


def bond(side, lots, principal, fee, **changes):
    """Identify a checked CNY bond order without changing its raw money."""
    return replace(TradeFact('bond', side, lots, 1, 'cny', D(principal), D(fee), None,
                             cny_bond=True), **changes)


@pytest.mark.parametrize('side,initial,fee,raw_expected,actual', [
    ('SELL', '10000', '33.632023500', '11815.360294500', '11815.360295000'),
    ('BUY', '80000', '33.690715500', '11312.548284500', '11312.548285000'),
    ('SELL', '11312.615318', '33.630884500', '79813.083433500', '79813.083434000'),
])
def test_recorded_odd_and_even_half_micro_ties(side, initial, fee, raw_expected, actual):
    """Principal isolates each recorded cash tie; the first fixture is not a fill replay."""
    initial, fee, raw_expected, actual = map(D, (initial, fee, raw_expected, actual))
    principal = (raw_expected + fee - initial) * (1 if side == 'SELL' else -1)
    trade = bond(side, 67, principal, fee)
    original = asdict(trade)
    before = NativeState({'bond': D(67)} if side == 'SELL' else {}, {'cny': initial}, {})
    after = NativeState({} if side == 'SELL' else {'bond': D(67)}, {'cny': actual}, {})
    reconcile(before, after, [trade])
    facts = money_deltas(before, after, [trade])['cny']
    assert facts['raw_expected'] == facts['expected'] == raw_expected
    assert facts['raw_residual'] == facts['residual'] == D('.000000500')
    assert facts['settlement_expected'] == actual
    assert facts['rounding_adjustment'] == D('.000000500')
    assert facts['settlement_residual'] == 0
    assert asdict(trade) == original


def test_sell_pair_rounds_each_order_before_aggregation():
    """Two equal facts are two orders and each carries its own cash adjustment."""
    before = NativeState({'bond': D(2)}, {'cny': D('2949.630308')}, {})
    one = bond('SELL', 1, '1022.791', '.501900500')
    two = bond('SELL', 2, '2045.582', '1.003801')
    first = NativeState({'bond': D(1)}, {'cny': D('3971.919408')}, {})
    split = NativeState({}, {'cny': D('4994.208508')}, {})
    combined = NativeState({}, {'cny': D('4994.208507')}, {})
    reconcile(before, first, [one])
    reconcile(first, split, [one])
    reconcile(before, split, [one, one])
    reconcile(before, combined, [two])
    assert split.money['cny'] - combined.money['cny'] == D('.000001')
    with pytest.raises(SandboxFailure):
        reconcile(before, combined, [one, one])
    terms = money_components(before, [one, one])['cny']
    assert terms['raw_expected'] == D('4994.208507')
    assert terms['settlement_expected'] == D('4994.208508')
    assert terms['rounding_adjustment'] == D('.000001')
    assert [step['sequence'] for step in terms['operations']] == [1, 2]
    assert [step['unrounded_settlement_expected'] for step in terms['operations']] == [
        D('3971.9194075'), D('4994.2085075')]
    assert [step['settlement_expected'] for step in terms['operations']] == [
        D('3971.919408'), D('4994.208508')]


@pytest.mark.parametrize('cash', ['4994.208507', '4994.208509'])
def test_split_pair_rejects_a_micro_in_either_direction(cash):
    """Neither final-only rounding nor a widened tolerance can match the pair."""
    before = NativeState({'bond': D(2)}, {'cny': D('2949.630308')}, {})
    trade = bond('SELL', 1, '1022.791', '.501900500')
    with pytest.raises(SandboxFailure):
        reconcile(before, NativeState({}, {'cny': D(cash)}, {}), [trade, trade])


def test_combined_two_lot_order_does_not_round_per_lot():
    """A single fee and a single settlement apply to the combined order."""
    before = NativeState({'bond': D(2)}, {'cny': D('2949.630308')}, {})
    with pytest.raises(SandboxFailure):
        reconcile(before, NativeState({}, {'cny': D('4994.208508')}, {}),
                  [bond('SELL', 2, '2045.582', '1.003801')])


def test_later_sale_cannot_cover_negative_cash_before_rounding():
    """The first raw debit must be affordable before any later sale or quantization."""
    before = NativeState({}, {'cny': D(1)}, {})
    after = NativeState({}, {'cny': D(1)}, {})
    with pytest.raises(SandboxFailure, match='exceeds owned cash'):
        reconcile(before, after, [bond('BUY', 1, '1', '.000000500'),
                                  bond('SELL', 1, '1', '0')])


@pytest.mark.parametrize('fx', [False, True])
def test_rub_and_fx_keep_all_native_nanos(fx):
    """The CNY bond exception cannot quantize native RUB or an FX base balance."""
    if fx:
        before = NativeState({}, {'rub': D('30000.000000123'), 'cny': D('.123456789')}, {})
        after = NativeState({}, {'rub': D('18000.000000118'), 'cny': D('1000.123456789')}, {})
        trade = TradeFact('fx', 'BUY', 1, 1000, 'rub', D(12000), D('.000000005'), 'cny')
    else:
        before = NativeState({}, {'rub': D('10.000000123')}, {})
        after = NativeState({'share': D(1)}, {'rub': D('9.000000118')}, {})
        trade = TradeFact('share', 'BUY', 1, 1, 'rub', D(1), D('.000000005'), None)
    reconcile(before, after, [trade])
    for code, facts in money_deltas(before, after, [trade]).items():
        assert facts['raw_expected'] == facts['settlement_expected'] == after.money[code]
        assert facts['rounding_adjustment'] == facts['settlement_residual'] == 0
    rounded = replace(after, money={
        code: value.quantize(D('.000001')) for code, value in after.money.items()})
    with pytest.raises(SandboxFailure):
        reconcile(before, rounded, [trade])


@pytest.mark.parametrize('fault', ['micro', 'double-fee', 'wrong-currency', 'insufficient',
                                   'blocked', 'inventory', 'unknown-bond', 'fx-flag'])
def test_rounding_does_not_repair_invalid_money_or_ownership(fault):
    """Identity, position, blocked cash and funding guards survive settlement modeling."""
    trade = bond('SELL', 1, '1022.791', '.501900500')
    before = NativeState({'bond': D(1)}, {'cny': D('2949.630308')}, {})
    after = NativeState({}, {'cny': D('3971.919408')}, {})
    if fault == 'micro':
        after = replace(after, money={'cny': after.money['cny'] + D('.000001')})
    elif fault == 'double-fee':
        after = replace(after, money={'cny': after.money['cny'] - trade.commission})
    elif fault == 'wrong-currency':
        trade = replace(trade, currency='rub')
    elif fault == 'insufficient':
        trade = bond('BUY', 1, '1', '.000000500')
        before = NativeState({}, {'cny': D(1)}, {})
        after = NativeState({'bond': D(1)}, {'cny': D(0)}, {})
    elif fault == 'blocked':
        after = replace(after, blocked={'cny': D('.000001')})
    elif fault == 'inventory':
        before = replace(before, quantities={})
    elif fault == 'unknown-bond':
        trade = replace(trade, cny_bond=False)
    else:
        trade = replace(trade, base_currency='usd')
    with pytest.raises(SandboxFailure):
        reconcile(before, after, [trade])


@pytest.mark.parametrize('changes,initial', [
    ({'total': D('1.000000001')}, D(10)),
    ({'commission': D('.000000750')}, D(10)),
    ({}, D('10.000000001')),
])
def test_unmeasured_precision_fails_closed(changes, initial):
    """The measured domain cannot imply rules for other principal/fee/cash precision."""
    trade = bond('BUY', 1, '1', '.000000500', **changes)
    with pytest.raises(SandboxFailure, match='precision'):
        reconcile(NativeState({}, {'cny': initial}, {}),
                  NativeState({'bond': D(1)}, {'cny': D(9)}, {}), [trade])


@pytest.mark.parametrize('changes,initial,raw,reason', [
    ({'commission': D('.000000750')}, D(10), D('8.999999250'), 'precision'),
    ({'total': D('1.000000001')}, D(10), D('8.999999499'), 'precision'),
    ({}, D('10.000000001'), D('8.999999501'), 'precision'),
    ({}, D(1), D('-.000000500'), 'exceeds owned cash'),
])
def test_unsupported_model_keeps_raw_diagnostics(changes, initial, raw, reason):
    """A failed settlement hypothesis must not discard valid raw money evidence."""
    trade = bond('BUY', 1, '1', '.000000500', **changes)
    before = NativeState({}, {'cny': initial, 'rub': D(7)}, {})
    after = NativeState({'bond': D(1)}, {'cny': D(9), 'rub': D(7)}, {})
    terms = money_components(before, [trade])['cny']
    facts = money_deltas(before, after, [trade])
    assert terms['expected'] == terms['raw_expected'] == raw
    assert terms['operations'][0]['raw_expected'] == raw
    assert terms['operations'][0]['unrounded_settlement_expected'] == raw
    assert terms['settlement_expected'] is terms['rounding_adjustment'] is None
    assert terms['settlement_status'] == 'unsupported'
    assert reason in terms['settlement_error']
    assert facts['cny']['expected'] == facts['cny']['raw_expected'] == raw
    assert facts['cny']['residual'] == facts['cny']['raw_residual'] == D(9) - raw
    assert facts['cny']['settlement_status'] == 'unsupported'
    assert reason in facts['cny']['settlement_error']
    assert (facts['cny']['settlement_expected'] is facts['cny']['rounding_adjustment']
            is facts['cny']['settlement_residual'] is None)
    assert facts['rub']['settlement_expected'] == D(7)
    assert facts['rub']['settlement_status'] == 'supported'
    with pytest.raises(SandboxFailure, match=reason):
        reconcile(before, after, [trade])


def test_reverse_fx_keeps_prior_per_order_adjustment_and_rub_exact():
    """Reverse FX preserves the bond adjustment without introducing one of its own."""
    before = NativeState({'bond': D(67)},
                         {'cny': D('11312.615318'), 'rub': D('11906.2')}, {})
    sell = bond('SELL', 67, '68534.099', '33.630884500')
    fx = TradeFact('fx', 'SELL', 68, 1000, 'rub', D(839392), D('419.696'), 'cny')
    after = NativeState({}, {'cny': D('11813.083434'), 'rub': D('850878.504')}, {})
    reconcile(before, after, [sell, fx])
    facts = money_deltas(before, after, [sell, fx])
    assert facts['cny']['raw_expected'] == D('11813.083433500')
    assert facts['cny']['settlement_expected'] == D('11813.083434')
    assert facts['rub']['rounding_adjustment'] == 0
    assert facts['rub']['settlement_residual'] == 0


@pytest.mark.parametrize('error,accepted', [
    (D('.000000001'), True), (D('.000000002'), False),
])
def test_existing_nano_comparison_is_unchanged(error, accepted):
    """The original one-nano tolerance is neither removed nor broadened."""
    before = NativeState({}, {'cny': D(10)}, {})
    after = NativeState({'bond': D(1)}, {'cny': D(9) + error}, {})
    trade = bond('BUY', 1, '1', '0')
    if accepted:
        reconcile(before, after, [trade])
    else:
        with pytest.raises(SandboxFailure):
            reconcile(before, after, [trade])
