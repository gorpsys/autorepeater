"""Diagnostic fee models never change the application's monetary reconciliation."""
from dataclasses import replace
from decimal import Decimal as D, ROUND_HALF_UP

import pytest

from scripts.sandbox_commission_probe import CommissionSample, compare_commissions


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
    (False, 'response_fee_rounding_6dp_consistent'),
    (True, 'per_order_cash_rounding_6dp_consistent'),
])
def test_distinct_fee_models_are_reported_only_when_independent_history_fits(rounded_cash, verdict):
    """Only the independently matching balance/history model is named."""
    result = compare_commissions(samples(rounded_cash=rounded_cash))
    assert result['verdict'] == verdict
    assert result['comparable_principal'] is True
    assert result['response_fee_sum_delta'] == D('.000001')
    assert result['charged_fee_sum_delta'] == (D('.000001') if rounded_cash else D(0))
    assert 'ROUND_HALF_UP' in result['response_rounding_modes']


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
