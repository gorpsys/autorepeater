"""Compare observed CNY fee hypotheses, without asserting an undocumented API contract."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP

D = Decimal
NANO = D('.000000001')
MICRO = D('.000001')


@dataclass(frozen=True)
class CommissionObservation:
    """Displayed native balances and independently observed native fill prices."""
    before_balance: D
    after_balance: D
    fill_prices: tuple[D, ...]


@dataclass(frozen=True)
class CommissionSample:  # pylint: disable=too-many-instance-attributes
    """One checked FILL and its account delta; missing history remains explicitly unknown."""
    lots: int
    principal: D
    reported_fee: D
    debit: D
    history_principal: D | None
    history_fee: D | None
    observation: CommissionObservation | None = None
    side: str = 'BUY'

    @property
    def cash_principal(self):
        """Cash debit is negative for a sale; raw principal always stays positive."""
        return self.principal if self.side == 'BUY' else -self.principal

    @property
    def before_balance(self):
        """Missing displayed balances remain unknown, not reconstructed from fees."""
        return None if self.observation is None else self.observation.before_balance

    @property
    def after_balance(self):
        """Observed post-trade balance, not the model's expected balance."""
        return None if self.observation is None else self.observation.after_balance

    @property
    def fill_prices(self):
        """Missing per-fill prices prevent controlled inference."""
        return () if self.observation is None else self.observation.fill_prices


def _rounding_modes(values, observed):
    return [mode for mode in (ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP)
            if all(value is not None and value.quantize(MICRO, rounding=mode) == actual
                   for value, actual in zip(values, observed))]


def _balance_models(samples):
    """Contrast per-fee, sequential cash and cumulative display-only hypotheses."""
    if any(item.before_balance is None or item.after_balance is None for item in samples):
        return []
    result = []
    for model in ('per-operation-fee', 'per-operation-balance', 'display-final-balance'):
        for mode in (ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP):
            predicted = []
            balance = samples[0].before_balance
            for index, item in enumerate(samples):
                if index == 1:
                    balance = item.before_balance
                fee = (item.reported_fee.quantize(MICRO, rounding=mode)
                       if model == 'per-operation-fee' else item.reported_fee)
                balance -= item.cash_principal + fee
                predicted.append(balance.quantize(MICRO, rounding=mode))
                if model == 'per-operation-balance':
                    balance = predicted[-1]
            residuals = tuple(item.after_balance - value
                              for item, value in zip(samples, predicted))
            result.append({'model': model, 'rounding': mode,
                           'predicted_after': tuple(predicted),
                           'balance_residuals': residuals,
                           'matches': all(abs(value) <= NANO for value in residuals)})
    return result


def _comparison_controls(samples):
    native_prices = tuple(item.principal / item.lots for item in samples)
    raw_fees = tuple(item.reported_fee / item.lots for item in samples)
    fill_prices = tuple(price for item in samples for price in item.fill_prices)
    return {'normalized_native_prices': native_prices, 'normalized_raw_fees': raw_fees,
            'per_fill_prices_match': (len(set(native_prices)) == 1
                                      and all(item.fill_prices for item in samples)
                                      and len(set(fill_prices)) == 1
                                      and all(price > 0 for price in fill_prices)),
            'normalized_raw_fees_match': len(set(raw_fees)) == 1}


def compare_commissions(samples: tuple[CommissionSample, ...]) -> dict:
    """Report tested numeric fits and unresolved alternatives, never a ledger-contract verdict."""
    if (len(samples) != 3 or tuple(item.lots for item in samples) != (2, 1, 1)
            or len({item.side for item in samples}) != 1
            or any(item.side not in ('BUY', 'SELL') for item in samples)):
        raise ValueError('invalid commission probe design')
    charged = tuple(item.debit - item.cash_principal for item in samples)
    reported = tuple(item.reported_fee for item in samples)
    history = tuple(item.history_fee for item in samples)
    residuals = tuple(item.debit - item.cash_principal - item.reported_fee for item in samples)
    history_residuals = tuple(None if item.history_principal is None or item.history_fee is None
                              else item.debit - (item.history_principal if item.side == 'BUY'
                                                 else -item.history_principal) - item.history_fee
                              for item in samples)
    history_modes = _rounding_modes(reported, history)
    comparable = samples[0].principal == samples[1].principal + samples[2].principal
    principal_matches = all(item.history_principal is not None
                            and abs(item.history_principal - item.principal) <= NANO
                            for item in samples)
    controls = _comparison_controls(samples)
    balances_match = (all(item.before_balance is not None and item.after_balance is not None
                          and item.before_balance - item.after_balance == item.debit
                          for item in samples)
                      and samples[0].before_balance == samples[1].before_balance
                      and samples[1].after_balance == samples[2].before_balance)
    controlled = bool(comparable and principal_matches and controls['per_fill_prices_match']
                      and controls['normalized_raw_fees_match']
                      and balances_match and history_modes)
    models = _balance_models(samples)
    cash_controls = {
        'principal_sum_matches': comparable,
        'raw_fee_sum_matches': reported[0] == reported[1] + reported[2],
        'balances_match': balances_match,
        'principal_exact_micro': all(item.principal % MICRO == 0 for item in samples),
        'combined_fee_exact_micro': reported[0] % MICRO == 0,
        'single_fees_half_micro': all(value % MICRO == MICRO / 2 for value in reported[1:]),
    }
    settlement_verdict = 'inconclusive'
    if (all(cash_controls.values())
            and any(item['matches'] and item['model'] != 'display-final-balance' for item in models)
            and not any(item['matches'] and item['model'] == 'display-final-balance'
                        for item in models)):
        settlement_verdict = 'per-operation-rounding-supported'
    return {'verdict': 'inconclusive', 'comparable_principal': comparable,
            'history_principal_matches': principal_matches,
            'reported_residuals': residuals, 'history_residuals': history_residuals,
            'history_rounding_modes': history_modes,
            **controls, 'controlled_inputs': controlled,
            'models': models,
            'settlement_verdict': settlement_verdict, 'settlement_controls': cash_controls,
            'split_minus_combined': (None if samples[0].after_balance is None
                                     or samples[2].after_balance is None else
                                     samples[2].after_balance - samples[0].after_balance),
            'consistent_models': [item for item in models if controlled and item['matches']],
            'unresolved_alternatives': ['raw_fee_may_not_be_authoritative',
                                        'other_ledger_precision_or_fee_rules'],
            'response_fee_sum_delta': reported[1] + reported[2] - reported[0],
            'charged_fee_sum_delta': charged[1] + charged[2] - charged[0]}
