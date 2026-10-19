"""Read native security midpoints and currency identity without order pricing or effects."""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from t_tech.invest import InstrumentIdType, RequestError
from t_tech.invest.services import Services

from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit, currency_code
from autorepeater.fx_data import ORDER_BOOK_MAX_AGE_SECONDS, market_timestamp
from autorepeater.fx_diagnostics import sdk_metadata_facts
from autorepeater.market_diagnostics import (
    BookTiming, diagnostic_operation, measure_stage, warn_book_failure,
)
from autorepeater.strategy_data import DataAccessError
from autorepeater.tinvest_requests import call_api


def _read(method, **kwargs):
    try:
        return call_api(method, **kwargs)
    except RequestError as error:
        raise DataAccessError('security data read failed') from error


def _amount(value, currency):
    if value is None:
        raise ValueError('security units/nano are missing')
    return MoneyAmount.from_units_nano(currency, getattr(value, 'units', None),
                                       getattr(value, 'nano', None))


def _money(value, currency):
    if value is None or currency_code(value.currency) != currency:
        raise ValueError('security nominal/ACI settlement currency mismatch')
    return _amount(value, currency)


def _level(levels, currency):
    if (len(levels) != 1 or not isinstance(levels[0].quantity, int)
            or isinstance(levels[0].quantity, bool) or levels[0].quantity <= 0):
        raise ValueError('security book requires a positive best level on both sides')
    return _amount(levels[0].price, currency).amount


class TInvestSecurityData:
    """Uses the existing client's unary deadlines and safe neutral read errors."""

    def __init__(self, client: Services, *, clock: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    @measure_stage('security.metadata')
    def _instrument(self, method, uid):
        response = _read(method, id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        if response.instrument is None or response.instrument.uid != uid:
            raise ValueError('security metadata UID mismatch')
        return response.instrument

    def get_cash_currency(self, uid: str) -> str:
        """Portfolio quantity is native units; full ISO metadata supplies their identity."""
        item = self._instrument(self._client.instruments.currency_by, uid)
        return currency_code(item.iso_currency_name)

    def get_security_quote(self, uid: str) -> SecurityQuote:
        """Shares/ETFs quote money; bonds quote clean percent of nominal, plus native ACI."""
        item = self._instrument(self._client.instruments.get_instrument_by, uid)
        currency = currency_code(item.currency)
        nominal = interest = None
        unit = SecurityQuoteUnit.MONEY_PER_PIECE
        if item.instrument_type == 'bond':
            bond = self._instrument(self._client.instruments.bond_by, uid)
            if currency_code(bond.currency) != currency:
                raise ValueError('bond settlement currency mismatch')
            nominal, interest = _money(bond.nominal, currency), _money(bond.aci_value, currency)
            unit = SecurityQuoteUnit.PERCENT_NOMINAL
        elif item.instrument_type not in ('share', 'etf'):
            raise ValueError('unsupported security quotation units')
        timing = BookTiming('security.book', sdk_metadata_facts(item))
        with diagnostic_operation('security.book'):
            book = _read(self._client.market_data.get_order_book, instrument_id=uid, depth=1)
        timing.received()
        now = self._clock()
        timing.checked()
        warn_book_failure(uid, book.instrument_uid, book.orderbook_ts, now,
                          timing=timing)
        market_timestamp(now)
        market_timestamp(book.orderbook_ts)
        if book.instrument_uid != uid or not timedelta(0) <= now - book.orderbook_ts <= timedelta(
                seconds=ORDER_BOOK_MAX_AGE_SECONDS):
            raise ValueError('security book UID mismatch or stale market time')
        quote = SecurityQuote(currency, _level(book.bids, currency), _level(book.asks, currency),
                              unit, nominal, interest)
        quote.estimate_per_piece()
        return quote
