"""Independent RUB/CNY sandbox oracles; account deltas are never runtime attribution."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from autorepeater.cash_facts import SecurityQuote, SecurityQuoteUnit
from scripts.sandbox_lifecycle import SandboxFailure

D = Decimal


def require(condition: object, reason: str) -> None:
    """Fixed safe failures survive the live report's sanitization."""
    if not condition:
        raise SandboxFailure(reason)


@dataclass(frozen=True)
class NativeState:
    """Physical security pieces and actual money, including blocked native money."""
    quantities: dict[str, D]
    money: dict[str, D]
    blocked: dict[str, D]


@dataclass(frozen=True)
class TradeFact:  # pylint: disable=too-many-instance-attributes
    """Checked sandbox order fields; total already contains bond ACI, fee is separate."""
    uid: str
    side: str
    lots: int
    lot_size: int
    currency: str
    total: D
    commission: D
    base_currency: str | None
    cny_bond: bool = False


def native_midpoint(quote: SecurityQuote) -> D:
    """Independent dimensional oracle, without execution price or broker RUB marks."""
    require(0 < quote.bid <= quote.ask, 'invalid native bid/ask')
    midpoint = (quote.bid + quote.ask) / 2
    if quote.unit == SecurityQuoteUnit.PERCENT_NOMINAL:
        nominal, aci = quote.nominal, quote.accrued_interest
        require(nominal is not None and aci is not None, 'missing native bond units')
        require(nominal.currency == aci.currency == quote.currency and nominal.amount > 0
                and aci.amount >= 0, 'invalid native bond units')
        return midpoint * nominal.amount / 100 + aci.amount
    require(quote.unit == SecurityQuoteUnit.MONEY_PER_PIECE
            and quote.nominal is None and quote.accrued_interest is None,
            'unexpected security quotation units')
    return midpoint


def fees_by_currency(trades: list[TradeFact]) -> dict[str, D]:
    """Count each executed fee once, without also summing history BROKER_FEE."""
    result: dict[str, D] = {}
    for trade in trades:
        require(trade.commission.is_finite() and trade.commission >= 0, 'invalid commission')
        result[trade.currency] = result.get(trade.currency, D(0)) + trade.commission
    return result


def cash_value(money: dict[str, D], rates: dict[str, D]) -> D:
    """Cash reserve is the sum of native units valued by the same FX midpoint."""
    require(set(money) <= set(rates) and rates.get('rub') == 1, 'missing cash valuation rate')
    require(all(value.is_finite() and value > 0 for value in rates.values()), 'invalid FX rate')
    return sum((amount * rates[code] for code, amount in money.items()), D(0))


def required_fx_lots(purchase: D, reserve: D, available: D, lot: int, *,
                     commission: D = D(0)) -> int:
    """First test the purchase reason, then subtract existing units before rounding."""
    require(all(value.is_finite() and value >= 0
                for value in (purchase, reserve, available, commission))
            and isinstance(lot, int) and not isinstance(lot, bool) and lot > 0,
            'invalid funding oracle input')
    if purchase == 0 or available >= purchase + commission:
        return 0
    return int((max(D(0), purchase + reserve - available) / lot).to_integral_value(
        rounding=ROUND_CEILING))


def _settlement_step(balance: D | None, delta: D,
                     trade: TradeFact) -> tuple[D | None, D | None, str | None]:
    """Retain raw evidence when the measured CNY model cannot predict settlement."""
    if balance is None:
        return None, None, None
    unrounded = balance + delta
    if not trade.cny_bond:
        return unrounded, unrounded, None
    # Observations cover micro principals/cash and micro or half-micro fees.
    if not (balance.is_finite() and balance % D('.000001') == 0
            and trade.total % D('.000001') == 0
            and trade.commission % D('.000001') in (D(0), D('.0000005'))):
        return unrounded, None, 'unsupported sandbox CNY bond precision'
    if unrounded < 0:
        return unrounded, None, 'CNY bond exceeds owned cash'
    return unrounded, unrounded.quantize(D('.000001'), rounding=ROUND_HALF_UP), None


def money_components(before: NativeState, trades: list[TradeFact]) -> dict[str, dict]:
    """Model observed Sandbox CNY bonds only, not a general broker settlement contract."""
    fees_by_currency(trades)
    codes = set(before.money) | {trade.currency for trade in trades} | {
        trade.base_currency for trade in trades if trade.base_currency is not None}
    result = {code: {'before': before.money.get(code, D(0)), 'security_principal': D(0),
                     'fx_quote': D(0), 'fx_base': D(0), 'commission': D(0),
                     'raw_expected': before.money.get(code, D(0)),
                     'settlement_expected': before.money.get(code, D(0)),
                     'settlement_error': None,
                     'rounding_adjustment': D(0), 'operations': []} for code in codes}
    for sequence, trade in enumerate(trades, 1):
        require(trade.side in ('BUY', 'SELL')
                and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                        for value in (trade.lots, trade.lot_size))
                and trade.total.is_finite() and trade.total > 0
                and isinstance(trade.cny_bond, bool), 'invalid trade facts')
        require(not trade.cny_bond or (trade.currency == 'cny' and trade.base_currency is None),
                'invalid CNY bond identity')
        sign = D(1) if trade.side == 'BUY' else D(-1)
        key = 'security_principal' if trade.base_currency is None else 'fx_quote'
        result[trade.currency][key] -= sign * trade.total
        result[trade.currency]['commission'] -= trade.commission
        changes = {trade.currency: -sign * trade.total - trade.commission}
        if trade.base_currency is not None:
            require(trade.base_currency != trade.currency, 'invalid FX money identity')
            result[trade.base_currency]['fx_base'] += sign * D(trade.lots * trade.lot_size)
            changes[trade.base_currency] = sign * D(trade.lots * trade.lot_size)
        for code, delta in changes.items():
            terms = result[code]
            unrounded, settled, error = _settlement_step(terms['settlement_expected'], delta, trade)
            terms['settlement_error'] = terms['settlement_error'] or error
            terms['raw_expected'] += delta
            terms['operations'].append({
                'sequence': sequence, 'uid': trade.uid, 'side': trade.side,
                'cny_bond': trade.cny_bond, 'before': terms['settlement_expected'],
                'raw_delta': delta, 'raw_expected': terms['raw_expected'],
                'unrounded_settlement_expected': unrounded, 'settlement_expected': settled,
                'rounding_adjustment': None if settled is None else settled - unrounded})
            terms['settlement_expected'] = settled
    for terms in result.values():
        terms['expected'] = terms['raw_expected']
        terms['settlement_status'] = ('unsupported' if terms['settlement_expected'] is None
                                      else 'supported')
        terms['rounding_adjustment'] = (None if terms['settlement_expected'] is None else
                                        terms['settlement_expected'] - terms['raw_expected'])
    return result


def money_deltas(before: NativeState, after: NativeState,
                 trades: list[TradeFact]) -> dict[str, dict]:
    """Preserve raw residuals and expose the separate sequential settlement model."""
    components = money_components(before, trades)
    result = {}
    for code in sorted(set(components) | set(after.money)):
        terms = components.get(code, {})
        raw = terms.get('raw_expected', D(0))
        settled = terms.get('settlement_expected', D(0))
        initial, actual = before.money.get(code, D(0)), after.money.get(code, D(0))
        result[code] = {
            'before': initial, 'expected': raw, 'raw_expected': raw, 'actual': actual,
            'expected_delta': raw - initial, 'actual_delta': actual - initial,
            'residual': actual - raw, 'raw_residual': actual - raw,
            'settlement_expected': settled,
            'rounding_adjustment': terms.get('rounding_adjustment', D(0)),
            'settlement_residual': None if settled is None else actual - settled,
            'settlement_status': terms.get('settlement_status', 'supported'),
            'settlement_error': terms.get('settlement_error'),
        }
    return result


def reconcile(before: NativeState, after: NativeState, trades: list[TradeFact]) -> None:
    """Sandbox conservation at nano precision; no slippage comparison with estimates."""
    components = money_components(before, trades)
    for terms in components.values():
        require(terms['settlement_expected'] is not None, terms['settlement_error'])
    quantities = dict(before.quantities)
    for trade in trades:
        sign = D(1) if trade.side == 'BUY' else D(-1)
        pieces = D(trade.lots * trade.lot_size)
        if trade.base_currency is None:
            quantities[trade.uid] = quantities.get(trade.uid, D(0)) + sign * pieces
    require(all(value >= 0 for value in after.quantities.values())
            and {uid: value for uid, value in quantities.items() if value} == after.quantities,
            'security quantities disagree with checked fills')
    require(all(value == 0 for value in after.blocked.values()), 'native money remains blocked')
    for code in set(components) | set(after.money):
        require(after.money.get(code, D(0)) >= 0 and abs(
            components.get(code, {}).get('settlement_expected', D(0))
            - after.money.get(code, D(0))) <= D('.000000001'),
            'native money disagrees with order totals and single commissions')
