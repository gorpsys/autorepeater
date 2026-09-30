# pylint: disable=R0801, R0913, R0917
"""Tests for the T-Invest implementation of the strategy data port."""
import inspect
from datetime import datetime, timezone
from decimal import Decimal, Overflow, localcontext
from unittest.mock import Mock, call, create_autospec

import pytest
from grpc import StatusCode
from t_tech.invest import FindInstrumentResponse
from t_tech.invest import GetLastPricesResponse
from t_tech.invest import Instrument
from t_tech.invest import InstrumentIdType
from t_tech.invest import InstrumentResponse
from t_tech.invest import InstrumentShort
from t_tech.invest import LastPrice
from t_tech.invest import MoneyValue
from t_tech.invest import PortfolioPosition
from t_tech.invest import PortfolioResponse
from t_tech.invest import PositionData
from t_tech.invest import PositionsMoney
from t_tech.invest import PositionsSecurities
from t_tech.invest import PositionsStreamResponse
from t_tech.invest import Quotation
from t_tech.invest import RequestError
from t_tech.invest.services import InstrumentsService
from t_tech.invest.services import MarketDataService
from t_tech.invest.services import OperationsService
from t_tech.invest.services import OperationsStreamService

from autorepeater.strategy_data import DataAccessError
from autorepeater.strategy_data import InstrumentInfo
from autorepeater.strategy_data import InstrumentMatch
from autorepeater.strategy_data import InstrumentType
from autorepeater.strategy_data import MoneyBlocking
from autorepeater.strategy_data import PortfolioEntry
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_data import PositionEvent
from autorepeater.strategy_data import PriceQuote
from autorepeater.strategy_data import SecurityBlocking
from autorepeater.tinvest_strategy_data import TInvestStrategyData


@pytest.fixture(name='client')
def client_fixture():
    """Expose only the four SDK services used by the read adapter."""
    services = {
        'instruments': InstrumentsService,
        'operations': OperationsService,
        'operations_stream': OperationsStreamService,
        'market_data': MarketDataService,
    }
    client = Mock(spec_set=list(services))
    for name, service in services.items():
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    return client


@pytest.fixture(name='data')
def data_fixture(client):
    """Build the adapter without constructing an SDK client or service."""
    return TInvestStrategyData(client)


def test_get_portfolio_converts_ordered_positions_without_extra_calls(client, data):
    """A portfolio keeps source order and exact per-unit Decimal values."""
    positions = [
        PortfolioPosition(
            instrument_uid='share-uid',
            instrument_type='share',
            current_price=MoneyValue(currency='RUB', units=12, nano=345000001),
            quantity=Quotation(units=-2, nano=-1),
        ),
        PortfolioPosition(
            instrument_uid='currency-uid',
            instrument_type='currency',
            current_price=MoneyValue(currency='USD', units=0, nano=0),
            quantity=Quotation(units=3, nano=140000000),
        ),
        PortfolioPosition(
            instrument_uid='future-uid',
            instrument_type='futures',
            current_price=MoneyValue(currency='RUB', units=1, nano=0),
            quantity=Quotation(units=1, nano=0),
        ),
    ]
    client.operations.get_portfolio.return_value = PortfolioResponse(positions=positions)

    result = data.get_portfolio('account')

    assert result == PortfolioSnapshot(positions=(
        PortfolioEntry(
            uid='share-uid',
            instrument_type=InstrumentType.SHARE,
            currency='RUB',
            current_price=Decimal('12.345000001'),
            quantity=Decimal('-2.000000001'),
            diagnostic_text=str(positions[0]),
        ),
        PortfolioEntry(
            uid='currency-uid',
            instrument_type=InstrumentType.CURRENCY,
            currency='USD',
            current_price=Decimal('0'),
            quantity=Decimal('3.14'),
            diagnostic_text=str(positions[1]),
        ),
        PortfolioEntry(
            uid='future-uid',
            instrument_type=InstrumentType.OTHER,
            currency='RUB',
            current_price=Decimal('1'),
            quantity=Decimal('1'),
            diagnostic_text=str(positions[2]),
        ),
    ))
    assert client.mock_calls == [call.operations.get_portfolio(account_id='account')]


def test_find_instruments_preserves_order_duplicates_and_short_fields(client, data):
    """Search results remain short metadata and are not enriched by UID calls."""
    matches = [
        InstrumentShort(
            uid='uid-2', ticker='DUP', name='Second',
            instrument_type='etf', class_code='TQTF', lot=99),
        InstrumentShort(
            uid='uid-1', ticker='DUP', name='First',
            instrument_type='share', class_code='TQBR', lot=10),
        InstrumentShort(
            uid='uid-2', ticker='DUP', name='Second duplicate',
            instrument_type='bond', class_code='TQOB', lot=1),
    ]
    client.instruments.find_instrument.return_value = FindInstrumentResponse(
        instruments=matches)

    result = data.find_instruments('DUP')

    assert result == [
        InstrumentMatch('uid-2', 'DUP', 'Second', InstrumentType.ETF, 'TQTF'),
        InstrumentMatch('uid-1', 'DUP', 'First', InstrumentType.SHARE, 'TQBR'),
        InstrumentMatch(
            'uid-2', 'DUP', 'Second duplicate', InstrumentType.OTHER, 'TQOB'),
    ]
    assert client.mock_calls == [call.instruments.find_instrument(query='DUP')]
    assert not hasattr(result[0], 'currency')
    assert not hasattr(result[0], 'lot')


def test_find_instruments_accepts_empty_response(client, data):
    """An empty search is business input for a strategy, not an adapter error."""
    client.instruments.find_instrument.return_value = FindInstrumentResponse(
        instruments=[])

    assert data.find_instruments('missing') == []
    client.instruments.find_instrument.assert_called_once_with(query='missing')


def test_get_instrument_uses_uid_query_and_full_metadata(client, data):
    """The full lookup includes lot and currency without another search."""
    client.instruments.get_instrument_by.return_value = InstrumentResponse(
        instrument=Instrument(
            uid='uid', ticker='TEST', name='Test share',
            instrument_type='share', class_code='TQBR', lot=10, currency='RUB'))

    result = data.get_instrument('uid')

    assert result == InstrumentInfo(
        uid='uid', ticker='TEST', name='Test share',
        instrument_type=InstrumentType.SHARE, class_code='TQBR',
        lot=10, currency='RUB')
    assert client.mock_calls == [call.instruments.get_instrument_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')]


def test_get_last_prices_preserves_response_order_duplicates_and_sign(client, data):
    """The adapter converts records but leaves completeness and sign to strategies."""
    quote_time = datetime(2026, 9, 30, 10, 20, tzinfo=timezone.utc)
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(
        last_prices=[
            LastPrice(
                instrument_uid='uid-2', price=Quotation(units=0, nano=0),
                time=quote_time),
            LastPrice(
                instrument_uid='uid-1', price=Quotation(units=-1, nano=-2),
                time=None),
            LastPrice(
                instrument_uid='uid-2', price=Quotation(units=1, nano=1),
                time=quote_time),
        ])

    result = data.get_last_prices(('uid-1', 'uid-2', 'uid-3'))

    assert result == [
        PriceQuote('uid-2', Decimal('0'), quote_time),
        PriceQuote('uid-1', Decimal('-1.000000002'), None),
        PriceQuote('uid-2', Decimal('1.000000001'), quote_time),
    ]
    assert client.mock_calls == [call.market_data.get_last_prices(
        instrument_id=['uid-1', 'uid-2', 'uid-3'])]


def test_get_last_prices_accepts_empty_and_incomplete_response(client, data):
    """Missing requested UIDs are validated by INDEX, not by the adapter."""
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(
        last_prices=[])

    assert data.get_last_prices(['uid-1', 'uid-2']) == []
    client.market_data.get_last_prices.assert_called_once_with(
        instrument_id=['uid-1', 'uid-2'])


def test_position_events_convert_position_and_service_events(client, data):
    """The lazy stream keeps service events and ordered blocking details."""
    position = PositionData(
        account_id='account',
        securities=[PositionsSecurities(blocked=0), PositionsSecurities(blocked=7)],
        money=[
            PositionsMoney(blocked_value=MoneyValue(currency='RUB', units=0, nano=0)),
            PositionsMoney(blocked_value=MoneyValue(currency='USD', units=-1, nano=-1)),
        ],
    )
    position_response = PositionsStreamResponse(position=position)
    service_response = PositionsStreamResponse(position=None)
    client.operations_stream.positions_stream.return_value = iter(
        [position_response, service_response])

    events = data.position_events(('source', 'destination'))

    assert client.operations_stream.positions_stream.call_args_list == [
        call(accounts=['source', 'destination'])]
    assert iter(events) is events
    assert list(events) == [
        PositionEvent(
            has_position=True,
            account_id='account',
            securities=(SecurityBlocking(0), SecurityBlocking(7)),
            money=(MoneyBlocking(Decimal('0')),
                   MoneyBlocking(Decimal('-1.000000001'))),
            diagnostic_text=str(position_response),
        ),
        PositionEvent(
            has_position=False,
            account_id='',
            securities=(),
            money=(),
            diagnostic_text=str(service_response),
        ),
    ]


@pytest.mark.parametrize(
    'method_name, service_name, sdk_method, args, kwargs',
    [
        ('get_portfolio', 'operations', 'get_portfolio', ('account',),
         {'account_id': 'account'}),
        ('find_instruments', 'instruments', 'find_instrument', ('TEST',),
         {'query': 'TEST'}),
        ('get_instrument', 'instruments', 'get_instrument_by', ('uid',),
         {'id_type': InstrumentIdType.INSTRUMENT_ID_TYPE_UID, 'id': 'uid'}),
        ('get_last_prices', 'market_data', 'get_last_prices', (['uid'],),
         {'instrument_id': ['uid']}),
    ],
)
def test_normal_calls_translate_request_error(
        client, data, method_name, service_name, sdk_method, args, kwargs):
    """Every finite read operation exposes transport errors through the own port."""
    error = RequestError(
        StatusCode.UNAVAILABLE, 'read unavailable', ())
    method = getattr(getattr(client, service_name), sdk_method)
    method.side_effect = error

    with pytest.raises(DataAccessError, match='read unavailable') as caught:
        getattr(data, method_name)(*args)

    assert caught.value.__cause__ is error
    method.assert_called_once_with(**kwargs)


def test_position_events_translates_stream_opening_request_error(client, data):
    """A failure while obtaining the SDK iterator is translated immediately."""
    error = RequestError(
        StatusCode.UNAVAILABLE, 'open unavailable', ())
    client.operations_stream.positions_stream.side_effect = error

    with pytest.raises(DataAccessError, match='open unavailable') as caught:
        data.position_events(['account'])

    assert caught.value.__cause__ is error
    client.operations_stream.positions_stream.assert_called_once_with(
        accounts=['account'])


def test_position_events_translates_request_error_during_iteration(client, data):
    """A stream may fail only after yielding one valid event."""
    response = PositionsStreamResponse(position=None)
    error = RequestError(
        StatusCode.UNAVAILABLE, 'next unavailable', ())

    def responses():
        yield response
        raise error

    client.operations_stream.positions_stream.return_value = responses()
    events = data.position_events(['account'])

    assert next(events) == PositionEvent(False, '', (), (), str(response))
    with pytest.raises(DataAccessError, match='next unavailable') as caught:
        next(events)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    'method_name, response',
    [
        ('get_portfolio', PortfolioResponse(positions=[object()])),
        ('find_instruments', FindInstrumentResponse(instruments=[object()])),
        ('get_instrument', object()),
        ('get_last_prices', GetLastPricesResponse(last_prices=[object()])),
    ],
)
def test_programming_errors_are_not_masked_as_data_access_errors(
        client, data, method_name, response):
    """Unexpected object shapes remain visible as programming errors."""
    sdk_methods = {
        'get_portfolio': client.operations.get_portfolio,
        'find_instruments': client.instruments.find_instrument,
        'get_instrument': client.instruments.get_instrument_by,
        'get_last_prices': client.market_data.get_last_prices,
    }
    sdk_methods[method_name].return_value = response
    arguments = {
        'get_portfolio': ('account',),
        'find_instruments': ('query',),
        'get_instrument': ('uid',),
        'get_last_prices': (['uid'],),
    }

    with pytest.raises(AttributeError):
        getattr(data, method_name)(*arguments[method_name])


def test_position_event_programming_error_is_not_masked(client, data):
    """Arbitrary iterator failures are not classified as transport errors."""
    failure = RuntimeError('broken iterator')

    def responses():
        yield from ()
        raise failure

    client.operations_stream.positions_stream.return_value = responses()

    with pytest.raises(RuntimeError, match='broken iterator') as caught:
        next(data.position_events(['account']))
    assert caught.value is failure


def test_missing_full_instrument_is_contextual_value_error(client, data):
    """A successful response without metadata is invalid data, not transport failure."""
    client.instruments.get_instrument_by.return_value = InstrumentResponse(
        instrument=None)

    with pytest.raises(ValueError, match=r'uid.*instrument') as caught:
        data.get_instrument('uid')

    assert not isinstance(caught.value, DataAccessError)


def test_missing_quotation_price_is_contextual_value_error(client, data):
    """A quote record must contain a price before it enters strategy code."""
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(
        last_prices=[LastPrice(instrument_uid='uid', price=None)])

    with pytest.raises(ValueError, match=r'uid.*price') as caught:
        data.get_last_prices(['uid'])

    assert not isinstance(caught.value, DataAccessError)


@pytest.mark.parametrize(
    'field, invalid_value',
    [
        ('units', None),
        ('nano', 'invalid'),
        ('units', Decimal('NaN')),
        ('nano', Decimal('Infinity')),
        ('units', True),
    ],
)
def test_invalid_quote_parts_are_contextual_value_error(
        client, data, field, invalid_value):
    """Invalid units/nano values identify the quote field and UID."""
    quotation = Quotation(units=1, nano=2)
    setattr(quotation, field, invalid_value)
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(
        last_prices=[LastPrice(instrument_uid='quote-uid', price=quotation)])

    with pytest.raises(
            ValueError, match=rf'quote-uid.*price\.{field}') as caught:
        data.get_last_prices(['quote-uid'])

    assert not isinstance(caught.value, DataAccessError)


@pytest.mark.parametrize(
    'value_name, field_name, invalid_value',
    [
        ('current_price', 'units', object()),
        ('quantity', 'nano', Decimal('-Infinity')),
    ],
)
def test_invalid_portfolio_parts_are_contextual_value_error(
        client, data, value_name, field_name, invalid_value):
    """Portfolio conversion failures identify the position and value."""
    position = PortfolioPosition(
        instrument_uid='portfolio-uid', instrument_type='share',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=1, nano=0))
    setattr(getattr(position, value_name), field_name, invalid_value)
    client.operations.get_portfolio.return_value = PortfolioResponse(
        positions=[position])

    with pytest.raises(
            ValueError, match=rf'portfolio-uid.*{value_name}') as caught:
        data.get_portfolio('account')

    assert not isinstance(caught.value, DataAccessError)


@pytest.mark.parametrize('value_name', ['current_price', 'quantity'])
def test_missing_portfolio_value_is_contextual_value_error(client, data, value_name):
    """Missing numeric DTOs identify the position UID and field before use."""
    position = PortfolioPosition(
        instrument_uid='portfolio-uid', instrument_type='share',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=1, nano=0))
    setattr(position, value_name, None)
    client.operations.get_portfolio.return_value = PortfolioResponse(
        positions=[position])

    with pytest.raises(ValueError) as caught:
        data.get_portfolio('account')

    assert str(caught.value) == f'portfolio position portfolio-uid {value_name} is missing'
    assert not isinstance(caught.value, DataAccessError)
    assert client.mock_calls == [call.operations.get_portfolio(account_id='account')]


def test_invalid_money_blocking_is_contextual_value_error(client, data):
    """Money blocking values are converted with the same finite-number rules."""
    position = PositionData(
        account_id='account', securities=[],
        money=[PositionsMoney(blocked_value=MoneyValue(
            currency='RUB', units=0, nano=Decimal('NaN')))])
    client.operations_stream.positions_stream.return_value = iter([
        PositionsStreamResponse(position=position)])

    with pytest.raises(ValueError, match=r'account.*blocked_value\.nano') as caught:
        next(data.position_events(['account']))

    assert not isinstance(caught.value, DataAccessError)


def test_missing_money_blocking_is_contextual_value_error(client, data):
    """A missing units/nano value does not become an AttributeError or zero."""
    position = PositionData(
        account_id='account', securities=[],
        money=[PositionsMoney(blocked_value=None)])
    client.operations_stream.positions_stream.return_value = iter([
        PositionsStreamResponse(position=position)])

    with pytest.raises(ValueError, match=r'account.*blocked_value.*missing'):
        next(data.position_events(['account']))


def test_nonfinite_decimal_result_is_contextual_value_error(client, data):
    """A non-trapping Decimal context cannot leak an infinite converted value."""
    quotation = Quotation(units=0, nano=0)
    maximum = Decimal('9.999999999999999999999999999E+999999')
    quotation.units = maximum
    quotation.nano = Decimal('9.999999999999999999999999999E+1000008')
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(
        last_prices=[LastPrice(instrument_uid='quote-uid', price=quotation)])

    with localcontext() as context:
        context.traps[Overflow] = False
        with pytest.raises(ValueError, match=r'quote-uid.*price.*not finite'):
            data.get_last_prices(['quote-uid'])


def test_public_adapter_surface_is_exactly_the_read_port(client):
    """Strategies cannot trade or escape through a client/proxy operation."""
    public_methods = {
        name for name, value in inspect.getmembers(
            TInvestStrategyData, predicate=inspect.isfunction)
        if not name.startswith('_')
    }
    adapter = TInvestStrategyData(client)

    assert public_methods == {
        'get_portfolio', 'find_instruments', 'get_instrument',
        'get_last_prices', 'position_events'}
    for forbidden in ('orders', 'users', 'client', 'proxy', 'post_order'):
        assert not hasattr(adapter, forbidden)
    assert '__getattr__' not in TInvestStrategyData.__dict__
