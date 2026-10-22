"""Read SDK 1.51.0 currency metadata, two-sided books and own nonmargin limits."""
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from t_tech.invest import GetMaxLotsRequest, InstrumentStatus, RequestError
from t_tech.invest.services import Services

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.execution_data import ExecutionDataError, TradeRules
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.fx_diagnostics import (
    catalog_skipped, metadata_failure, pair_failure, selected_pair,
)
from autorepeater.market_diagnostics import (
    BookTiming, diagnostic_operation, measure_stage, warn_book_failure,
)
from autorepeater.tinvest_validation import (
    sdk_bool as _bool, sdk_count as _count, sdk_text as _text,
)
from autorepeater.tinvest_requests import call_api


def _read(method, **kwargs):
    try:
        return call_api(method, **kwargs)
    except RequestError as error:
        raise ExecutionDataError('FX data read failed') from error


def _amount(value, currency: str) -> Decimal:
    if value is None:
        raise ValueError('FX units/nano are missing')
    return MoneyAmount.from_units_nano(
        currency, getattr(value, 'units', None), getattr(value, 'nano', None)).amount


def _unusable_placeholder(item, nominal: Decimal) -> bool:
    """Only the observed zero-unit noninstrument, never a valid closed-market route."""
    if not (nominal == 0 and isinstance(item.lot, int) and not isinstance(item.lot, bool)
            and item.lot == 0
            and all(flag is False for flag in (
                item.api_trade_available_flag, item.buy_available_flag,
                item.sell_available_flag))):
        return False
    try:
        _text(item.uid, 'FX pair UID')
    except ValueError as error:
        metadata_failure(item, error, 'uid', 'nonempty_uid_without_whitespace')
        raise
    try:
        currency_code(item.currency)
    except ValueError as error:
        metadata_failure(item, error, 'currency', 'three_ascii_letters')
        raise
    return True


def _pair(item) -> FxPair | None:
    try:
        base = currency_code(item.iso_currency_name)
    except ValueError as error:
        metadata_failure(item, error, 'iso_currency_name', 'three_ascii_letters')
        raise
    _check_nominal_currency(item, base)
    try:
        nominal = _amount(item.nominal, base)
    except ValueError as error:
        metadata_failure(item, error, 'nominal',
                         'integer_units_nano_abs_nano_lt_1e9_same_sign')
        raise
    if _unusable_placeholder(item, nominal):
        return None
    try:
        pair = FxPair(item.uid, base, item.currency, nominal, item.lot,
                      item.api_trade_available_flag, item.buy_available_flag,
                      item.sell_available_flag)
    except ValueError as error:
        pair_failure(item, nominal, error)
        raise
    selected_pair(item, nominal)
    return pair


def _check_nominal_currency(item, base):
    if item.nominal is None:
        error = ValueError('FX nominal currency does not match ISO base currency')
        metadata_failure(item, error, 'nominal', 'required_nominal')
        raise error
    try:
        nominal_currency = currency_code(item.nominal.currency)
    except ValueError as error:
        metadata_failure(item, error, 'nominal.currency', 'three_ascii_letters')
        raise
    if nominal_currency != base:
        error = ValueError('FX nominal currency does not match ISO base currency')
        metadata_failure(item, error, 'nominal.currency', 'nominal_currency_matches_iso_base')
        raise error


def _book_price(levels, currency: str) -> Decimal:
    if len(levels) != 1 or _count(levels[0].quantity, 'FX book quantity') == 0:
        raise ValueError('FX order book must contain a positive best level on both sides')
    return _amount(levels[0].price, currency)


class TInvestFxData:
    """No requests on construction, no market/cap cache and no submission API."""

    def __init__(self, client: Services, *, clock: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    @measure_stage('fx.catalog')
    def list_pairs(self) -> list[FxPair]:
        """Full ISO/nominal identity rather than parsing FindInstrument/ticker strings."""
        with measure_stage('fx.catalog.request'):
            response = _read(self._client.instruments.currencies,
                             instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL)
        pairs, skipped = [], []
        omitted = 0
        try:
            for item in response.instruments:
                pair = _pair(item)
                if pair is not None:
                    pairs.append(pair)
                elif len(skipped) < 20:
                    skipped.append(item)
                else:
                    omitted += 1
        finally:
            if skipped:
                catalog_skipped(skipped, omitted)
        return pairs

    def get_quote(self, pair: FxPair) -> FxQuote:
        """Depth one supplies bid/ask; last_price and its timestamp are never substitutes."""
        timing = BookTiming('fx.book', event='fx_book_rejected')
        with diagnostic_operation('fx.book'):
            book = _read(self._client.market_data.get_order_book, instrument_id=pair.uid, depth=1)
        timing.received()
        received = self._clock()
        timing.checked()
        warn_book_failure(pair.uid, book.instrument_uid, book.orderbook_ts, received,
                          timing=timing)
        if book.instrument_uid != pair.uid:
            raise ValueError('FX order book UID mismatch')
        quote = FxQuote(pair, _book_price(book.bids, pair.quote_currency),
                        _book_price(book.asks, pair.quote_currency), book.orderbook_ts, received)
        quote.validate_fresh(received)
        return quote

    def get_trade_rules(self, account_id: str, pair: FxPair) -> TradeRules:
        """Status flags and own limits are broker facts; readiness is read separately."""
        _text(account_id, 'account_id')
        status = _read(self._client.market_data.get_trading_status, instrument_id=pair.uid)
        if status.instrument_uid != pair.uid:
            raise ValueError('FX trading status UID mismatch')
        api = _bool(status.api_trade_available_flag, 'FX API permission')
        bestprice = _bool(status.bestprice_order_available_flag, 'FX BESTPRICE permission')
        caps = _read(self._client.orders.get_max_lots,
                     request=GetMaxLotsRequest(account_id=account_id, instrument_id=pair.uid))
        if currency_code(caps.currency) != pair.quote_currency:
            raise ValueError('FX own caps currency mismatch')
        if (not hasattr(caps.buy_limits, 'buy_money_amount')
                or not hasattr(caps.sell_limits, 'sell_max_lots')):
            raise ValueError('FX own limits are missing')
        money = _amount(caps.buy_limits.buy_money_amount, pair.quote_currency)
        if money < 0:
            raise ValueError('FX own money must be nonnegative')
        return TradeRules(
            pair.lot_size, pair.quote_currency, pair.api_trade_available and api, bestprice,
            money, _count(caps.buy_limits.buy_max_lots, 'FX own buy lots'),
            _count(caps.sell_limits.sell_max_lots, 'FX own sell lots'))
