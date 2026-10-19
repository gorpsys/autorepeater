"""Compare observed CNY fee hypotheses, without asserting an undocumented API contract."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP

D = Decimal
NANO = D('.000000001')
MICRO = D('.000001')


@dataclass(frozen=True)
class CommissionSample:
    """One checked FILL and its account delta; missing history remains explicitly unknown."""
    lots: int
    principal: D
    reported_fee: D
    debit: D
    history_principal: D | None
    history_fee: D | None


def _rounding_modes(values, observed):
    return [mode for mode in (ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP)
            if all(value is not None and value.quantize(MICRO, rounding=mode) == actual
                   for value, actual in zip(values, observed))]


def compare_commissions(samples: tuple[CommissionSample, ...]) -> dict:
    """Require equal total principal and independent history before naming a consistent model."""
    if len(samples) != 3 or tuple(item.lots for item in samples) != (2, 1, 1):
        raise ValueError('invalid commission probe design')
    charged = tuple(item.debit - item.principal for item in samples)
    reported = tuple(item.reported_fee for item in samples)
    history = tuple(item.history_fee for item in samples)
    residuals = tuple(item.debit - item.principal - item.reported_fee for item in samples)
    history_residuals = tuple(None if item.history_principal is None or item.history_fee is None
                              else item.debit - item.history_principal - item.history_fee
                              for item in samples)
    response_modes = _rounding_modes(history, reported)
    cash_modes = _rounding_modes(history, charged)
    comparable = samples[0].principal == samples[1].principal + samples[2].principal
    principal_matches = all(item.history_principal is not None
                            and abs(item.history_principal - item.principal) <= NANO
                            for item in samples)
    differs = any(fee is not None and abs(fee - actual) > NANO
                  for fee, actual in zip(history, reported))
    reported_fit = all(abs(value) <= NANO for value in residuals)
    history_fit = all(value is not None and abs(value) <= NANO for value in history_residuals)
    verdict = 'inconclusive'
    if comparable and principal_matches and differs:
        if history_fit and not reported_fit and response_modes:
            verdict = 'response_fee_rounding_6dp_consistent'
        elif reported_fit and not history_fit and cash_modes:
            verdict = 'per_order_cash_rounding_6dp_consistent'
    return {'verdict': verdict, 'comparable_principal': comparable,
            'history_principal_matches': principal_matches,
            'reported_residuals': residuals, 'history_residuals': history_residuals,
            'response_rounding_modes': response_modes, 'cash_rounding_modes': cash_modes,
            'response_fee_sum_delta': reported[1] + reported[2] - reported[0],
            'charged_fee_sum_delta': charged[1] + charged[2] - charged[0]}
