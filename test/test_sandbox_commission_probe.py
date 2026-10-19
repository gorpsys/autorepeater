"""Diagnostic fee models never change the application's monetary reconciliation."""
from dataclasses import replace
from decimal import Decimal as D, ROUND_HALF_UP

import pytest

from scripts.sandbox_commission_probe import (
    CommissionObservation, CommissionSample, compare_commissions)


def samples(*, rounded_cash=False):
    """Three equal-price fills distinguish exact fees from six-decimal responses."""
    result = []
    for lots in (2, 1, 1):
        principal = D(100) * lots
        fee = D('.1234565') * lots
        reported = fee.quantize(D('.000001'), rounding=ROUND_HALF_UP)
        result.append(CommissionSample(
            lots, principal, reported, principal + (reported if rounded_cash else fee),
            principal, fee))
    return tuple(result)


@pytest.mark.parametrize('rounded_cash,verdict', [
    (False, 'inconclusive'),
    (True, 'inconclusive'),
])
def test_distinct_fee_models_are_reported_only_when_independent_history_fits(rounded_cash, verdict):
    """Only the independently matching balance/history model is named."""
    result = compare_commissions(samples(rounded_cash=rounded_cash))
    assert result['verdict'] == verdict
    assert result['comparable_principal'] is True
    assert result['response_fee_sum_delta'] == D('.000001')
    assert result['charged_fee_sum_delta'] == (D('.000001') if rounded_cash else D(0))
    assert not result['consistent_models']


@pytest.mark.parametrize('fault', [
    'equal-fees', 'price-change', 'missing-history', 'principal-disagreement', 'unexplained-cash',
])
def test_nondifferentiating_or_incomplete_facts_are_inconclusive(fault):
    """Equal facts and missing monetary provenance cannot establish a model."""
    values = list(samples())
    if fault == 'equal-fees':
        values = [replace(item, reported_fee=item.history_fee) for item in values]
    elif fault == 'price-change':
        values[0] = replace(
            values[0], principal=D(201), history_principal=D(201), debit=values[0].debit + 1)
    elif fault == 'missing-history':
        values[1] = replace(values[1], history_fee=None)
    elif fault == 'principal-disagreement':
        values[1] = replace(values[1], history_principal=D('99.99'))
    else:
        values[2] = replace(values[2], debit=values[2].debit + D('.01'))
    assert compare_commissions(tuple(values))['verdict'] == 'inconclusive'


def test_each_fee_is_counted_once_and_nano_residual_is_preserved():
    """Half-micro differences remain exact; no tolerance or production rounding changes."""
    result = compare_commissions(samples())
    assert result['reported_residuals'] == (D(0), D('-.0000005'), D('-.0000005'))
    assert result['history_residuals'] == (D(0), D(0), D(0))


@pytest.mark.parametrize('values', [(), samples()[:2], tuple(reversed(samples()))])
def test_probe_requires_exact_two_versus_one_plus_one_design(values):
    """Wrong counts or ordering cannot become a different controlled experiment."""
    with pytest.raises(ValueError, match='probe design'):
        compare_commissions(values)


def actual_samples(*, model='per-operation-fee', mode=ROUND_HALF_UP):
    """Actual precision direction: raw fees 7dp, history and positions 6dp."""
    result = []
    exact, displayed = D(1000), D(1000)
    for index, lots in enumerate((2, 1, 1)):
        if index == 1:
            exact, displayed = D(1000), D(1000)
        principal, fee = D(100) * lots, D('.1234565') * lots
        history_fee = fee.quantize(D('.000001'), rounding=mode)
        exact -= principal + (history_fee if model == 'per-operation-fee' else fee)
        after = exact.quantize(D('.000001'), rounding=mode)
        result.append(CommissionSample(
            lots, principal, fee, displayed - after, principal, history_fee,
            CommissionObservation(displayed, after, (D(100),))))
        displayed = after
    return tuple(result)


@pytest.mark.parametrize('mode', ['ROUND_DOWN', 'ROUND_HALF_EVEN', 'ROUND_HALF_UP'])
@pytest.mark.parametrize('model', ['per-operation-fee', 'display-final-balance'])
def test_actual_direction_predictions_keep_unresolved_alternatives(model, mode):
    """Compare numeric observations without asserting broker ledger semantics."""
    values = actual_samples(model=model, mode=mode)
    result = compare_commissions(values)
    assert result['verdict'] == 'inconclusive'
    assert result['controlled_inputs'] is True
    assert result['unresolved_alternatives']
    matching = [item for item in result['models'] if item['matches']]
    expected = next(item for item in matching if item['model'] == model
                    and item['rounding'] == mode)
    assert expected['predicted_after'] == tuple(item.after_balance for item in values)
    assert expected['balance_residuals'] == (D(0),) * 3
    assert mode in result['history_rounding_modes']


def test_actual_direction_has_distinguishing_split_delta():
    """Individually rounded raw fees differ from final-balance rounding."""
    cash = compare_commissions(actual_samples())
    display = compare_commissions(actual_samples(model='display-final-balance'))
    assert cash['charged_fee_sum_delta'] == D('.000001')
    assert display['charged_fee_sum_delta'] == 0
    assert cash['reported_residuals'] == (D(0), D('.0000005'), D('.0000005'))
    assert display['reported_residuals'] == (D(0), D('-.0000005'), D('.0000005'))
    assert not any(item['matches'] and item['model'] == 'display-final-balance'
                   for item in cash['models'])


@pytest.mark.parametrize('fault', [
    'aggregate-only', 'raw-fee-change', 'fill-price-change', 'missing-fills',
    'missing-history', 'principal-disagreement', 'missing-balances', 'balance-discontinuity',
    'unequal-funding',
])
def test_confounded_inputs_cannot_identify_a_model(fault):
    """Equal aggregate principal alone hides price or fee confounding."""
    values = list(actual_samples())
    if fault == 'aggregate-only':
        values[1] = replace(values[1], principal=D(90), history_principal=D(90))
        values[2] = replace(values[2], principal=D(110), history_principal=D(110))
    elif fault == 'raw-fee-change':
        values[0] = replace(values[0], reported_fee=D('.2'))
    elif fault == 'fill-price-change':
        values[0] = replace(values[0], observation=replace(
            values[0].observation, fill_prices=(D(90), D(110))))
    elif fault == 'missing-fills':
        values[0] = replace(values[0], observation=replace(values[0].observation, fill_prices=()))
    elif fault == 'missing-history':
        values[1] = replace(values[1], history_fee=None)
    elif fault == 'principal-disagreement':
        values[1] = replace(values[1], history_principal=D('99.99'))
    elif fault == 'missing-balances':
        values[1] = replace(values[1], observation=None)
    elif fault == 'balance-discontinuity':
        values[2] = replace(values[2], observation=replace(
            values[2].observation, before_balance=values[2].before_balance + 1))
    else:
        values[0] = replace(values[0], observation=replace(
            values[0].observation, before_balance=D(1001)))
    result = compare_commissions(tuple(values))
    assert result['verdict'] == 'inconclusive'
    assert result['controlled_inputs'] is False
    assert not result['consistent_models']


def test_overlapping_models_and_unexplained_balances_remain_inconclusive():
    """Multiple fits or unexplained residuals are not a conclusion."""
    values = tuple(CommissionSample(
        lots, D(100) * lots, D(0), D(100) * lots, D(100) * lots, D(0),
        CommissionObservation(D(before), D(after), (D(100),)))
        for lots, before, after in ((2, 1000, 800), (1, 1000, 900), (1, 900, 800)))
    result = compare_commissions(values)
    assert len({item['model'] for item in result['models'] if item['matches']}) == 2
    assert result['verdict'] == 'inconclusive'
    values = list(actual_samples())
    values[2] = replace(values[2], observation=replace(
        values[2].observation, after_balance=values[2].after_balance - D('.01')),
                        debit=values[2].debit + D('.01'))
    result = compare_commissions(tuple(values))
    assert not result['consistent_models']
