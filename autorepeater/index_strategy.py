"""Fresh SDK snapshots and pure index target calculation from a validated base."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from t_tech.invest import InstrumentIdType

from autorepeater import reporting
from autorepeater.portfolio import TargetPortfolio


INDEX_BOARDS = {'share': 'TQBR', 'etf': 'TQTF'}


@dataclass
class IndexQuote:
    """Per-unit price and lot size for a resolved index constituent."""
    uid: str
    price: Decimal
    lot: int
    currency: str = ''
    time: datetime | None = None


def _resolve_index_instrument(client, ticker, resolved):
    response = client.instruments.find_instrument(query=ticker)
    matches = [item for item in response.instruments
               if item.ticker == ticker and item.instrument_type in INDEX_BOARDS
               and item.class_code == INDEX_BOARDS[item.instrument_type]]
    if len(matches) != 1:
        raise ValueError(
            f'expected exactly one index instrument: {ticker} (TQBR/TQTF), '
            f'found {len(matches)}')
    class_code = matches[0].class_code
    instrument_type = matches[0].instrument_type
    uid = matches[0].uid
    if not isinstance(uid, str) or not uid or uid in resolved:
        raise ValueError(f'invalid or duplicate index UID: {ticker} ({class_code})')
    instrument = client.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid).instrument
    if (instrument is None or instrument.uid != uid or instrument.ticker != ticker
            or instrument.instrument_type != instrument_type
            or instrument.class_code != class_code):
        raise ValueError(f'invalid index instrument metadata: {ticker} ({uid}, {class_code})')
    if (isinstance(instrument.lot, bool) or not isinstance(instrument.lot, int)
            or instrument.lot <= 0):
        raise ValueError(f'invalid index lot: {ticker} ({class_code})')
    return instrument


def _index_price(quotation, ticker):
    if quotation is None:
        raise ValueError(f'missing index price: {ticker}')
    try:
        price = Decimal(quotation.units) + Decimal(quotation.nano) / Decimal('1000000000')
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ValueError(f'invalid index price: {ticker}') from error
    if not price.is_finite() or price <= 0:
        raise ValueError(f'invalid index price: {ticker}')
    return price


class IndexStrategy:
    """Index data and targets with config fixed at construction, without SDK access."""

    def __init__(self, config):
        self.config = config

    @property
    def default_reserve(self):
        """Use the config's reserve unless the caller explicitly overrides it."""
        return self.config.reserve

    def load_snapshot(self, client):
        """Resolve the entire base anew; unavailable data aborts the calculation."""
        instruments = {}
        for item in self.config.instruments:
            instrument = _resolve_index_instrument(client, item.ticker, instruments)
            instruments[instrument.uid] = instrument
        response = client.market_data.get_last_prices(instrument_id=list(instruments))
        prices = {}
        for quote in response.last_prices:
            uid = quote.instrument_uid
            if uid not in instruments or uid in prices:
                raise ValueError(f'unexpected or duplicate index quote UID: {uid}')
            prices[uid] = quote
        snapshot = {}
        for uid, instrument in instruments.items():
            ticker = instrument.ticker
            if uid not in prices:
                raise ValueError(f'missing index price: {ticker} ({uid})')
            quote = prices[uid]
            snapshot[ticker] = IndexQuote(
                uid, _index_price(quote.price, ticker), instrument.lot,
                currency=instrument.currency, time=quote.time)
        return snapshot

    def build_target(self, snapshot, budget):
        """Use the shared pure calculator without further SDK calls."""
        return build_index_target(self.config, snapshot, budget)

    def events(self, client, dst_account_id):
        """Recalculate populated, fully unblocked destinations; propagate stream errors."""
        for response in client.operations_stream.positions_stream(accounts=[dst_account_id]):
            position = response.position
            triggered = (
                position is not None and position.account_id == dst_account_id
                and bool(position.securities or position.money)
                and all(item.blocked == 0 for item in position.securities)
                and all(item.blocked_value.units == 0 and item.blocked_value.nano == 0
                        for item in position.money))
            if not triggered:
                reporting.print_skipped_event(response)
            yield triggered


@dataclass
class IndexAllocation:
    """One pass of allocation, retained for calibration including exclusions."""
    weight: Decimal
    ideal_lots: Decimal
    lots: int
    actual_weight: Decimal
    error: Decimal
    exclusion_reason: str


@dataclass
class IndexCalculation:
    """Target plus current capitalizations and chronological per-ticker passes."""
    target: TargetPortfolio
    capitalizations: dict[str, Decimal]
    passes: list[dict[str, IndexAllocation]]
    min_exclusions: list[str]


def _capitalizations(config, snapshot):
    result = {}
    uids = set()
    for instrument in sorted(config.instruments, key=lambda item: item.ticker):
        ticker = instrument.ticker
        quote = snapshot.get(ticker)
        if quote is None:
            raise ValueError(f'missing index snapshot: {ticker}')
        if not isinstance(quote.price, Decimal) or not quote.price.is_finite() or quote.price <= 0:
            raise ValueError(f'invalid index price: {ticker}')
        if isinstance(quote.lot, bool) or not isinstance(quote.lot, int) or quote.lot <= 0:
            raise ValueError(f'invalid index lot: {ticker}')
        if not isinstance(quote.uid, str) or not quote.uid or quote.uid in uids:
            raise ValueError(f'invalid or duplicate index UID: {ticker}')
        uids.add(quote.uid)
        result[ticker] = (instrument.reference_index_capitalization * quote.price
                          / instrument.reference_price)
    return result


def _relative_error(lots, lot_cost, budget, weight):
    return abs(lots * lot_cost / budget - weight) / weight


def _allocate_lots(capitalizations, snapshot, budget):
    total = sum((capitalizations[ticker] for ticker in sorted(capitalizations)), Decimal(0))
    weights = {ticker: value / total for ticker, value in capitalizations.items()}
    lot_costs = {ticker: snapshot[ticker].price * snapshot[ticker].lot for ticker in weights}
    ideal = {ticker: budget * weight / lot_costs[ticker] for ticker, weight in weights.items()}
    lots = {ticker: round(value) for ticker, value in ideal.items()}
    remaining = budget - sum((lots[ticker] * lot_costs[ticker] for ticker in weights), Decimal(0))
    penalties = {}
    for ticker, value in ideal.items():
        if lots[ticker] > value.to_integral_value(rounding=ROUND_FLOOR):
            penalties[ticker] = (
                _relative_error(lots[ticker] - 1, lot_costs[ticker], budget, weights[ticker])
                - _relative_error(lots[ticker], lot_costs[ticker], budget, weights[ticker]))
    # Cancel the least damaging upward roundings until the target fits the budget.
    for ticker in sorted(penalties, key=lambda item: (penalties[item], item)):
        if remaining >= 0:
            break
        lots[ticker] -= 1
        remaining += lot_costs[ticker]
    return {
        ticker: IndexAllocation(
            weight=weight, ideal_lots=ideal[ticker], lots=lots[ticker],
            actual_weight=lots[ticker] * lot_costs[ticker] / budget,
            error=_relative_error(lots[ticker], lot_costs[ticker], budget, weight),
            exclusion_reason='')
        for ticker, weight in weights.items()
    }


def calculate_index_target(config, snapshot, budget):
    """Allocate an affordable capitalization prefix and renormalize after suffix cuts.

    Config is validated by load_index_config; snapshot maps every ticker to an
    IndexQuote. Budget already includes the engine's reserve. Decimal errors
    are compared directly with the configured threshold without corrections.
    """
    if not isinstance(budget, Decimal) or not budget.is_finite() or budget <= 0:
        raise ValueError('index budget must be a positive finite Decimal')
    capitalizations = _capitalizations(config, snapshot)
    candidates = dict(sorted(capitalizations.items(), key=lambda item: (-item[1], item[0])))
    total = sum(capitalizations.values(), Decimal(0))
    min_exclusions = []
    # Apply the minimum once against the full index, before any renormalization.
    for position, ticker in enumerate(list(candidates)):
        if (len(config.instruments) > 1
                and budget * (capitalizations[ticker] / total) < config.min_position_value):
            min_exclusions = list(candidates)[position:]
            for excluded in min_exclusions:
                del candidates[excluded]
            break
    passes = []
    target = TargetPortfolio({}, {})
    while candidates:
        allocations = _allocate_lots(candidates, snapshot, budget)
        passes.append(allocations)
        cut = False
        for ticker, allocation in allocations.items():
            if cut:
                allocation.exclusion_reason = 'prefix_tail'
            elif allocation.lots == 0:
                allocation.exclusion_reason = 'zero_lots'
            elif allocation.ideal_lots < 1 and allocation.error > config.max_lot_weight_error:
                allocation.exclusion_reason = 'weight_error'
            if allocation.exclusion_reason:
                cut = True
                del candidates[ticker]
        # Each unsuccessful pass shortens the prefix; excluded rows never return.
        if len(candidates) == len(allocations):
            target = TargetPortfolio(
                {snapshot[ticker].uid: Decimal(allocation.lots) * snapshot[ticker].lot
                 for ticker, allocation in allocations.items()},
                {snapshot[ticker].uid: snapshot[ticker].price for ticker in allocations})
            break
    return IndexCalculation(target, capitalizations, passes, min_exclusions)


def build_index_target(config, snapshot, budget):
    """Return the engine's target using the same calculation as calibration."""
    return calculate_index_target(config, snapshot, budget).target
