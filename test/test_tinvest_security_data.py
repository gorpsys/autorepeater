"""Native security books have explicit units, market times and safe read errors."""
import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import RequestError, schemas as sdk
from t_tech.invest.services import InstrumentsService, MarketDataService

from autorepeater.strategy_data import DataAccessError
from autorepeater.fx import build_fx_snapshot
from autorepeater.strategy_data import InstrumentType, PortfolioEntry, PortfolioSnapshot
from autorepeater.tinvest_fx_data import TInvestFxData
from autorepeater.tinvest_security_data import TInvestSecurityData
from autorepeater.valuation import Valuation

D = Decimal
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def quotation(value):
    """Exact SDK quotation without float conversion."""
    return sdk.Quotation(units=value, nano=0)


@pytest.fixture(name='reads')
def fixture_reads():
    """All SDK calls are checked against the unwrapped real services."""
    client = Mock(spec_set=['instruments', 'market_data'])
    for name, service in [('instruments', InstrumentsService), ('market_data', MarketDataService)]:
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    client.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', currency='cny', instrument_type='share'))
    client.market_data.get_order_book.return_value = sdk.GetOrderBookResponse(
        instrument_uid='uid', bids=[sdk.Order(price=quotation(89), quantity=2)],
        asks=[sdk.Order(price=quotation(91), quantity=3)], orderbook_ts=NOW,
        last_price=quotation(999))
    return client, TInvestSecurityData(client, clock=lambda: NOW)


@pytest.mark.parametrize('age', [0, 60, 60.164933, 90, 90.000001, 120, 128.014157, 160, 180])
def test_security_midpoint_ignores_last_trade_and_execution_price(reads, age):
    """Depth-one book of the security is distinct from the FX book."""
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW - timedelta(seconds=age)
    assert data.get_security_quote('uid').estimate_per_piece().amount == D(90)
    client.market_data.get_order_book.assert_called_once_with(instrument_id='uid', depth=1)
    client.market_data.get_last_prices.assert_not_called()


def test_bond_quote_converts_nominal_percent_and_explicit_aci_once(reads):
    """CNY settlement, nominal and ACI are checked independently."""
    client, data = reads
    client.instruments.get_instrument_by.return_value.instrument.instrument_type = 'bond'
    client.instruments.bond_by.return_value = sdk.BondResponse(instrument=sdk.Bond(
        uid='uid', currency='cny', nominal=sdk.MoneyValue(currency='cny', units=1000, nano=0),
        aci_value=sdk.MoneyValue(currency='cny', units=5, nano=1)))
    quote = data.get_security_quote('uid')
    assert quote.estimate_per_piece().amount == D('905.000000001')
    assert quote.currency == 'cny'


def test_cash_iso_identity_does_not_use_pair_quote_currency(reads):
    """CNY/RUB has RUB quote currency but represents CNY cash units."""
    client, data = reads
    client.instruments.currency_by.return_value = sdk.CurrencyResponse(instrument=sdk.Currency(
        uid='uid', iso_currency_name='CNY', currency='rub'))
    assert data.get_cash_currency('uid') == 'cny'
    client.market_data.get_order_book.assert_not_called()


@pytest.mark.parametrize('timestamp', [None, NOW - timedelta(seconds=180, microseconds=1),
                                       NOW - timedelta(seconds=240),
                                       NOW + timedelta(milliseconds=100, microseconds=1),
                                       NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_stale_or_unknown_security_book_rejected(reads, timestamp):
    """Receipt time and last trade time cannot substitute for orderbook_ts."""
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = timestamp
    with pytest.raises(ValueError):
        data.get_security_quote('uid')


@pytest.mark.parametrize('ahead', [timedelta(microseconds=1), timedelta(milliseconds=50),
                                   timedelta(milliseconds=65), timedelta(microseconds=65426),
                                   timedelta(milliseconds=100)])
def test_security_accepts_bounded_market_clock_skew(reads, ahead):
    """One hundred milliseconds inclusive is a market-clock allowance, not a new mark."""
    client, data = reads
    client.market_data.get_order_book.return_value.orderbook_ts = NOW + ahead
    assert data.get_security_quote('uid').estimate_per_piece().amount == D(90)
    assert client.market_data.get_order_book.return_value.orderbook_ts == NOW + ahead
    client.market_data.get_order_book.assert_called_once_with(instrument_id='uid', depth=1)
    client.market_data.get_last_prices.assert_not_called()


def test_security_samples_validation_clock_after_book_response(reads):
    """RPC time cannot consume the permitted clock skew before receipt."""
    client, _ = reads

    def clock():
        client.market_data.get_order_book.assert_called_once_with(instrument_id='uid', depth=1)
        return NOW

    client.market_data.get_order_book.return_value.orderbook_ts = NOW
    assert TInvestSecurityData(client, clock=clock).get_security_quote(
        'uid').estimate_per_piece().amount == D(90)


@pytest.mark.parametrize('field, value', [('bids', []), ('asks', []), ('instrument_uid', 'other')])
def test_bad_book_rejected(reads, field, value):
    """A complete matching two-sided book is required."""
    client, data = reads
    setattr(client.market_data.get_order_book.return_value, field, value)
    with pytest.raises(ValueError):
        data.get_security_quote('uid')


def test_transport_failure_is_safe_and_preserves_cause(reads):
    """No raw broker message is exposed as the neutral error text."""
    client, data = reads
    error = RequestError(StatusCode.DEADLINE_EXCEEDED, 'private broker details', None)
    client.market_data.get_order_book.side_effect = error
    with pytest.raises(DataAccessError, match='security data read failed') as caught:
        data.get_security_quote('uid')
    assert caught.value.__cause__ is error
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('instrument', [None, sdk.Instrument(uid='other')])
def test_missing_or_mismatched_metadata_rejected(reads, instrument):
    """The book cannot repair an unrelated or absent security identity."""
    client, data = reads
    client.instruments.get_instrument_by.return_value.instrument = instrument
    with pytest.raises(ValueError, match='metadata UID'):
        data.get_security_quote('uid')
    client.market_data.get_order_book.assert_not_called()


@pytest.mark.parametrize('field, value', [
    ('currency', 'usd'), ('nominal', None),
    ('nominal', sdk.MoneyValue(currency='usd', units=1000, nano=0)),
    ('aci_value', None), ('aci_value', sdk.MoneyValue(currency='rub', units=5, nano=0)),
])
def test_bond_missing_or_mismatched_native_units_rejected(reads, field, value):
    """Settlement, nominal and ACI never inherit one another's currency or zero."""
    client, data = reads
    client.instruments.get_instrument_by.return_value.instrument.instrument_type = 'bond'
    bond = sdk.Bond(uid='uid', currency='cny',
                    nominal=sdk.MoneyValue(currency='cny', units=1000, nano=0),
                    aci_value=sdk.MoneyValue(currency='cny', units=5, nano=0))
    setattr(bond, field, value)
    client.instruments.bond_by.return_value = sdk.BondResponse(instrument=bond)
    with pytest.raises(ValueError, match='currency mismatch'):
        data.get_security_quote('uid')
    client.market_data.get_order_book.assert_not_called()


def test_unsupported_security_units_rejected(reads):
    """Futures units cannot be treated as money per share."""
    client, data = reads
    client.instruments.get_instrument_by.return_value.instrument.instrument_type = 'futures'
    with pytest.raises(ValueError, match='unsupported security quotation units'):
        data.get_security_quote('uid')
    client.market_data.get_order_book.assert_not_called()


@pytest.mark.parametrize('price', [None, sdk.Quotation(units=-1, nano=0)])
def test_missing_or_negative_book_money_rejected(reads, price):
    """A missing price cannot be replaced by the last trade."""
    client, data = reads
    client.market_data.get_order_book.return_value.bids[0].price = price
    with pytest.raises(ValueError):
        data.get_security_quote('uid')


@pytest.mark.parametrize('quantity', [0, -1, True, '1'])
def test_best_level_quantity_is_a_positive_integer(reads, quantity):
    """Malformed response quantities stay observable after SDK construction."""
    client, data = reads
    client.market_data.get_order_book.return_value.bids[0].quantity = quantity
    with pytest.raises(ValueError, match='positive best level'):
        data.get_security_quote('uid')


def test_separate_security_and_fx_sdk_books_reach_rub_valuation(reads):
    """90 CNY per security times 12 RUB per CNY, independently of broker's 500 RUB."""
    client, security = reads
    client.instruments.currencies.return_value = sdk.CurrenciesResponse(instruments=[
        sdk.Currency(uid='cny-fx', iso_currency_name='CNY', currency='rub', lot=1000,
                     nominal=sdk.MoneyValue(currency='cny', units=10, nano=0),
                     api_trade_available_flag=True, buy_available_flag=True,
                     sell_available_flag=True)])
    security_book = client.market_data.get_order_book.return_value
    fx_book = sdk.GetOrderBookResponse(
        instrument_uid='cny-fx', orderbook_ts=NOW,
        bids=[sdk.Order(price=quotation(110), quantity=1)],
        asks=[sdk.Order(price=quotation(130), quantity=1)])
    client.market_data.get_order_book.side_effect = (
        lambda *, instrument_id, depth: fx_book if instrument_id == 'cny-fx' else security_book)
    fx = build_fx_snapshot(TInvestFxData(client, clock=lambda: NOW), ['cny'], now=NOW)
    value = Valuation(fx, security, clock=lambda: NOW)
    value.begin_pass()
    original = PortfolioEntry('uid', InstrumentType.SHARE, 'rub', D(500), D(2), '')
    entry = value.portfolio(PortfolioSnapshot((original,))).positions[0]
    assert entry.current_price == D(500)
    assert entry.native_price == D(90)
    assert entry.settlement_currency == 'cny'
    assert entry.mark == D(1080)
    assert fx.rate('cny') == D(12)
    assert client.market_data.get_order_book.call_args_list == [
        call(instrument_id='cny-fx', depth=1), call(instrument_id='uid', depth=1)]
    client.market_data.get_last_prices.assert_not_called()
