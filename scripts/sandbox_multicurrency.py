"""Independent RUB/CNY sandbox oracles; account deltas are never runtime attribution."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

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


def money_components(before: NativeState, trades: list[TradeFact]) -> dict[str, dict[str, D]]:
    """Signed principal/FX/fee terms explain the existing oracle without rounding."""
    codes = set(before.money) | {trade.currency for trade in trades} | {
        trade.base_currency for trade in trades if trade.base_currency is not None}
    result = {code: {'before': before.money.get(code, D(0)), 'security_principal': D(0),
                     'fx_quote': D(0), 'fx_base': D(0), 'commission': D(0)} for code in codes}
    for trade in trades:
        sign = D(1) if trade.side == 'BUY' else D(-1)
        key = 'security_principal' if trade.base_currency is None else 'fx_quote'
        result[trade.currency][key] -= sign * trade.total
        result[trade.currency]['commission'] -= trade.commission
        if trade.base_currency is not None:
            result[trade.base_currency]['fx_base'] += sign * D(trade.lots * trade.lot_size)
    for terms in result.values():
        terms['expected'] = sum(terms.values(), D(0))
    return result


def money_deltas(before: NativeState, after: NativeState,
                 trades: list[TradeFact]) -> dict[str, dict[str, D]]:
    """Observe the oracle's native deltas without rounding or changing its assertions."""
    expected = dict(before.money)
    for trade in trades:
        sign = D(1) if trade.side == 'BUY' else D(-1)
        if trade.base_currency is not None:
            expected[trade.base_currency] = expected.get(trade.base_currency, D(0)) + sign * D(
                trade.lots * trade.lot_size)
        expected[trade.currency] = (expected.get(trade.currency, D(0))
                                    - sign * trade.total - trade.commission)
    return {code: {
        'before': before.money.get(code, D(0)), 'expected': expected.get(code, D(0)),
        'actual': after.money.get(code, D(0)),
        'expected_delta': expected.get(code, D(0)) - before.money.get(code, D(0)),
        'actual_delta': after.money.get(code, D(0)) - before.money.get(code, D(0)),
        'residual': after.money.get(code, D(0)) - expected.get(code, D(0)),
    } for code in sorted(set(before.money) | set(expected) | set(after.money))}


def reconcile(before: NativeState, after: NativeState, trades: list[TradeFact]) -> None:
    """Sandbox conservation at nano precision; no slippage comparison with estimates."""
    quantities, cash = dict(before.quantities), dict(before.money)
    for trade in trades:
        require(trade.side in ('BUY', 'SELL')
                and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                        for value in (trade.lots, trade.lot_size))
                and trade.total.is_finite() and trade.total > 0, 'invalid trade facts')
        sign = D(1) if trade.side == 'BUY' else D(-1)
        pieces = D(trade.lots * trade.lot_size)
        if trade.base_currency is None:
            quantities[trade.uid] = quantities.get(trade.uid, D(0)) + sign * pieces
        else:
            require(trade.base_currency != trade.currency, 'invalid FX money identity')
            cash[trade.base_currency] = cash.get(trade.base_currency, D(0)) + sign * pieces
        cash[trade.currency] = (cash.get(trade.currency, D(0))
                                - sign * trade.total - trade.commission)
    fees_by_currency(trades)
    require(all(value >= 0 for value in after.quantities.values())
            and {uid: value for uid, value in quantities.items() if value} == after.quantities,
            'security quantities disagree with checked fills')
    require(all(value == 0 for value in after.blocked.values()), 'native money remains blocked')
    for code in set(cash) | set(after.money):
        require(after.money.get(code, D(0)) >= 0 and abs(
            cash.get(code, D(0)) - after.money.get(code, D(0))) <= D('.000000001'),
            'native money disagrees with order totals and single commissions')
