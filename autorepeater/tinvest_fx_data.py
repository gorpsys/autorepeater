"""Read SDK 1.51.0 currency metadata, two-sided books and own nonmargin limits."""
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from t_tech.invest import GetMaxLotsRequest, InstrumentStatus, RequestError
from t_tech.invest.services import Services

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.execution_data import ExecutionDataError, TradeRules
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.tinvest_execution_data import _bool, _count, _text
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


def _pair(item) -> FxPair:
    base = currency_code(item.iso_currency_name)
    if item.nominal is None or currency_code(item.nominal.currency) != base:
        raise ValueError('FX nominal currency does not match ISO base currency')
    return FxPair(item.uid, base, item.currency, _amount(item.nominal, base), item.lot,
                  item.api_trade_available_flag, item.buy_available_flag, item.sell_available_flag)


def _book_price(levels, currency: str) -> Decimal:
    if len(levels) != 1 or _count(levels[0].quantity, 'FX book quantity') == 0:
        raise ValueError('FX order book must contain a positive best level on both sides')
    return _amount(levels[0].price, currency)


class TInvestFxData:
    """No requests on construction, no market/cap cache and no submission API."""

    def __init__(self, client: Services, *, clock: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def list_pairs(self) -> list[FxPair]:
        """Full ISO/nominal identity rather than parsing FindInstrument/ticker strings."""
        response = _read(self._client.instruments.currencies,
                         instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL)
        return [_pair(item) for item in response.instruments]

    def get_quote(self, pair: FxPair) -> FxQuote:
        """Depth one supplies bid/ask; last_price and its timestamp are never substitutes."""
        book = _read(self._client.market_data.get_order_book, instrument_id=pair.uid, depth=1)
        received = self._clock()
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
