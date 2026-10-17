"""Pure direct RUB routes and immutable per-calculation valuation snapshots."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.fx_data import FxData, FxPair, FxQuote, fx_ratio, market_timestamp
from autorepeater.strategy_plan import exact_product


def select_direct_pair(pairs: Sequence[FxPair], currency: str, *,
                       pair_uid: str | None = None) -> FxPair:
    """Never infer identity/settlement from a ticker, availability or catalog order."""
    currency = currency_code(currency)
    identities = [pair.uid for pair in pairs]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate FX pair UID')
    candidates = [pair for pair in pairs
                  if pair.base_currency == currency and currency != 'rub'
                  and pair.quote_currency == 'rub'
                  and (pair_uid is None or pair.uid == pair_uid)]
    if not candidates:
        raise ValueError('no direct FX route')
    if len(candidates) != 1:
        raise ValueError('ambiguous direct FX route; select an explicit pair UID')
    return candidates[0]


@dataclass(frozen=True, slots=True)
class FxSnapshot:
    """One immutable set of native-to-RUB midpoint observations, independent of caps."""
    quotes: Mapping[str, FxQuote]
    as_of: datetime
    _rates: Mapping[str, Decimal] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        market_timestamp(self.as_of)
        copied = {}
        for currency, quote in self.quotes.items():
            code = currency_code(currency)
            if code == 'rub' or code in copied or code != quote.pair.base_currency:
                raise ValueError('invalid FX snapshot currency identity')
            quote.validate_fresh(self.as_of)
            copied[code] = quote
        object.__setattr__(self, 'quotes', MappingProxyType(copied))
        object.__setattr__(self, '_rates', MappingProxyType({
            currency: quote.lot_quote.midpoint_per_unit for currency, quote in copied.items()}))

    def validate_fresh(self, now: datetime) -> None:
        """Detect expiry before effects; the caller must read a new observation."""
        market_timestamp(now)
        for quote in self.quotes.values():
            quote.validate_fresh(now)

    def rate(self, currency: str) -> Decimal:
        """RUB per native unit, with an implicit rate only for RUB itself."""
        code = currency_code(currency)
        if code == 'rub':
            return Decimal(1)
        if code not in self.quotes:
            raise ValueError('missing FX valuation rate')
        return self._rates[code]

    def to_rub(self, amount: MoneyAmount) -> MoneyAmount:
        """Retain exact finite Decimal multiplication and explicit source currency."""
        return MoneyAmount('rub', exact_product(amount.amount, self.rate(amount.currency)))

    def from_rub(self, amount: MoneyAmount, currency: str) -> MoneyAmount:
        """Convert a RUB valuation to native units; no FX request or trading permission."""
        if amount.currency != 'rub':
            raise ValueError('from_rub requires rub money')
        code = currency_code(currency)
        if code == 'rub':
            return amount
        return MoneyAmount(code, fx_ratio(amount.amount, self.rate(code)))


def build_fx_snapshot(data: FxData, currencies: Sequence[str], *,
                      now: datetime | None = None) -> FxSnapshot:
    """Load each required direct pair once; validate the whole set after all reads."""
    codes = sorted({currency_code(currency) for currency in currencies} - {'rub'})
    quotes = {}
    if codes:
        pairs = data.list_pairs()
        for currency in codes:
            pair = select_direct_pair(pairs, currency)
            quote = data.get_quote(pair)
            if quote.pair != pair:
                raise ValueError('FX quote identity differs from selected pair')
            quotes[currency] = quote
    return FxSnapshot(quotes, now if now is not None else datetime.now(timezone.utc))
