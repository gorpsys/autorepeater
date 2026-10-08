# pylint: disable=R0913, R0917, too-many-lines
"""tests"""
import hashlib
import inspect
import json
import logging
import runpy
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, DivisionByZero
from pathlib import Path
from unittest.mock import ANY, Mock, call, create_autospec, patch
from zipfile import ZipFile

import pytest
from grpc import StatusCode

from t_tech.invest import MoneyValue
from t_tech.invest import Instrument
from t_tech.invest import InstrumentStatus
from t_tech.invest import Share, SharesResponse
from t_tech.invest import PortfolioPosition
from t_tech.invest import Quotation
from t_tech.invest import PositionData
from t_tech.invest import PositionsStreamResponse
from t_tech.invest import PositionsMoney
from t_tech.invest import PositionsSecurities
from t_tech.invest import OrderDirection
from t_tech.invest import OrderType
from t_tech.invest import FindInstrumentResponse
from t_tech.invest import InstrumentResponse
from t_tech.invest import PortfolioResponse
from t_tech.invest import InstrumentShort
from t_tech.invest import GetAccountsResponse
from t_tech.invest import Account
from t_tech.invest import AccountType
from t_tech.invest import AccountStatus
from t_tech.invest import InstrumentIdType
from t_tech.invest import GetLastPricesResponse
from t_tech.invest import LastPrice
from t_tech.invest import SecurityTradingStatus
from t_tech.invest import PostOrderResponse
from t_tech.invest import RequestError
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.services import InstrumentsService
from t_tech.invest.services import OperationsService
from t_tech.invest.services import UsersService
from t_tech.invest.services import OrdersService
from t_tech.invest.services import OperationsStreamService
from t_tech.invest.services import MarketDataService

from autorepeater.index_config import AllocationDriftRange
from autorepeater.account_strategy import AccountStrategy
from autorepeater.account_strategy import PreparedAccountSource
from autorepeater.account_config import AccountConfig
from autorepeater.index_config import IndexConfig, IndexInstrument
from autorepeater.index_config import load_index_config, load_index_configs
from autorepeater.index_strategy import IndexQuote, IndexStrategy
from autorepeater.index_strategy import build_index_target, calculate_index_target
from autorepeater.money import blocked_to_string
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_decimal_price
from autorepeater.money import currency_to_string
from autorepeater.money import get_quantity_position
from autorepeater.money import money_to_string
from autorepeater.money import no_money_to_string
from autorepeater.portfolio import get_portfolio
from autorepeater.portfolio import TargetPortfolio
from autorepeater.portfolio import validate_target
from autorepeater.repeater import AutoRepeater
from autorepeater.execution_data import ExecutionData
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.tinvest_execution_data import TInvestExecutionData
from autorepeater.reporting import GetInstrumentException
from autorepeater.strategy_contract import AlgorithmDefinition, Strategy
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
from autorepeater.strategy_data import StrategyData
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from autorepeater.triggers import check_triggers
from autorepeater import logging_config
from autorepeater import index_config as index_config_module
from autorepeater import reporting
from autorepeater import runner as runner_module
from autorepeater import serverless
from autorepeater import strategies
from autorepeater.runner import RunnerParams
import handler as cloud_entrypoint
import main as cli
from scripts import check_imoex_strategy as calibration

pytestmark = pytest.mark.usefixtures('named_accounts')


def account_strategy(src, reserve='0.01'):
    """Construct a strategy from explicit settings without filesystem I/O."""
    return AccountStrategy(PreparedAccountSource(
        src, AccountConfig(src, src, Decimal(reserve), Decimal('0.0092'))))


class TestException(Exception):
    """TestException исключение для остановки бесконечного цикла в тесте mainflow"""


@pytest.mark.parametrize(
    'currency, units, nano, expected',
    [
        ('RUB', 1, 500000000, 'RUB - 1.5'),
        ('RUB', -1, -500000000, 'RUB - -1.5'),
        ('RUB', 0, 0, 'RUB - 0.0'),
        ('USD', 0, 999999999, 'USD - 0.999999999'),  # Максимальное значение nano
        ('EUR', 999999999, 0, 'EUR - 999999999.0'),  # Большое значение units
        ('RUB', 0, 1, 'RUB - 0.000000001'),  # Минимальное значение nano
    ],
    ids=[
        'positive_value',
        'negative_value',
        'zero_value',
        'max_nano',
        'max_units',
        'min_nano'
    ]
)
def test_money_to_string(currency, units, nano, expected):
    """money to string"""
    money = MoneyValue(currency=currency, units=units, nano=nano)
    result = money_to_string(money)

    assert result == expected


@pytest.mark.parametrize(
    'currency, units, nano, expected',
    [
        ('RUB', 1, 500000000, 'blocked RUB - 1.5'),
        ('RUB', -1, -500000000, 'blocked RUB - -1.5'),
        ('RUB', 0, 0, 'blocked RUB - 0.0'),
        # Максимальное значение nano
        ('USD', 0, 999999999, 'blocked USD - 0.999999999'),
        # Добавляем .0 для целого числа
        ('EUR', 999999999, 0, 'blocked EUR - 999999999.0'),
        ('RUB', 0, 1, 'blocked RUB - 0.000000001'),  # Минимальное значение nano
    ],
    ids=[
        'positive_blocked',
        'negative_blocked',
        'zero_blocked',
        'max_nano_blocked',
        'max_units_blocked',
        'min_nano_blocked'
    ]
)
def test_blocked_to_string(currency, units, nano, expected):
    """blocked money to string"""
    money = MoneyValue(currency=currency, units=units, nano=nano)
    result = blocked_to_string(money)

    assert result == expected


@pytest.mark.parametrize(
    'instrument, expected',
    [
        (Instrument(name='fake company', ticker='FKC'), 'fake company(FKC)'),
        (
            Instrument(name='FinEx Акции американских компаний',
                       ticker='IE00BD3QHZ91'),
            'FinEx Акции американских компаний(IE00BD3QHZ91)',
        ),
        (Instrument(name='', ticker=''), '()'),  # Пустые значения
        (Instrument(name='Company', ticker=''), 'Company()'),  # Пустой тикер
        (Instrument(name='', ticker='TICK'), '(TICK)'),  # Пустое название
        (Instrument(name='Company & Co.', ticker='C&C'),
         'Company & Co.(C&C)'),  # Специальные символы
    ],
    ids=[
        'simple_company',
        'complex_company_name',
        'empty_values',
        'empty_ticker',
        'empty_name',
        'special_chars'
    ]
)
def test_no_money_to_string(instrument, expected):
    """instrument to string"""
    result = no_money_to_string(instrument)

    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, Decimal('3.0')),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, Decimal('-2.25')),
        (MoneyValue('RUB', 0, 0), 5, 500000000, Decimal('0')),
        (MoneyValue('EUR', 999999999, 0), 999999999, 0, Decimal(
            '999999998000000001')),  # Максимальные значения
    ],
    ids=[
        'positive_values',
        'negative_values',
        'zero_money',
        'max_values'
    ]
)
def test_currency_to_decimal(money, quantity_units, quantity_nano, expected):
    """currency to decimal"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_decimal(position)
    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, Decimal('1.5')),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, Decimal('-1.5')),
        (MoneyValue('RUB', 0, 0), 5, 500000000, Decimal('0')),
        (MoneyValue('EUR', 999999999, 0), 999999999, 0,
         Decimal('999999999')),  # Максимальные значения
    ],
    ids=[
        'positive_price',
        'negative_price',
        'zero_price',
        'max_price'
    ]
)
def test_currency_to_decimal_price(
        money,
        quantity_units,
        quantity_nano,
        expected):
    """currency price to decimal"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_decimal_price(position)
    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, 'RUB - 3.0'),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, 'RUB - -2.25'),
        (MoneyValue('RUB', 0, 0), 5, 500000000, 'RUB - 0.0'),
    ],
    ids=[
        'positive_string',
        'negative_string',
        'zero_string'
    ]
)
def test_currency_to_string(money, quantity_units, quantity_nano, expected):
    """currency to string"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_string(position)

    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, Decimal('2.0')),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, Decimal('1.5')),
        (MoneyValue('RUB', 0, 0), 5, 500000000, Decimal('5.5')),
        (MoneyValue('EUR', 999999999, 0), 999999999, 0,
         Decimal('999999999.0')),  # Максимальные значения
    ],
    ids=[
        'positive_quantity',
        'negative_quantity',
        'zero_quantity',
        'max_quantity'
    ]
)
def test_get_quantity_position(money, quantity_units, quantity_nano, expected):
    """quantity position as Decimal"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = get_quantity_position(position)
    assert result == expected


@pytest.mark.parametrize(
    'account_id, position_securities, position_money, expected',
    [
        ('1', (), (), False),
        ('2', (), (MoneyBlocking(Decimal('1')),), False),
        ('1', (SecurityBlocking(0),), (), True),
        ('2', (), (MoneyBlocking(Decimal('0')),), True),
        ('3', (), (), False),
        ('1', (SecurityBlocking(1),), (), False),
        ('2', (), (MoneyBlocking(Decimal('0.000000001')),), False),
        ('1', (SecurityBlocking(0), SecurityBlocking(1)), (), False),
    ],
    ids=[
        'empty_positions',
        'blocked_money',
        'unblocked_securities',
        'unblocked_money',
        'unknown_account',
        'blocked_securities',
        'partially_blocked_money',
        'mixed_blocked_securities'
    ]
)
def test_check_triggers(
        account_id,
        position_money,
        position_securities,
        expected):
    """check triggers"""
    src_account = '1'
    dst_account = '2'
    event = PositionEvent(
        has_position=True,
        account_id=account_id,
        money=position_money,
        securities=position_securities,
        diagnostic_text='event',
    )
    result = check_triggers(event, src_account, dst_account)

    assert result == expected


def test_account_strategy_uses_own_data_port_and_models(caplog):
    """Account calculations and output do not depend on SDK DTO fields."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((
        PortfolioEntry('share', InstrumentType.SHARE, 'RUB', Decimal('4'),
                       Decimal('10.5'), 'share diagnostic'),
        PortfolioEntry('cash', InstrumentType.CURRENCY, 'RUB', Decimal('1'),
                       Decimal('999'), 'cash diagnostic'),
    ))
    data.find_instruments.return_value = [InstrumentMatch(
        'share', 'SHR', 'share1', InstrumentType.SHARE, 'TQBR')]

    with caplog.at_level(logging.INFO, logger=logging_config.LOGGER_NAME):
        snapshot = account_strategy('4').load_snapshot(data)
    target = account_strategy('4', reserve='0').build_target(snapshot, Decimal('21'))

    assert target == TargetPortfolio(
        {'share': Decimal('5.25')}, {'share': Decimal('4')})
    assert data.mock_calls == [call.get_portfolio('4')]
    assert caplog.messages == [
        'src account', 'share - 10.5 - RUB - 42.0',
        'RUB - 999.0', 'total: 42.000000000',
    ]


@pytest.mark.parametrize('instrument_type', ['share', 'etf', 'currency', 'bond'])
def test_strategy_position_output_matches_sdk_path(client, instrument_type):
    """Own models preserve formatted values and the display query sequence."""
    position = PortfolioPosition(
        instrument_uid='1', instrument_type=instrument_type,
        current_price=MoneyValue('RUB', 3, 1), quantity=Quotation(-2, -1))
    client.operations.get_portfolio.side_effect = [PortfolioResponse(positions=[position])]
    data = TInvestStrategyData(client)
    entry = data.get_portfolio('4').positions[0]
    client.reset_mock()

    expected = reporting.postiton_to_string(client, position)
    sdk_calls = client.mock_calls[:]
    client.reset_mock()

    rendered = reporting.strategy_position_to_string(entry)
    assert rendered == (expected.replace('share1(SHR)', '1')
                        if instrument_type in ('share', 'etf') else expected)
    assert client.mock_calls == []
    assert sdk_calls == ([call.instruments.find_instrument(query='1')]
                         if instrument_type in ('share', 'etf') else [])


@pytest.mark.parametrize('count', [0, 2])
@pytest.mark.parametrize('price', [Decimal('1'), Decimal('1e28')])
def test_strategy_position_requires_unique_display_match(count, price):
    """Neutral reporting preserves the existing lookup error without extra reads."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = [InstrumentMatch(
        'uid', 'SHR', 'Share', InstrumentType.SHARE, 'TQBR')] * count
    position = PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', price,
                              Decimal('2'), 'diagnostic')

    assert reporting.strategy_position_to_string(position).startswith('uid - 2.0 - RUB - ')
    assert data.mock_calls == []


def test_strategy_position_unknown_type_uses_diagnostic_without_arithmetic():
    """Unknown instruments keep their diagnostic even outside nano-format precision."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    position = PortfolioEntry('uid', InstrumentType.OTHER, 'RUB', Decimal('1e28'),
                              Decimal('1'), 'unknown instrument diagnostic')

    assert reporting.strategy_position_to_string(position) == position.diagnostic_text
    assert data.mock_calls == []


def test_account_strategy_quantizes_each_position_before_sum(client):
    """Two sub-nano products round individually rather than after summing."""
    client.operations.get_portfolio.side_effect = [PortfolioResponse(positions=[
        PortfolioPosition(
            instrument_uid=uid, instrument_type='share',
            current_price=MoneyValue('RUB', 0, 1), quantity=Quotation(0, 600000000))
        for uid in ('1', '2')])]
    strategy = account_strategy('4', reserve='0')

    snapshot = strategy.load_snapshot(TInvestStrategyData(client))

    assert snapshot[1] == Decimal('0.000000002')
    assert strategy.build_target(snapshot, Decimal('0.000000002')) == TargetPortfolio(
        {'1': Decimal('0.6'), '2': Decimal('0.6')},
        {'1': Decimal('0.000000001'), '2': Decimal('0.000000001')})
    assert client.mock_calls == [call.operations.get_portfolio(account_id='4')]


def test_index_strategy_uses_own_data_port_and_preserves_query_order(index_sdk_config):
    """Index resolution remains business logic over SDK-independent metadata."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    ticker = index_sdk_config.instruments[0].ticker
    data.find_instruments.return_value = [InstrumentMatch(
        'uid', ticker, 'index name', InstrumentType.SHARE, 'TQBR')]
    data.get_instrument.return_value = InstrumentInfo(
        'uid', ticker, 'index name', InstrumentType.SHARE, 'TQBR', 10, 'RUB', True)
    quote_time = datetime(2026, 9, 30, tzinfo=timezone.utc)
    data.get_last_prices.return_value = [PriceQuote('uid', Decimal('12.5'), quote_time)]
    config = replace(index_sdk_config, instruments=(index_sdk_config.instruments[0],))

    snapshot = IndexStrategy(config).load_snapshot(data)

    assert snapshot == {
        ticker: IndexQuote('uid', Decimal('12.5'), 10, currency='RUB', time=quote_time)}
    assert data.mock_calls == [
        call.find_instruments(ticker),
        call.get_instrument('uid'),
        call.get_last_prices(['uid']),
    ]


@pytest.fixture(name='client')
def client_tinvest():
    """SDK services enforce signatures; response data stays explicit."""
    services = {
        'instruments': InstrumentsService,
        'operations': OperationsService,
        'users': UsersService,
        'orders': OrdersService,
        'operations_stream': OperationsStreamService,
        'market_data': MarketDataService,
    }
    client = Mock(spec_set=list(services))
    for name, service in services.items():
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))

    instrument_names = {'1': ('share1', 'SHR'), '2': ('etf2', 'ETF')}
    search_results = {
        uid: FindInstrumentResponse(
            instruments=[InstrumentShort(name=name, ticker=ticker)])
        for uid, (name, ticker) in instrument_names.items()
    }
    instruments = {
        uid: InstrumentResponse(instrument=Instrument(
            name=name,
            ticker=ticker,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
            lot=1))
        for uid, (name, ticker) in instrument_names.items()
    }
    client.instruments.find_instrument.side_effect = lambda **kwargs: search_results.get(
        kwargs['query'], FindInstrumentResponse(instruments=[]))
    client.instruments.get_instrument_by.side_effect = lambda **kwargs: instruments[kwargs['id']]

    portfolios = {
        '1': PortfolioResponse(positions=[
            PortfolioPosition(
                instrument_type='currency',
                current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                quantity=Quotation(units=2, nano=0))]),
        '4': PortfolioResponse(positions=[
            PortfolioPosition(
                instrument_type='share',
                instrument_uid='1',
                current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                quantity=Quotation(units=2, nano=0))]),
        '5': PortfolioResponse(positions=[
            PortfolioPosition(
                instrument_type='currency',
                current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                quantity=Quotation(units=2, nano=0))]),
    }
    client.operations.get_portfolio.side_effect = lambda **kwargs: portfolios.get(
        kwargs['account_id'], PortfolioResponse(positions=[]))
    client.users.get_accounts.return_value = GetAccountsResponse(accounts=[
        Account(
            id='1',
            type=AccountType.ACCOUNT_TYPE_TINKOFF,
            name='account name',
            status=AccountStatus.ACCOUNT_STATUS_OPEN),
        Account(
            id='2',
            type=AccountType.ACCOUNT_TYPE_TINKOFF,
            name='account name',
            status=AccountStatus.ACCOUNT_STATUS_OPEN),
    ])
    client.orders.post_order.return_value = PostOrderResponse()
    client.operations_stream.positions_stream.side_effect = [
        iter(()),
        iter(()),
        RequestError(code='1', details='details', metadata='metadata'),
        TestException(),
    ]
    return client


@pytest.fixture(name='auto_repeater')
def auto_repeater_fixture(client):
    """auto_repeater_fixture - фикстура создаёт и возвращает основной класс передав ему клиента"""
    data = TInvestStrategyData(client)
    return AutoRepeater(account_strategy('4'), data, TInvestExecutionData(client, data),
                        TInvestOrderExecutor(client))


@pytest.mark.parametrize(
    'service_name, method_name, kwargs',
    [
        ('instruments', 'find_instrument', {'query': '1'}),
        ('instruments', 'get_instrument_by', {
            'id_type': InstrumentIdType.INSTRUMENT_ID_TYPE_UID, 'id': '1'}),
        ('operations', 'get_portfolio', {'account_id': '4'}),
        ('users', 'get_accounts', {}),
        ('orders', 'post_order', {
            'instrument_id': '1', 'quantity': 2,
            'direction': OrderDirection.ORDER_DIRECTION_BUY,
            'account_id': '5', 'order_type': OrderType.ORDER_TYPE_BESTPRICE}),
        ('operations_stream', 'positions_stream', {'accounts': ['4', '5']}),
    ],
)
def test_client_service_contract(client, service_name, method_name, kwargs):
    """The actual fixture accepts SDK calls and rejects misspelled API names."""
    service = getattr(client, service_name)
    method = getattr(service, method_name)
    assert method(**kwargs) is not None
    method.assert_called_once_with(**kwargs)

    with pytest.raises(TypeError, match='unexpected keyword argument'):
        method(**kwargs, unknown_argument=True)
    method.assert_called_once_with(**kwargs)
    with pytest.raises(AttributeError):
        getattr(service, 'unknown_method')
    with pytest.raises(AttributeError):
        service.unknown_method = Mock()


def test_client_unknown_service(client):
    """The client container only permits the five configured services."""
    with pytest.raises(AttributeError):
        getattr(client, 'unknown_service')
    with pytest.raises(AttributeError):
        client.unknown_service = Mock()


def test_client_empty_portfolio(client):
    """An account without configured positions retains the empty fallback."""
    response = client.operations.get_portfolio(account_id='unknown')
    assert isinstance(response, PortfolioResponse)
    assert response.positions == []


def test_set_debug(auto_repeater):
    """test_set_debug"""
    # Проверка включения отладки
    auto_repeater.set_debug(True)
    assert auto_repeater.debug is True

    # Проверка выключения отладки
    auto_repeater.set_debug(False)
    assert auto_repeater.debug is False

    # Проверка повторного включения
    auto_repeater.set_debug(True)
    assert auto_repeater.debug is True

    # Проверка с невалидными значениями
    with pytest.raises(TypeError):
        auto_repeater.set_debug(1)  # Должно быть bool
    with pytest.raises(TypeError):
        auto_repeater.set_debug("True")  # Должно быть bool


@pytest.mark.parametrize(
    'instrument_type, price, quantity, uid, expected',
    [
        (
            "currency",
            MoneyValue(currency="USD", units=100, nano=0),
            Quotation(units=1, nano=0),
            "",
            "USD - 100.0"
        ),
        (
            "share",
            MoneyValue(currency="USD", units=100, nano=0),
            Quotation(units=2, nano=0),
            "1",
            "share1(SHR) - 2.0 - USD - 200.0"
        ),
        (
            "currency",
            MoneyValue(currency="RUB", units=0, nano=999999999),
            Quotation(units=0, nano=1),
            "",
            "RUB - 0.000000001"  # Минимальные значения
        ),
        (
            "share",
            MoneyValue(currency="EUR", units=999999999, nano=0),
            Quotation(units=999999999, nano=0),
            "2",
            # Максимальные значения
            "etf2(ETF) - 999999999.0 - EUR - 999999998000000001.0"
        ),
        (
            "currency",
            MoneyValue(currency="RUB", units=-100, nano=-500000000),
            Quotation(units=-2, nano=0),
            "",
            "RUB - 201.0"  # Отрицательные значения: -100.5 * -2 = 201.0
        ),
    ],
    ids=[
        'simple_currency',
        'simple_share',
        'min_values',
        'max_values',
        'negative_values'
    ]
)
def test_postiton_to_string(
        client,
        instrument_type,
        price,
        quantity,
        uid,
        expected):
    """test_postiton_to_string"""
    position = PortfolioPosition(
        instrument_type=instrument_type,
        current_price=price,
        quantity=quantity,
        instrument_uid=uid,
    )
    result = reporting.postiton_to_string(client, position)
    assert result == expected
    assert client.mock_calls == ([call.instruments.find_instrument(query=uid)]
                                 if instrument_type in ['share', 'etf'] else [])


def test_get_instrument(client):
    """test_get_instrument"""
    instrument_id = "1"
    instrument = InstrumentShort(name='share1', ticker='SHR')
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = FindInstrumentResponse(
        instruments=[instrument])
    result = reporting.get_instrument(client, instrument_id)
    assert result is instrument
    client.instruments.find_instrument.assert_called_once_with(query='1')


@pytest.mark.parametrize('instruments', [[], [InstrumentShort(), InstrumentShort()]])
def test_get_instrument_fail(client, instruments):
    """test_get_instrument_fail"""
    instrument_id = "none_id"
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = FindInstrumentResponse(
        instruments=instruments)
    with pytest.raises(GetInstrumentException, match='error get instrument'):
        reporting.get_instrument(client, instrument_id)
    client.instruments.find_instrument.assert_called_once_with(query='none_id')


def test_account_strategy_basic_target(client):
    """The account strategy preserves source data and scales it by the budget ratio."""
    strategy = account_strategy('4')
    positions, total = strategy.load_snapshot(TInvestStrategyData(client))
    assert len(positions) == 1
    position = positions['1']
    assert position.uid == '1'
    assert position.instrument_type == InstrumentType.SHARE
    assert position.currency == 'RUB'
    assert position.current_price == Decimal('1.2')
    assert position.quantity == Decimal('2')
    assert total == Decimal('2.4')
    target = strategy.build_target((positions, total), Decimal('2.4'))
    assert target.quantities == {'1': Decimal('1.98')}
    assert target.prices == {'1': Decimal('1.2')}


@pytest.mark.parametrize('flag', ['-r', '--reserve'])
def test_cli_rejects_removed_reserve(flag, monkeypatch):
    """Removed overrides are rejected before source preparation or client creation."""
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', 'ACCOUNT', '-s', '4', flag, '0'])
    with patch.object(cli, 'prepare_strategy') as prepare, patch.object(cli, 'Runner') as runner:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        prepare.assert_not_called()
        runner.assert_not_called()


def test_calibration_rejects_removed_reserve():
    """The calibration override is rejected before any SDK work."""
    with patch.object(calibration, 'Client') as client:
        with pytest.raises(SystemExit) as error:
            calibration.main(['--reserve', '0'])
        assert error.value.code == 2
        client.assert_not_called()


@pytest.fixture(name='fractional_portfolios')
def fractional_portfolios_fixture(client):
    """Source cash is ignored; destination cash and reserve yield a 0.75 ratio."""
    src_positions = {
        '1': PortfolioPosition(
            instrument_type='share', instrument_uid='1',
            current_price=MoneyValue(currency='RUB', units=4, nano=0),
            quantity=Quotation(units=10, nano=500000000)),
        '2': PortfolioPosition(
            instrument_type='etf', instrument_uid='2',
            current_price=MoneyValue(currency='RUB', units=8, nano=0),
            quantity=Quotation(units=20, nano=250000000)),
    }
    dst_positions = {
        '1': PortfolioPosition(
            instrument_type='share', instrument_uid='1',
            current_price=MoneyValue(currency='RUB', units=3, nano=0),
            quantity=Quotation(units=10, nano=250000000)),
        '2': PortfolioPosition(
            instrument_type='etf', instrument_uid='2',
            current_price=MoneyValue(currency='RUB', units=7, nano=0),
            quantity=Quotation(units=1, nano=125000000)),
    }
    client.operations.get_portfolio.side_effect = [
        PortfolioResponse(positions=[*src_positions.values(), PortfolioPosition(
            instrument_type='currency', instrument_uid='cash',
            current_price=MoneyValue(currency='RUB', units=1, nano=0),
            quantity=Quotation(units=999, nano=0))]),
        PortfolioResponse(positions=[*dst_positions.values(), PortfolioPosition(
            instrument_type='currency', instrument_uid='cash',
            current_price=MoneyValue(currency='RUB', units=1, nano=0),
            quantity=Quotation(units=131, nano=375000000))]),
    ]
    instruments = {
        uid: InstrumentResponse(instrument=Instrument(
            uid=uid, name=f'instrument {uid}', ticker=f'TEST{uid}', lot=lot,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
        for uid, lot in [('1', 2), ('2', 5)]
    }
    client.instruments.get_instrument_by.side_effect = lambda **kwargs: instruments[kwargs['id']]
    return src_positions, dst_positions


@pytest.fixture(name='target_strategy')
def target_strategy_fixture():
    """Only the strategy contract exists; the snapshot cannot be unpacked."""
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    strategy.load_snapshot.return_value = object()
    strategy.event_accounts.return_value = ('5',)
    strategy.should_rebalance.return_value = True
    return strategy


@pytest.mark.parametrize('accounts', [
    None, '5', ['5'], (), ('',), (' ',), ('5 ',), ('a\tb',), (None,), (5,),
    (True,), ('5', '5'), ('5', []),
])
def test_mainflow_rejects_invalid_event_accounts_before_stream(target_strategy, accounts):
    """The common consumer rejects malformed declarations without opening a stream."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.position_events.side_effect = AssertionError('unexpected subscription')
    target_strategy.event_accounts.return_value = accounts
    engine = AutoRepeater(
        target_strategy, data, create_autospec(ExecutionData, instance=True), Mock())
    with patch.object(engine, 'sync_accounts', autospec=True) as sync, \
            pytest.raises(ValueError, match='event_accounts'):
        engine.mainflow('5')
    sync.assert_called_once_with('5')
    target_strategy.event_accounts.assert_called_once_with('5')
    target_strategy.should_rebalance.assert_not_called()
    assert data.mock_calls == []


@pytest.mark.parametrize('decision', [None, 0, 1, '', 'yes', [], object()])
def test_mainflow_rejects_non_bool_decision_without_retry(target_strategy, decision):
    """Truthy and falsy non-bools cannot silently change the rebalance decision."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    event = PositionEvent(False, '', (), (), 'ping')
    data.position_events.side_effect = [iter((event,)), AssertionError('unexpected resubscription')]
    target_strategy.should_rebalance.return_value = decision
    engine = AutoRepeater(
        target_strategy, data, create_autospec(ExecutionData, instance=True), Mock())
    with patch.object(engine, 'sync_accounts', autospec=True) as sync, \
            pytest.raises(ValueError, match='should_rebalance.*bool'):
        engine.mainflow('5')
    sync.assert_called_once_with('5')
    target_strategy.event_accounts.assert_called_once_with('5')
    target_strategy.should_rebalance.assert_called_once_with(event, '5')
    data.position_events.assert_called_once_with(('5',))


def test_mainflow_consumes_events_lazily_and_reports_each_skip_once(target_strategy):
    """One shared stream is advanced only after handling the previous event."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    target_strategy.event_accounts.return_value = ('00123', '5')
    events = [PositionEvent(False, '', (), (), 'ping'),
              PositionEvent(True, '5', (), (), 'ready')]
    consumed = []
    timeline = Mock()
    timeline.attach_mock(target_strategy, 'strategy')
    timeline.attach_mock(data, 'data')

    def stream():
        for event in events:
            consumed.append(event)
            yield event

    def decide(event, dst_account_id):
        assert consumed == events[:events.index(event) + 1]
        assert dst_account_id == '5'
        return event.has_position

    data.position_events.side_effect = [stream(), TestException()]
    target_strategy.should_rebalance.side_effect = decide
    engine = AutoRepeater(
        target_strategy, data, create_autospec(ExecutionData, instance=True), Mock())
    with patch.object(engine, 'sync_accounts', autospec=True) as sync, \
            patch.object(reporting, 'print_skipped_strategy_event', autospec=True) as skipped:
        timeline.attach_mock(sync, 'sync')
        with pytest.raises(TestException):
            engine.mainflow('5')
    assert timeline.mock_calls == [
        call.sync('5'), call.strategy.event_accounts('5'),
        call.data.position_events(('00123', '5')),
        call.strategy.should_rebalance(events[0], '5'),
        call.strategy.should_rebalance(events[1], '5'), call.sync('5'),
        call.strategy.event_accounts('5'), call.data.position_events(('00123', '5')),
    ]
    skipped.assert_called_once_with(events[0])


@pytest.mark.parametrize('failure', ['accounts', 'decision'])
def test_mainflow_event_method_value_error_is_not_retried(target_strategy, failure):
    """Configuration or predicate errors leave the loop immediately."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    event = PositionEvent(False, '', (), (), 'ping')
    data.position_events.side_effect = [iter((event,)), AssertionError('unexpected resubscription')]
    method = (target_strategy.event_accounts if failure == 'accounts'
              else target_strategy.should_rebalance)
    method.side_effect = [ValueError('invalid event method'),
                          AssertionError('unexpected event method retry')]
    engine = AutoRepeater(
        target_strategy, data, create_autospec(ExecutionData, instance=True), Mock())
    with patch.object(engine, 'sync_accounts', autospec=True) as sync, \
            pytest.raises(ValueError, match='invalid event method'):
        engine.mainflow('5')
    sync.assert_called_once_with('5')
    target_strategy.event_accounts.assert_called_once_with('5')
    assert data.position_events.call_args_list == ([call(('5',))] if failure == 'decision' else [])


@pytest.mark.parametrize('failure', ['open', 'read', 'initial_sync', 'event_sync', 'end'])
@pytest.mark.parametrize('error_type', [DataAccessError])
def test_common_event_stream_recovers_transport_failures(
        client, target_strategy, failure, error_type, caplog):
    """The consumer recovers both transport error types at each stream/sync boundary."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    event = PositionEvent(True, '5', (), (), 'ready')
    error = (RequestError(StatusCode.UNAVAILABLE, 'common stream failed', ())
             if error_type is RequestError else error_type('common stream failed'))

    def failing_stream():
        yield PositionEvent(False, '', (), (), 'ping')
        raise error

    first_stream = {'open': error, 'read': failing_stream(), 'initial_sync': iter(()),
                    'event_sync': iter((event, event)), 'end': iter(())}[failure]
    data.position_events.side_effect = [first_stream, iter((event,)), TestException()]
    target_strategy.should_rebalance.side_effect = lambda incoming, _dst: incoming.has_position
    results = ([error, None] if failure == 'initial_sync' else
               [None, error, None, None] if failure == 'event_sync' else [None, None])
    engine = AutoRepeater(
        target_strategy, data, create_autospec(ExecutionData, instance=True), Mock())
    with patch.object(engine, 'sync_accounts', autospec=True, side_effect=results) as sync, \
            caplog.at_level(logging.ERROR, logger=logging_config.LOGGER_NAME), \
            pytest.raises(TestException):
        engine.mainflow('5')
    assert sync.call_args_list == [call('5')] * len(results)
    assert data.position_events.call_args_list == [call(('5',))] * 3
    assert target_strategy.event_accounts.call_args_list == [call('5')] * 3
    expected_events = ([PositionEvent(False, '', (), (), 'ping'), event] if failure == 'read'
                       else [event, event, event] if failure == 'event_sync' else [event])
    assert target_strategy.should_rebalance.call_args_list == [
        call(incoming, '5') for incoming in expected_events]
    if failure == 'event_sync':
        assert not list(first_stream)
    if failure == 'end':
        assert not caplog.messages
    else:
        assert any(str(error) in message for message in caplog.messages)
    client.orders.post_order.assert_not_called()


def test_strategy_protocol_declares_complete_runtime_surface():
    """The shared protocol documents the complete surface consumed by the engine."""
    assert Strategy._is_protocol is True  # pylint: disable=protected-access
    assert 'default_reserve' not in Strategy.__dict__
    assert 'events' not in Strategy.__dict__
    assert {
        'load_snapshot', 'allocation_profile', 'build_plan', 'event_accounts', 'should_rebalance'
    } <= set(Strategy.__dict__)


@pytest.mark.parametrize('missing_member', [
    'load_snapshot', 'allocation_profile', 'build_plan', 'event_accounts', 'should_rebalance',
])
def test_direct_repeater_rejects_incomplete_strategy_before_use(client, missing_member):
    """Direct construction enforces the same strategy boundary as registered factories."""
    members = ['load_snapshot', 'allocation_profile', 'build_plan',
               'event_accounts', 'should_rebalance']
    members.remove(missing_member)
    strategy = Mock(spec_set=members)

    with pytest.raises(TypeError, match=missing_member):
        AutoRepeater(strategy, TInvestStrategyData(client), Mock(), Mock())

    assert client.mock_calls == []


@pytest.mark.parametrize('quantities, prices', [
    ({}, {}),
    ({'1': Decimal('2'), '2': Decimal('0')}, {'1': Decimal('1.25'), '2': Decimal('0')}),
    ({'negative': Decimal('-2.5'), 'fractional': Decimal('0.125')},
     {'negative': Decimal('-1'), 'fractional': Decimal('0')}),
    ({'1': Decimal('2')}, {'1': Decimal('-1.25'), 'unused': None}),
    ({}, {'unused': Decimal('NaN')}),
])
def test_validate_target_accepts_complete_prices(quantities, prices):
    """Zero and negative estimates are valid; unused prices impose no constraints."""
    target = TargetPortfolio(quantities, prices)

    assert validate_target(target) is None
    assert target.quantities == quantities
    assert target.prices == prices


@pytest.mark.parametrize('quantity', [Decimal('0'), Decimal('2')])
@pytest.mark.parametrize('prices', [
    {}, {'missing': None}, {'missing': 0}, {'missing': 1.25},
    {'missing': '1.25'}, {'missing': True}, {'missing': Decimal('NaN')},
    {'missing': Decimal('sNaN')}, {'missing': Decimal('Infinity')},
    {'missing': Decimal('-Infinity')},
])
def test_validate_target_rejects_invalid_price_for_every_uid(quantity, prices):
    """Every UID needs a finite Decimal, including targets with zero quantities."""
    target = TargetPortfolio(
        {'valid': Decimal('1'), 'missing': quantity}, {'valid': Decimal('2'), **prices})

    with pytest.raises(ValueError, match='UID: missing'):
        validate_target(target)


@pytest.mark.parametrize('quantities, prices, message', [
    ([], {}, 'quantities'),
    ({}, [], 'prices'),
    ({'': Decimal('1')}, {'': Decimal('1')}, 'quantity UID'),
    ({1: Decimal('1')}, {1: Decimal('1')}, 'quantity UID'),
    ({'bad': None}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': True}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': 1}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': 1.5}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': '1'}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': Decimal('NaN')}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': Decimal('sNaN')}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': Decimal('Infinity')}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
    ({'bad': Decimal('-Infinity')}, {'bad': Decimal('1')}, 'quantity for UID: bad'),
])
def test_validate_target_rejects_invalid_maps_uids_and_quantities(
        quantities, prices, message):
    """Target maps, UID keys, and quantities are validated without coercion."""
    with pytest.raises(ValueError, match=message):
        validate_target(TargetPortfolio(quantities, prices))


@pytest.mark.parametrize('reason', [None, '', 'INDEX/leaf: empty target'])
def test_validate_empty_reason_for_empty_target(reason):
    """Only strings or None may diagnose an empty target."""
    assert validate_target(TargetPortfolio({}, {}, reason)) is None


@pytest.mark.parametrize('reason', [False, 0, [], {}, Decimal('1')])
def test_validate_target_rejects_nonstring_reason(reason):
    """Diagnostics do not silently accept arbitrary objects."""
    with pytest.raises(ValueError, match='empty_reason'):
        validate_target(TargetPortfolio({}, {}, reason))


@pytest.mark.parametrize('src', ['4', '0004', '0', '123456789012345678901234567890'])
def test_strategy_account_selection_without_sdk(src, client, monkeypatch):
    """Account preparation retains leading zeros and never reads index paths."""
    definition = strategies.ALGORITHMS['ACCOUNT']
    factory = Mock(wraps=AccountStrategy)
    monkeypatch.setitem(strategies.ALGORITHMS, 'ACCOUNT',
                        AlgorithmDefinition(definition.prepare_source, factory))
    monkeypatch.setenv('INDEX_CONFIG_DIR', '/missing')
    monkeypatch.setenv('IMOEX_CONFIG_PATH', '/conflicting')
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        prepared = strategies.prepare_strategy('ACCOUNT', src)
        factory.assert_not_called()
        strategy = strategies.create_strategy(prepared)
    assert isinstance(strategy, AccountStrategy)
    assert strategy.src == src
    factory.assert_called_once_with(prepared.prepared_source)
    assert prepared.prepared_source == PreparedAccountSource(
        src, AccountConfig(src, src, Decimal('0.01'), Decimal('0.0092')))
    assert strategy.config == AccountConfig(src, src, Decimal('0.01'), Decimal('0.0092'))
    sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize('src, message', [
    (None, 'src is required'), ('', 'src is required'), (' \t\n', 'src is required'),
    ('imoex', 'unsupported src: imoex'), ('unknown', 'unsupported src: unknown'),
    (' IMOEX', 'unsupported src:  IMOEX'), ('IMOEX ', 'unsupported src: IMOEX '),
    (' 123 ', 'unsupported src:  123 '), ('123\n', 'unsupported src: 123\n'),
    ('\u0661\u0662\u0663', 'unsupported src: \u0661\u0662\u0663'),
    ('\uff11\uff12\uff13', 'unsupported src: \uff11\uff12\uff13'),
    ('\u00b2', 'unsupported src: \u00b2'), ('+123', 'unsupported src: +123'),
    ('-123', 'unsupported src: -123'), ('12.3', 'unsupported src: 12.3'),
    (123, 'unsupported src: 123'), ([], 'unsupported src: []'),
])
def test_strategy_rejects_unsupported_source_without_construction(src, message):
    """ACCOUNT rejects invalid IDs without normalizing or selecting another algorithm."""
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        with pytest.raises(strategies.UnsupportedSourceError) as exc_info:
            strategies.prepare_strategy('ACCOUNT', src)
        assert str(exc_info.value) == message
    sdk_client.assert_not_called()


def test_strategy_named_registration_is_exact_and_validation_is_pure(monkeypatch):
    """Only the selected registration interprets src and constructs a strategy."""
    strategy = Mock(spec_set=[
        'load_snapshot', 'allocation_profile', 'build_plan', 'event_accounts', 'should_rebalance'])
    factory = Mock(return_value=strategy)
    monkeypatch.setitem(strategies.ALGORITHMS, 'TEST',
                        AlgorithmDefinition(lambda src, context: src, factory))
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        prepared = strategies.prepare_strategy('TEST', 'TEST')
        factory.assert_not_called()
        assert strategies.create_strategy(prepared) is strategy
        for name in ['test', ' TEST', 'TEST ']:
            with pytest.raises(ValueError, match='algoritm'):
                strategies.prepare_strategy(name, 'TEST')
    factory.assert_called_once_with('TEST')
    sdk_client.assert_not_called()


@pytest.mark.parametrize('invalid_strategy, member', [
    (object(), 'load_snapshot'),
    (Mock(spec_set=['allocation_profile', 'build_plan', 'event_accounts', 'should_rebalance']),
     'load_snapshot'),
    (Mock(spec_set=['load_snapshot', 'allocation_profile', 'event_accounts', 'should_rebalance']),
     'build_plan'),
    (Mock(spec_set=['load_snapshot', 'allocation_profile', 'build_plan', 'should_rebalance']),
     'event_accounts'),
    (Mock(spec_set=['load_snapshot', 'allocation_profile', 'build_plan', 'event_accounts']),
     'should_rebalance'),
])
def test_registered_factory_result_is_validated_before_client(
        monkeypatch, invalid_strategy, member):
    """A registered factory cannot defer an invalid strategy failure until SDK startup."""
    factory = Mock(return_value=invalid_strategy)
    monkeypatch.setitem(strategies.ALGORITHMS, 'BROKEN',
                        AlgorithmDefinition(lambda src, context: src, factory))

    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'configure_local_logging', autospec=True), \
            pytest.raises(TypeError, match=member):
        runner_module.Runner('test-token', strategies.prepare_strategy('BROKEN', 'BROKEN'), '5')

    factory.assert_called_once_with('BROKEN')
    sdk_client.assert_not_called()


def test_strategy_numeric_source_uses_selected_algorithm(monkeypatch):
    """A numeric source has no routing priority over the selected algorithm."""
    factory = Mock(return_value=account_strategy('different'))
    monkeypatch.setitem(strategies.ALGORITHMS, '0004',
                        AlgorithmDefinition(lambda src, context: src, factory))
    prepared = strategies.prepare_strategy('0004', '0004')
    assert strategies.create_strategy(prepared).src == 'different'
    factory.assert_called_once_with('0004')


@pytest.mark.usefixtures('fractional_portfolios')
def test_account_strategy_fractional_target(client, caplog):
    """Source cash is reported but excluded; quantities and prices share one snapshot."""
    strategy = account_strategy('4', reserve='0.1')
    with caplog.at_level(logging.INFO, logger=logging_config.LOGGER_NAME):
        snapshot = strategy.load_snapshot(TInvestStrategyData(client))

    positions, total = snapshot
    assert total == Decimal('204.000000000')
    assert {
        uid: (position.instrument_type, position.current_price, position.quantity)
        for uid, position in positions.items()
    } == {
        '1': (InstrumentType.SHARE, Decimal('4'), Decimal('10.5')),
        '2': (InstrumentType.ETF, Decimal('8'), Decimal('20.25')),
    }
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='4'),
    ]
    assert caplog.messages == [
        'src account', '1 - 10.5 - RUB - 42.0', '2 - 20.25 - RUB - 162.0',
        'RUB - 999.0', 'total: 204.000000000']

    client.reset_mock()
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        target = strategy.build_target(snapshot, Decimal('170'))
    assert target == TargetPortfolio(
        {'1': Decimal('7.875'), '2': Decimal('15.1875')},
        {'1': Decimal('4'), '2': Decimal('8')})
    validate_target(target)
    assert client.mock_calls == []
    sdk_client.assert_not_called()


def test_account_strategy_target_preserves_division_before_multiplication(client):
    """Decimal precision makes computing ratio first observably different from weights."""
    snapshot = ({
        '1': PortfolioEntry('1', InstrumentType.SHARE, 'RUB', Decimal('1'),
                            Decimal('3'), 'one'),
        '2': PortfolioEntry('2', InstrumentType.SHARE, 'RUB', Decimal('0.000000001'),
                            Decimal('0'), 'two'),
        '3': PortfolioEntry('3', InstrumentType.SHARE, 'RUB', Decimal('0'),
                            Decimal('0.000000001'), 'three'),
    }, Decimal('3'))

    target = account_strategy('4', reserve='0').build_target(snapshot, Decimal('1'))

    assert target == TargetPortfolio({
        '1': Decimal('0.9999999999999999999999999999'), '2': Decimal('0'),
        '3': Decimal('3.333333333333333333333333333E-10'),
    }, {'1': Decimal('1'), '2': Decimal('0.000000001'), '3': Decimal('0')})
    assert client.mock_calls == []
    validate_target(target)


def test_account_strategy_loads_fresh_snapshot(client):
    """A new synchronization sees new quantities and prices without mutating the old snapshot."""
    client.operations.get_portfolio.side_effect = [
        PortfolioResponse(positions=[PortfolioPosition(
            instrument_uid='1', instrument_type='share',
            quantity=Quotation(2, 0), current_price=MoneyValue('RUB', 4, 0))]),
        PortfolioResponse(positions=[PortfolioPosition(
            instrument_uid='2', instrument_type='etf',
            quantity=Quotation(3, 0), current_price=MoneyValue('RUB', 5, 0))]),
    ]
    strategy = account_strategy('0004', reserve='0')
    first = strategy.load_snapshot(TInvestStrategyData(client))
    second = strategy.load_snapshot(TInvestStrategyData(client))

    assert strategy.build_target(first, Decimal('8')) == TargetPortfolio(
        {'1': Decimal('2')}, {'1': Decimal('4')})
    assert strategy.build_target(second, Decimal('30')) == TargetPortfolio(
        {'2': Decimal('6')}, {'2': Decimal('5')})
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='0004'),
        call.operations.get_portfolio(account_id='0004')]


def test_account_strategy_load_error(client, caplog):
    """A failed source read propagates its error instead of becoming a liquidation target."""
    error = RequestError(code=StatusCode.UNAVAILABLE, details='source unavailable', metadata=())
    client.operations.get_portfolio.side_effect = error

    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME), \
            pytest.raises(DataAccessError) as exc_info:
        account_strategy('4').load_snapshot(TInvestStrategyData(client))

    assert exc_info.value.__cause__ is error
    assert client.mock_calls == [call.operations.get_portfolio(account_id='4')]
    assert len(caplog.records) == 2
    assert caplog.records[0].getMessage() == 'src account'
    failure = caplog.records[1]
    assert failure.levelno == logging.ERROR
    assert failure.msg == 'API request failed: method=%s status=%s elapsed_seconds=%.3f'
    assert failure.args[:2] == ('MagicMock', 'UNAVAILABLE')
    assert failure.args[2] >= 0
    assert 'source unavailable' not in caplog.text


@pytest.mark.parametrize('positions', [
    [],
    [PortfolioPosition(instrument_type='currency', instrument_uid='cash',
                       current_price=MoneyValue('RUB', 1, 0), quantity=Quotation(100, 0))],
    [PortfolioPosition(instrument_type='share', instrument_uid='1',
                       current_price=MoneyValue('RUB', 0, 0), quantity=Quotation(100, 0))],
])
def test_account_strategy_zero_source_value(client, positions):
    """Zero source value retains its division error and never yields an empty target."""
    client.operations.get_portfolio.side_effect = [PortfolioResponse(positions=positions)]
    strategy = account_strategy('4')
    snapshot = strategy.load_snapshot(TInvestStrategyData(client))
    client.reset_mock()

    with pytest.raises(DivisionByZero):
        strategy.build_target(snapshot, Decimal('100'))

    assert client.mock_calls == []


@pytest.mark.parametrize('position, expected', [
    (None, False),
    (PositionData(account_id='4', money=[], securities=[
        PositionsSecurities(instrument_uid='1', blocked=0)]), True),
    (PositionData(account_id='4', money=[], securities=[
        PositionsSecurities(instrument_uid='1', blocked=1)]), False),
    (PositionData(account_id='5', securities=[], money=[PositionsMoney(
        available_value=MoneyValue('RUB', 100, 0), blocked_value=MoneyValue('RUB', 0, 0))]), True),
    (PositionData(account_id='5', securities=[], money=[PositionsMoney(
        available_value=MoneyValue('RUB', 100, 0), blocked_value=MoneyValue('RUB', 0, 1))]), False),
    (PositionData(account_id='other', money=[], securities=[
        PositionsSecurities(instrument_uid='1', blocked=0)]), False),
])
def test_account_strategy_event_predicate(client, position, expected, caplog):
    """ACCOUNT makes a pure decision from the adapter's own event."""
    client.operations_stream.positions_stream.side_effect = [iter([
        PositionsStreamResponse(position=position)])]
    strategy = account_strategy('4')
    accounts = strategy.event_accounts('5')
    assert accounts == ('4', '5')
    event = next(TInvestStrategyData(client).position_events(accounts))
    client.reset_mock()
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        assert strategy.should_rebalance(event, '5') is expected
    assert client.mock_calls == []
    assert caplog.records == []


@pytest.fixture(name='rotation_portfolios')
def rotation_portfolios_fixture(client):
    """Equal-value portfolios require selling 100 shares before buying 50 ETFs."""
    src_positions = {'2': PortfolioPosition(
        instrument_type='etf', instrument_uid='2',
        current_price=MoneyValue(currency='RUB', units=2, nano=0),
        quantity=Quotation(units=50, nano=0))}
    dst_positions = {'1': PortfolioPosition(
        instrument_type='share', instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=100, nano=0))}
    client.operations.get_portfolio.side_effect = [
        PortfolioResponse(positions=list(src_positions.values())),
        PortfolioResponse(positions=list(dst_positions.values())),
    ]
    return src_positions, dst_positions


def test_print_all_portfolios(client, caplog):
    """Account output includes empty portfolios and calculated currency totals."""
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        reporting.print_all_portfolio(client)

    client.users.get_accounts.assert_called_once_with()
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id='1'), call(account_id='2')]
    assert client.mock_calls == [call.users.get_accounts(),
                                 call.operations.get_portfolio(account_id='1'),
                                 call.operations.get_portfolio(account_id='2')]
    assert caplog.messages == [
        'account name (1)', '------------', 'RUB - 2.4', 'total: 2.400000000', '============',
        'account name (2)', '------------', 'total: 0', '============']
    assert all(record.levelno == logging_config.IMPORTANT for record in caplog.records)
    client.orders.post_order.assert_not_called()


def test_position_unknown_type(client):
    """Unknown instrument types retain the SDK's string representation."""
    position = PortfolioPosition(instrument_type='unknown')
    assert reporting.postiton_to_string(client, position) == str(position)
    assert client.mock_calls == []


@pytest.mark.parametrize('positions', [[], [PortfolioPosition(instrument_type='currency')]])
def test_get_portfolio(client, caplog, positions):
    """Shared reading preserves the SDK snapshot without logging or additional queries."""
    response = PortfolioResponse(positions=positions)
    client.operations.get_portfolio.side_effect = None
    client.operations.get_portfolio.return_value = response
    with caplog.at_level(logging.DEBUG, logger=logging_config.LOGGER_NAME):
        assert get_portfolio(client, '4') is response
    assert client.mock_calls == [call.operations.get_portfolio(account_id='4')]
    assert caplog.records == []


def test_get_portfolio_error(client, caplog):
    """Portfolio loading passes the original SDK error to its caller."""
    error = RequestError(code=StatusCode.UNAVAILABLE, details='portfolio unavailable', metadata=())
    client.operations.get_portfolio.side_effect = error
    with caplog.at_level(logging.DEBUG, logger=logging_config.LOGGER_NAME), \
            pytest.raises(RequestError) as raised:
        get_portfolio(client, '4')
    assert raised.value is error
    assert client.mock_calls == [call.operations.get_portfolio(account_id='4')]
    assert caplog.records == []


def test_skipped_event_reporting_uses_only_strategy_diagnostics(caplog):
    """Skipped events keep DTO diagnostics without the obsolete raw-event API."""
    event = PositionEvent(False, '', (), (), 'skipped event diagnostic')
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        reporting.print_skipped_strategy_event(event)

    assert caplog.messages == ['skipped event diagnostic']
    assert [record.levelno for record in caplog.records] == [logging_config.IMPORTANT]
    assert not hasattr(reporting, 'print_skipped_event')


@pytest.mark.parametrize('method', ['run', 'run_sync'])
def test_runner_reporting_integration(method, client, caplog):
    """Only the explicitly named local browse reads every user's portfolio."""
    engine = Mock(spec_set=['set_debug', 'sync_accounts', 'mainflow'])
    engine.mainflow.side_effect = TestException()
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'configure_local_logging', autospec=True), \
            patch.object(runner_module.Runner, '_create_repeater', return_value=engine), \
            caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        sdk_client.return_value.__enter__.return_value = client
        runner = runner_module.Runner(
            'test-token', strategies.prepare_strategy('ACCOUNT', '4'), '5',
            RunnerParams(True))
        if method == 'run':
            with pytest.raises(TestException):
                runner.run()
        else:
            runner.run_sync()
        sdk_client.return_value.__exit__.assert_called_once()

    expected_accounts = ['1', '2'] if method == 'run' else []
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id=account) for account in expected_accounts]
    assert client.users.get_accounts.call_args_list == ([call()] if method == 'run' else [])
    getattr(engine, 'mainflow' if method == 'run' else 'sync_accounts').assert_called_once_with('5')
    assert any('account name' in message for message in caplog.messages) == (method == 'run')
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('method', ['run', 'run_sync'])
@pytest.mark.parametrize('src, message', [
    (None, 'src is required'), ('', 'src is required'), (' \t', 'src is required'),
    ('imoex', 'unsupported src: imoex'), ('unknown', 'unsupported src: unknown'),
])
def test_runner_invalid_source_before_client(method, src, message, client):
    """Both launch modes reject invalid sources in the constructor before opening SDK."""
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'configure_local_logging', autospec=True):
        sdk_client.return_value.__enter__.return_value = client
        with pytest.raises(strategies.UnsupportedSourceError, match=message):
            runner = runner_module.Runner(
                'test-token', strategies.prepare_strategy('ACCOUNT', src), '5')
            getattr(runner, method)()
        sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.fixture(name='invest_environment', autouse=True)
def invest_environment_fixture(monkeypatch):
    """All entrypoint tests use controlled credentials and account parameters."""
    for name in ('INVEST_TOKEN', 't_token', 'SRC_ACCOUNT', 'DST_ACCOUNT',
                 'IMOEX_CONFIG_PATH', 'INDEX_CONFIG_DIR', 'ACCOUNT_CONFIG_PATH',
                 'COMPOSITE_CONFIG_DIR', 'ALGORITM'):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture(name='named_strategy_factory')
def named_strategy_factory_fixture(client, target_strategy, monkeypatch):
    """Register a source with no account ID or source positions; SDK access is destination-only."""
    target_strategy.build_target.return_value = TargetPortfolio(
        {'1': Decimal('2'), '2': Decimal('3')},
        {'1': Decimal('5'), '2': Decimal('10')})
    target_strategy.should_rebalance.side_effect = [False, True, False]
    factory = Mock(return_value=target_strategy)
    monkeypatch.setitem(strategies.ALGORITHMS, 'TEST',
                        AlgorithmDefinition(lambda src, context: src, factory))
    portfolios = {'5': PortfolioResponse(positions=[
        PortfolioPosition(
            instrument_type='share', instrument_uid='1',
            current_price=MoneyValue(currency='RUB', units=4, nano=0),
            quantity=Quotation(units=5, nano=0)),
        PortfolioPosition(
            instrument_type='currency', instrument_uid='cash',
            current_price=MoneyValue(currency='RUB', units=1, nano=0),
            quantity=Quotation(units=80, nano=0)),
    ])}
    client.operations.get_portfolio.side_effect = lambda **kwargs: portfolios[kwargs['account_id']]
    client.users.get_accounts.return_value = GetAccountsResponse(accounts=[])
    client.operations_stream.positions_stream.side_effect = [
        iter([PositionsStreamResponse(position=None), PositionsStreamResponse(position=PositionData(
            account_id='5', securities=[], money=[])), PositionsStreamResponse(position=None)]),
        TestException(),
    ]
    return factory


@pytest.mark.parametrize('has_token', [False, True])
@pytest.mark.parametrize('arguments, message', [
    (['-s', ''], 'src is required'),
    (['-s', ' \t '], 'src is required'),
    (['-s', 'imoex'], 'unsupported src: imoex'),
    (['-s', 'unknown'], 'unsupported src: unknown'),
])
def test_cli_rejects_source_before_credentials(
        arguments, message, has_token, client, invest_environment):
    """Source errors take precedence over credentials and cannot construct a client."""
    if has_token:
        invest_environment.setenv('INVEST_TOKEN', 'test-token')
    invest_environment.setattr(sys, 'argv', ['main.py', '--algoritm', 'ACCOUNT', *arguments])
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(cli, 'os', wraps=cli.os) as cli_os:
        sdk_client.return_value.__enter__.return_value = client
        with pytest.raises(strategies.UnsupportedSourceError, match=f'^{message}$'):
            cli.main()
        assert cli_os.mock_calls == []
        sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize('has_token', [False, True])
@pytest.mark.parametrize('source_location, src, message', [
    ('query', None, 'src is required'),
    ('query', '', 'src is required'),
    ('query', ' \t ', 'src is required'),
    ('query', 'imoex', 'unsupported src: imoex'),
    ('query', 'unknown', 'unsupported src: unknown'),
    ('environment', '', 'src is required'),
    ('environment', ' \t ', 'src is required'),
    ('environment', 'imoex', 'unsupported src: imoex'),
    ('environment', 'unknown', 'unsupported src: unknown'),
])
def test_cloud_rejects_source_before_credentials(
        src, message, source_location, has_token, client, invest_environment):
    """An explicit invalid query source never falls back to a valid environment source."""
    invest_environment.setenv('ALGORITM', 'ACCOUNT')
    if has_token:
        invest_environment.setenv('INVEST_TOKEN', 'test-token')
        invest_environment.setenv('t_token', 'legacy-token')
    query = {}
    if source_location == 'query':
        query['src'] = src
        invest_environment.setenv('SRC_ACCOUNT', '4')
    elif src is not None:
        invest_environment.setenv('SRC_ACCOUNT', src)
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(serverless, 'configure_yc_logging', autospec=True), \
            patch.object(serverless, 'os', wraps=serverless.os) as cloud_os:
        sdk_client.return_value.__enter__.return_value = client
        with pytest.raises(strategies.UnsupportedSourceError, match=f'^{message}$'):
            cloud_entrypoint.handler({'queryStringParameters': query}, None)
        assert cloud_os.mock_calls == (
            [call.environ.get('ALGORITM', serverless.DEFAULT_ALGORITM),
             call.environ.get('SRC_ACCOUNT', None)]
            if source_location == 'environment'
            else [call.environ.get('ALGORITM', serverless.DEFAULT_ALGORITM)])
        sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize(
    'event, environment, expected',
    [
        ({'queryStringParameters': {
            'src': '4', 'dst': 'query-dst', 'token': 'query-token'}},
         {'SRC_ACCOUNT': '6', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token',
          't_token': 'legacy-token'}, ('4', 'query-dst', 'query-token')),
        ({'queryStringParameters': None},
         {'SRC_ACCOUNT': '4', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token',
          't_token': 'legacy-token'}, ('4', 'env-dst', 'env-token')),
        ({'queryStringParameters': {'dst': '', 'token': ''}},
         {'SRC_ACCOUNT': '4', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token'},
         ('4', 'env-dst', 'env-token')),
        ({}, {'SRC_ACCOUNT': '4', 't_token': 'legacy-token'},
         ('4', serverless.DEFAULT_DST_ACCOUNT, 'legacy-token')),
        (None, {'SRC_ACCOUNT': '4', 'INVEST_TOKEN': 'env-token'},
         ('4', serverless.DEFAULT_DST_ACCOUNT, 'env-token')),
    ],
    ids=['query_priority', 'null_query', 'empty_query_values', 'legacy_token', 'no_event',
         ],
)
def test_cloud_entrypoint(event, environment, expected, invest_environment):
    """The deployed entrypoint resolves parameters and performs exactly one sync."""
    for name, value in environment.items():
        invest_environment.setenv(name, value)
    invest_environment.setenv('ALGORITM', 'ACCOUNT')
    src, dst, token = expected
    with patch.object(serverless, 'Runner', autospec=True) as runner_class, \
            patch.object(serverless, 'configure_yc_logging', autospec=True) as configure:
        result = cloud_entrypoint.handler(event, None)

        configure.assert_called_once_with()
        runner_class.assert_called_once_with(
            token=token, prepared_strategy=strategies.prepare_strategy('ACCOUNT', src), dst=dst)
        assert runner_class.return_value.method_calls == [call.run_sync()]
        assert result == {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'isBase64Encoded': False,
            'body': f'Success sync, {src} {dst}!',
        }


def test_cloud_archive(tmp_path):
    """All packaged configs work by their JSON names from an isolated archive."""
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / 'project'
    config_dir = project / 'autorepeater/configs'
    config_dir.mkdir(parents=True)
    for name in ['Makefile', 'main.py', 'handler.py', 'requirements.txt']:
        shutil.copyfile(repository / name, project / name)
    (project / 'scripts').mkdir()
    shutil.copyfile(repository / 'scripts/build_yandex_archive.py',
                    project / 'scripts/build_yandex_archive.py')
    for source in (repository / 'autorepeater').glob('*.py'):
        shutil.copyfile(source, project / 'autorepeater' / source.name)
    for source in (repository / 'autorepeater/configs').rglob('*.json'):
        relative = source.relative_to(repository / 'autorepeater/configs')
        (config_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, config_dir / relative)
    extra = json.loads((config_dir / 'imoex.json').read_text(encoding='utf-8'))
    extra.update(name='SECOND_INDEX', min_position_value='2000', reserve='0.02')
    (config_dir / 'different-filename.json').write_text(json.dumps(extra), encoding='utf-8')
    (config_dir / 'unrelated-broken.json').write_text('{', encoding='utf-8')
    build = subprocess.run(
        ['make', 'claude-yandex-archive'], cwd=project, check=False,
        capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stdout + build.stderr
    extracted = tmp_path / 'extracted'
    expected = {'main.py', 'handler.py', 'requirements.txt'}
    expected.update(path.relative_to(project).as_posix() for path in config_dir.rglob('*.json'))
    expected.update(f'autorepeater/{path.name}'
                    for path in (repository / 'autorepeater').glob('*.py'))
    with ZipFile(project / 'build/yandex-function.zip') as archive:
        assert {item.filename for item in archive.infolist() if not item.is_dir()} == expected
        for path in config_dir.rglob('*.json'):
            assert archive.read(path.relative_to(project).as_posix()) == path.read_bytes()
        assert 'scripts/build_yandex_archive.py' not in archive.namelist()
        archive.extractall(extracted)

    working_directory = tmp_path / 'working'
    working_directory.mkdir()
    # -I ignores inherited import paths; only the extracted application is added.
    probe = subprocess.run(
        [sys.executable, '-I', '-c', '''
import os
import sys
import sysconfig
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, create_autospec, patch

extracted, repository = map(Path, sys.argv[1:])
assert not Path.cwd().is_relative_to(repository)
assert not Path.cwd().is_relative_to(extracted)
dependency_paths = {Path(sysconfig.get_path(key)).resolve() for key in ('purelib', 'platlib')}
source_paths = [path for path in sys.path
                if Path(path).resolve().is_relative_to(repository)
                and Path(path).resolve() not in dependency_paths]
assert not source_paths, ('application source leaked onto sys.path', source_paths)
sys.path.insert(0, str(extracted))
with patch('t_tech.invest.Client', side_effect=AssertionError('SDK client forbidden')) as client, \\
        patch('grpc.secure_channel', side_effect=AssertionError('gRPC forbidden')) as channel, \\
        patch('grpc.insecure_channel', side_effect=AssertionError('gRPC forbidden')) as insecure, \\
        patch('socket.socket.connect', side_effect=AssertionError('network forbidden')) as connect:
    import handler
    import main
    from autorepeater import index_config, reporting, runner
    from autorepeater.index_config import load_index_config
    from autorepeater.index_strategy import IndexQuote, IndexStrategy, build_index_target
    from autorepeater.composite_strategy import CompositeSnapshot, CompositeStrategy
    from autorepeater.strategy_data import InstrumentType, PortfolioEntry
    from autorepeater.repeater import AutoRepeater
    from autorepeater.strategy_data import StrategyData
    from autorepeater.strategy_plan import StrategyContext
    from autorepeater.strategies import create_strategy, prepare_strategy

    assert Path(handler.__file__).resolve() == extracted / 'handler.py'
    assert Path(main.__file__).resolve() == extracted / 'main.py'
    assert callable(handler.handler)
    account = create_strategy(prepare_strategy('ACCOUNT', '2193248994'))
    assert account.src == '2193248994'
    assert account.config.reserve == Decimal('0.01')
    entry = PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('10'), Decimal('10'), 'one')
    assert account.build_target(({'uid': entry}, Decimal('100')), Decimal('100')).quantities == {
        'uid': Decimal('9.90')}
    strategy = create_strategy(prepare_strategy('INDEX', 'IMOEX'))
    assert isinstance(strategy, IndexStrategy)
    assert strategy.config == load_index_config(extracted / 'autorepeater/configs/imoex.json')
    assert strategy.config.name == 'IMOEX'
    assert len(strategy.config.instruments) == 44
    assert strategy.config.max_lot_weight_error == Decimal('0.05')
    assert strategy.config.min_position_value == Decimal('3000')
    second = create_strategy(prepare_strategy('INDEX', 'SECOND_INDEX'))
    assert isinstance(second, IndexStrategy)
    assert second.config.name == 'SECOND_INDEX'
    assert second.config.min_position_value == Decimal('2000')
    assert second.config.reserve == Decimal('0.02')
    assert second.config is not strategy.config
    for name, reserve in [('GOLD', '0.006'), ('OBLG', '0.006'), ('TMON', '0.0005')]:
        single = create_strategy(prepare_strategy('INDEX', name))
        assert [item.ticker for item in single.config.instruments] == [name]
        assert single.config.reserve == Decimal(reserve)

    launches = []
    def capture_launch(application):
        data = create_autospec(StrategyData, instance=True, spec_set=True)
        execution = Mock(spec_set=[])
        with patch.object(runner, 'TInvestStrategyData', return_value=data):
            engine = application._create_repeater(execution)
        if isinstance(application.strategy, CompositeStrategy):
            strategy = application.strategy
            assert strategy.source.name == 'BALANCED'
            assert [(item.algoritm, item.src, item.weight) for item in strategy.source.components] == [
                ('INDEX', 'IMOEX', Decimal('0.684210526')),
                ('INDEX', 'OBLG', Decimal('0.210526316')),
                ('INDEX', 'GOLD', Decimal('0.105263158'))]
            assert sum(item.weight for item in strategy.source.components) == Decimal('1')
            snapshot = CompositeSnapshot(tuple(
                {item.ticker: IndexQuote('uid-' + item.ticker, Decimal('10'), 1)
                 for item in child.config.instruments} for child in strategy.children))
            target = strategy.build_plan(snapshot, StrategyContext(
                (), Decimal('100000'), Decimal(0), False, (),
                strategy.allocation_profile(snapshot).prices)).target
            assert target.quantities
            assert 'uid-OBLG' in target.quantities and 'uid-GOLD' in target.quantities
            launches.append(('BALANCED', None, engine.debug))
            assert engine.strategy is strategy and engine.data is data
            assert application.dst == 'destination'
            assert data.mock_calls == execution.mock_calls == []
            return
        config = application.strategy.config
        snapshot = {item.ticker: IndexQuote('uid-' + item.ticker, Decimal('10'), 1)
                    for item in config.instruments}
        target = application.strategy.build_target(snapshot, Decimal('100000'))
        assert target == build_index_target(config, snapshot, Decimal('100000') * (1 - config.reserve))
        assert target.quantities
        launches.append((config.name, config.reserve, engine.debug))
        assert application.dst == 'destination'
        assert engine.strategy is application.strategy
        assert engine.data is data
        assert data.mock_calls == []
        assert execution.mock_calls == []

    configs = extracted / 'autorepeater/configs'
    def assert_one_preparation(reads, warnings):
        assert Counter(Path(item.args[0]) for item in reads.call_args_list) == Counter(
            {path: 1 for path in configs.glob('*.json')})
        warnings.assert_called_once()
        assert 'unrelated-broken.json' in warnings.call_args.args[0]
        assert 'Expecting property name' in warnings.call_args.args[0]

    with patch.object(runner.Runner, 'run', autospec=True, side_effect=capture_launch) as local, \\
            patch.object(runner.Runner, 'run_sync', autospec=True,
                         side_effect=capture_launch) as cloud, \\
            patch.object(runner.TInvestOrderExecutor, 'submit_order', autospec=True,
                         side_effect=AssertionError('trading forbidden')) as trade:
        os.environ.update(INVEST_TOKEN='test-token', ALGORITM='INDEX', SRC_ACCOUNT='IMOEX')
        for name, reserve in [('IMOEX', Decimal('0.01')), ('SECOND_INDEX', Decimal('0.02'))]:
            sys.argv = ['main.py', '--algoritm', 'INDEX', '-s', name,
                        '-d', 'destination', '--debug']
            with patch.object(index_config, 'read_index_document',
                              wraps=index_config.read_index_document) as reads, \\
                    patch.object(reporting, 'print_index_config_warning') as warnings:
                main.main()
                assert_one_preparation(reads, warnings)
            assert launches[-1] == (name, reserve, True)
            for query in [True, False]:
                os.environ['SRC_ACCOUNT'] = name
                os.environ['DST_ACCOUNT'] = 'destination'
                # Query selection must override conflicting environment values.
                os.environ['ALGORITM'] = 'ACCOUNT' if query else 'INDEX'
                params = {'algoritm': 'INDEX', 'src': name, 'dst': 'destination'} if query else {}
                with patch.object(index_config, 'read_index_document',
                                  wraps=index_config.read_index_document) as reads, \\
                        patch.object(reporting, 'print_index_config_warning') as warnings:
                    result = handler.handler({'queryStringParameters': params}, None)
                    assert_one_preparation(reads, warnings)
                assert result['statusCode'] == 200
                assert result['body'] == f'Success sync, {name} destination!'
                assert launches[-1] == (name, reserve, False)
        assert len(launches) == 6
        assert local.call_count == 2
        assert cloud.call_count == 4

        os.environ.clear()
        for arguments in [[], ['-s', 'IMOEX'], ['--algoritm', 'INDEX']]:
            sys.argv = ['main.py', *arguments]
            try:
                main.main()
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError('missing CLI parameter accepted')
        for algorithm, source, message in [('UNKNOWN', 'IMOEX', 'unsupported algoritm'),
                                            ('INDEX', '', 'src is required'),
                                            ('ACCOUNT', 'IMOEX', 'unsupported src')]:
            sys.argv = ['main.py', '--algoritm', algorithm, '-s', source]
            try:
                main.main()
            except ValueError as error:
                assert message in str(error), str(error)
            else:
                raise AssertionError('invalid CLI selection accepted')
        os.environ.update(ALGORITM='INDEX', SRC_ACCOUNT='IMOEX')
        for params, missing in [({'algoritm': ''}, 'algoritm'),
                                ({'src': ''}, 'src'),
                                ({'algoritm': 'UNKNOWN'}, 'algoritm'),
                                ({'algoritm': 'ACCOUNT', 'src': 'IMOEX'}, 'unsupported src: IMOEX'),
                                ({'src': 'MISSING'}, 'unsupported src')]:
            try:
                handler.handler({'queryStringParameters': params}, None)
            except ValueError as error:
                assert missing in str(error), str(error)
            else:
                raise AssertionError('invalid cloud selection accepted')
        os.environ.clear()
        for environment in [{}, {'ALGORITM': 'COMPOSITE'}, {'SRC_ACCOUNT': 'BALANCED'}]:
            os.environ.clear()
            os.environ.update(INVEST_TOKEN='test-token', DST_ACCOUNT='destination', **environment)
            with patch.object(index_config, 'read_index_document',
                              wraps=index_config.read_index_document) as reads, \\
                    patch.object(reporting, 'print_index_config_warning') as warnings:
                result = handler.handler({}, None)
                assert Counter(Path(item.args[0]) for item in reads.call_args_list) == Counter(
                    {path: 3 for path in configs.glob('*.json')})
                assert warnings.call_count == 3
                assert all('unrelated-broken.json' in item.args[0]
                           for item in warnings.call_args_list)
            assert result['body'] == 'Success sync, BALANCED destination!'
            assert launches[-1] == ('BALANCED', None, False)
        for environment in [{'ALGORITM': 'INDEX'}, {'SRC_ACCOUNT': 'TMON'}]:
            os.environ.clear()
            os.environ.update(environment)
            try:
                handler.handler({}, None)
            except ValueError as error:
                assert ('src is required' if 'ALGORITM' in environment
                        else 'unsupported src: TMON') in str(error)
            else:
                raise AssertionError('implicit INDEX selection accepted')
        assert local.call_count == 2
        assert cloud.call_count == 7
        assert len(launches) == 9
        trade.assert_not_called()
    for name, module in list(sys.modules.items()):
        if name == 'autorepeater' or name.startswith('autorepeater.'):
            location = Path(module.__file__).resolve()
            assert location.is_relative_to(extracted), (name, location)
            assert not location.is_relative_to(repository), (name, location)
    client.assert_not_called()
    channel.assert_not_called()
    insecure.assert_not_called()
    connect.assert_not_called()
print('archive validation completed: 9 launches')
''', str(extracted), str(repository)],
        cwd=working_directory, env={}, check=False, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.rstrip().endswith('archive validation completed: 9 launches')


def test_cloud_missing_token(invest_environment):
    """Missing credentials fail before creating the runner."""
    invest_environment.delenv('INVEST_TOKEN', raising=False)
    invest_environment.setenv('ALGORITM', 'ACCOUNT')
    invest_environment.setenv('SRC_ACCOUNT', '4')
    with patch.object(serverless, 'Runner', autospec=True) as runner_class, \
            patch.object(serverless, 'configure_yc_logging', autospec=True):
        with pytest.raises(KeyError, match='t_token'):
            cloud_entrypoint.handler({}, None)
        runner_class.assert_not_called()


def test_cloud_sync_error(invest_environment):
    """A failed sync must not produce a successful HTTP response."""
    invest_environment.setenv('INVEST_TOKEN', 'test-token')
    invest_environment.setenv('SRC_ACCOUNT', '4')
    invest_environment.setenv('ALGORITM', 'ACCOUNT')
    error = RequestError(code=StatusCode.UNAVAILABLE, details='sync unavailable', metadata=())
    with patch.object(serverless, 'Runner', autospec=True) as runner_class, \
            patch.object(serverless, 'configure_yc_logging', autospec=True):
        runner_class.return_value.run_sync.side_effect = error
        with pytest.raises(RequestError) as raised:
            cloud_entrypoint.handler({}, None)
        assert raised.value is error
        runner_class.return_value.run_sync.assert_called_once_with()


@pytest.mark.parametrize(
    'arguments, src, dst, params',
    [
        (['-s', '4'], '4', None, RunnerParams(debug=False)),
        (['-s', '4', '-d', '5', '--debug'],
         '4', '5', RunnerParams(debug=True)),
    ],
    ids=['defaults', 'all_options'],
)
def test_cli(arguments, src, dst, params, invest_environment):
    """Command-line options and environment credentials reach the local runner."""
    invest_environment.setenv('INVEST_TOKEN', 'cli-token')
    invest_environment.setattr(sys, 'argv', ['main.py', '--algoritm', 'ACCOUNT', *arguments])
    with patch.object(cli, 'Runner', autospec=True) as runner_class:
        cli.main()
        runner_class.assert_called_once_with(
            token='cli-token', prepared_strategy=strategies.prepare_strategy('ACCOUNT', src),
            dst=dst, params=params)
        assert runner_class.return_value.method_calls == [call.run()]


def test_cli_script(invest_environment):
    """Executing main.py invokes the local runner without connecting to the API."""
    invest_environment.setenv('INVEST_TOKEN', 'cli-token')
    invest_environment.setattr(sys, 'argv', ['main.py', '--algoritm', 'ACCOUNT', '-s', '4'])
    with patch.object(runner_module, 'Runner', autospec=True) as runner_class:
        runpy.run_path('main.py', run_name='__main__')
        runner_class.assert_called_once_with(
            token='cli-token', prepared_strategy=strategies.prepare_strategy('ACCOUNT', '4'),
            dst=None,
            params=RunnerParams(debug=False))
        runner_class.return_value.run.assert_called_once_with()


@pytest.mark.parametrize('arguments', [['-s', '4'], ['-s', '4', '--threshold', 'invalid']])
def test_cli_invalid_input(arguments, invest_environment):
    """Missing credentials and invalid CLI arguments cannot start a runner."""
    invest_environment.setattr(sys, 'argv', ['main.py', '--algoritm', 'ACCOUNT', *arguments])
    with patch.object(cli, 'Runner', autospec=True) as runner_class:
        if '--threshold' in arguments:
            with pytest.raises(SystemExit) as raised:
                cli.main()
            assert raised.value.code == 2
        else:
            with pytest.raises(KeyError, match='INVEST_TOKEN'):
                cli.main()
        runner_class.assert_not_called()


def test_local_logging(monkeypatch):
    """Local logging enables the custom user-output level."""
    root_logger = logging.getLogger()
    monkeypatch.setattr(root_logger, 'level', logging.WARNING)
    with patch.object(logging, 'addLevelName', wraps=logging.addLevelName) as add_level:
        logging_config.configure_local_logging()
        add_level.assert_called_once_with(logging_config.IMPORTANT, 'IMPORTANT')
    assert root_logger.level == logging_config.IMPORTANT


@pytest.mark.parametrize(
    'level, expected',
    [(logging.INFO, 'INFO'), (logging_config.IMPORTANT, 'INFO'),
     (logging.WARNING, 'WARN'), (logging.CRITICAL, 'FATAL')],
)
def test_cloud_logging(level, expected, monkeypatch):
    """Repeated cloud calls reuse a single handler and emit Yandex JSON levels."""
    logger = logging_config.logger
    monkeypatch.setattr(logger, 'handlers', [])
    monkeypatch.setattr(logger, 'level', logging.NOTSET)
    monkeypatch.setattr(logger, 'propagate', True)
    logging_config.configure_yc_logging()
    first_handler = logger.handlers[0]
    logging_config.configure_yc_logging()

    assert logger.handlers == [first_handler]
    assert logger.level == logging.INFO
    assert logger.propagate is False
    record = logging.LogRecord(logger.name, level, __file__, 0, 'sync %s', ('complete',), None)
    result = json.loads(first_handler.format(record))
    assert result['message'] == 'sync complete'
    assert result['level'] == expected
    assert result['logger'] == logging_config.LOGGER_NAME


# Exact transcription of the approved reference table, including its precision.
IMOEX_REFERENCE_ROWS = [
    ('AFKS', '7.079', '1958950000', '0.29', '0.7', '0.25', '13867407050'),
    ('AFLT', '32', '795154243', '0.25', '0.8', '0.46', '25444935776'),
    ('ALRS', '18.93', '0', '0.34', '0.4', '0.34', '18960956715.12'),
    ('BSPB', '262.75', '0', '0.21', '1', '0.44', '24574321567.48'),
    ('CBOM', '8.815', '0', '0.22', '0.7', '0.81', '45381165440.19'),
    ('CHMF', '623.8', '0', '0.23', '0.4', '0.86', '48076338809.94'),
    ('CNRU', '657.2', '31068196', '0.4', '1', '0.37', '20418018411.2'),
    ('DOMRF', '2025', '0', '0.1', '1', '0.65', '36427465395'),
    ('ENPG', '288.65', '0', '0.19', '0.7', '0.44', '24525796599.44'),
    ('FLOT', '81.16', '0', '0.16', '0.7', '0.39', '21588504560.58'),
    ('GAZP', '96.39', '0', '0.47', '0.5', '9.59', '536244128481.29'),
    ('GMKN', '117.76', '0', '0.33', '0.4', '4.25', '237615755925.5'),
    ('HEAD', '2712', '0', '0.53', '1', '1.21', '67821713736.48'),
    ('IRAO', '2.34', '23385600000', '0.32', '0.7', '0.98', '54722304000'),
    ('LKOH', '5299.5', '0', '0.59', '0.4226207', '16.38', '915559923795.19'),
    ('MAGN', '20.27', '1340919600', '0.2', '0.6', '0.49', '27180440292'),
    ('MDMG', '1284', '0', '0.27', '0.9', '0.42', '23439904620.12'),
    ('MOEX', '147.55', '0', '0.61', '0.3', '1.1', '61466695428.41'),
    ('MTSS', '174.05', '0', '0.41', '0.4', '1.02', '57042203353.12'),
    ('NLMK', '68.14', '0', '0.21', '0.5', '0.77', '42879742934.03'),
    ('NVTK', '1030.3', '255049704', '0.21', '0.4', '4.7', '262777710031.2'),
    ('OZON', '2919', '0', '0.26', '1', '2.94', '164245038523.02'),
    ('PHOR', '5043', '7511000', '0.29', '0.2', '0.68', '37877973000'),
    ('PLZL', '968', '0', '0.22', '0.4', '2.07', '115909357781.18'),
    ('POSI', '1026', '19227780', '0.27', '1', '0.35', '19727702280'),
    ('RAGR', '69.38', '191749920', '0.2', '1', '0.24', '13303609449.6'),
    ('RENI', '66.84', '0', '0.32', '1', '0.21', '11912551620.86'),
    ('ROSN', '352.25', '0', '0.11', '0.4', '2.94', '164261157985.68'),
    ('RTKM', '38.63', '0', '0.29', '0.7', '0.46', '25744908629.45'),
    ('RUAL', '23.195', '0', '0.18', '0.7', '0.79', '44402649445.24'),
    ('SBER', '272.45', '0', '0.48', '0.2389355', '12.07', '674527989054.97'),
    ('SBERP', '273.27', '477871000', '1', '0.477871', '2.34', '130587808170'),
    ('SNGS', '15.195', '0', '0.25', '0.7', '1.7', '94999885669.93'),
    ('SNGSP', '40.435', '0', '0.73', '0.5', '2.03', '113672059000.76'),
    ('SVCB', '9.33', '0', '0.17', '1', '0.64', '35712990666.22'),
    ('T', '249.18', '0', '0.51', '0.8', '4.88', '272742741595.96'),
    ('TATN', '627.6', '0', '0.49', '0.5', '5.99', '334999839413.4'),
    ('TATNP', '599.7', '116531715', '0.79', '1', '1.25', '69884069485.5'),
    ('TRNFP', '1019.8', '27054825', '0.58', '0.3', '0.49', '27590510535'),
    ('UGLD', '0.722', '0', '0.11', '1', '0.32', '17693096191.71'),
    ('VKCO', '110.9', '0', '0.2', '0.9', '0.2', '11436313241.16'),
    ('VTBR', '52.57', '0', '0.2', '1', '2.43', '135922536097.82'),
    ('X5', '1791.5', '0', '0.29', '1', '2.52', '141091612054.52'),
    ('YDEX', '3547', '0', '0.26', '1', '6.53', '365211069204.54'),
]


@pytest.fixture(name='index_config_data')
def fixture_index_config_data():
    """A valid minimal base whose rounded percentage need not sum to 100."""
    return {
        'allocation_drift_limits': [
            {'budget_from': '0', 'budget_to': None,
             'upper_inclusive': False, 'limit': '0'}],
        'name': 'IMOEX',
        'max_lot_weight_error': '0.05',
        'reserve': '0.01',
        'instruments': [{
            'ticker': 'AFKS',
            'effective_quantity': '1958950000',
            'free_float': '0.29',
            'weight_limit': '0.7',
            'reference_price': '7.079',
            'reference_weight': '0.25',
            'reference_index_capitalization': '13867407050',
        }],
    }


def write_index_config(tmp_path, data, filename='index.json'):
    """Write the caller's JSON payload without contacting external services."""
    path = tmp_path / filename
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_index_registration_default_path(tmp_path, monkeypatch, client):
    """Preparation reads each JSON once; creation only invokes the saved factory."""
    monkeypatch.chdir(tmp_path)
    with patch('autorepeater.index_config.read_index_document',
               wraps=index_config_module.read_index_document) as load, \
            patch('t_tech.invest.Client', autospec=True) as sdk_client:
        prepared = strategies.prepare_strategy('INDEX', 'IMOEX')
        expected_paths = sorted(
            (Path(__file__).resolve().parents[1] / 'autorepeater' / 'configs').glob('*.json'))
        assert load.call_args_list == [call(path) for path in expected_paths]
        load.reset_mock()
        strategy = strategies.create_strategy(prepared)
        assert isinstance(strategy, IndexStrategy)
        assert len(strategy.config.instruments) == 44
        load.assert_not_called()
        sdk_client.assert_not_called()
    assert client.mock_calls == []


def test_index_registration_config_is_fixed(tmp_path, index_config_data, monkeypatch):
    """An override is loaded once; explicit validated configs retain their existing path."""
    path = write_index_config(tmp_path, index_config_data)
    monkeypatch.setenv('IMOEX_CONFIG_PATH', str(path))
    strategy = strategies.create_strategy(strategies.prepare_strategy('INDEX', 'IMOEX'))
    assert strategy.config == load_index_config(path)
    index_config_data['max_lot_weight_error'] = '0.1'
    write_index_config(tmp_path, index_config_data)
    assert strategy.config.max_lot_weight_error == Decimal('0.05')
    updated = strategies.create_strategy(strategies.prepare_strategy('INDEX', 'IMOEX'))
    assert updated.config.max_lot_weight_error == Decimal('0.1')
    monkeypatch.setenv('IMOEX_CONFIG_PATH', str(tmp_path / 'missing.json'))
    assert IndexStrategy(strategy.config).config is strategy.config


@pytest.fixture(name='configured_indexes')
def fixture_configured_indexes(tmp_path, index_config_data, monkeypatch):
    """Two names share the index algorithm but retain independent risk parameters."""
    write_index_config(tmp_path, dict(index_config_data, name='ALPHA'), 'unrelated.json')
    write_index_config(tmp_path, dict(index_config_data, name='BETA', min_position_value='101',
                                      reserve='0.02'),
                       'second.json')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))
    return tmp_path


def test_index_configured_names_and_targets(configured_indexes, client):
    """JSON names, not filenames, select independent configurations without SDK calls."""
    (configured_indexes / '.ignored.json').write_text('invalid', encoding='utf-8')
    (configured_indexes / 'ignored.txt').write_text('invalid', encoding='utf-8')
    nested = configured_indexes / 'nested'
    nested.mkdir()
    (nested / 'ignored.json').write_text('invalid', encoding='utf-8')
    assert set(load_index_configs()) == {'ALPHA', 'BETA'}
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        for name in ['ALPHA', 'BETA']:
            assert isinstance(
                strategies.prepare_strategy('INDEX', name).prepared_source, IndexConfig)
        alpha, beta = [strategies.create_strategy(strategies.prepare_strategy('INDEX', name))
                       for name in ['ALPHA', 'BETA']]
        for name in ['IMOEX', 'unrelated', 'second', 'alpha', ' ALPHA', 'ALPHA ']:
            with pytest.raises(strategies.UnsupportedSourceError, match='unsupported src'):
                strategies.create_strategy(strategies.prepare_strategy('INDEX', name))
        sdk_client.assert_not_called()
    assert isinstance(alpha, IndexStrategy) and isinstance(beta, IndexStrategy)
    assert alpha.config.name == 'ALPHA'
    assert beta.config.name == 'BETA'
    assert alpha.config is not beta.config
    assert alpha.config.instruments[0] is not beta.config.instruments[0]
    snapshot = {'AFKS': IndexQuote('afks', Decimal('10'), 1)}
    assert alpha.build_target(snapshot, Decimal('100')).quantities == {'afks': Decimal('9')}
    assert beta.build_target(snapshot, Decimal('100')).quantities == {'afks': Decimal('9')}
    assert beta.config.min_position_value == Decimal('101')
    assert alpha.config.reserve == Decimal('0.01')
    assert beta.config.reserve == Decimal('0.02')
    assert client.mock_calls == []


def test_index_config_rename_is_not_cached(configured_indexes, index_config_data):
    """Existing instances stay fixed; later construction sees the current JSON names."""
    previous = strategies.create_strategy(strategies.prepare_strategy('INDEX', 'ALPHA'))
    write_index_config(
        configured_indexes, dict(index_config_data, name='RENAMED'), 'unrelated.json')
    renamed = strategies.create_strategy(strategies.prepare_strategy('INDEX', 'RENAMED'))
    assert renamed.config.name == 'RENAMED'
    assert previous.config.name == 'ALPHA'
    with pytest.raises(strategies.UnsupportedSourceError, match='ALPHA'):
        strategies.create_strategy(strategies.prepare_strategy('INDEX', 'ALPHA'))


@pytest.mark.parametrize('collision', ['file', 'factory'])
def test_index_duplicate_names_fail(configured_indexes, index_config_data, monkeypatch, collision):
    """Config duplicates affect their own name; algorithm names are independent."""
    factory = Mock()
    if collision == 'file':
        write_index_config(configured_indexes, dict(index_config_data, name='ALPHA'), 'third.json')
        with pytest.raises(ValueError, match='duplicate strategy name: ALPHA'):
            strategies.prepare_strategy('INDEX', 'ALPHA')
        with pytest.raises(ValueError, match='duplicate strategy name: ALPHA'):
            load_index_configs()
    else:
        monkeypatch.setitem(strategies.ALGORITHMS, 'ALPHA',
                            AlgorithmDefinition(lambda src, context: src, factory))
    assert isinstance(strategies.prepare_strategy('INDEX', 'BETA').prepared_source, IndexConfig)
    factory.assert_not_called()


@pytest.mark.parametrize('case', ['missing', 'file', 'empty', 'both', 'blank_dir', 'blank_file'])
def test_index_config_locations_fail(tmp_path, index_config_data, monkeypatch, case):
    """Explicit bad locations never fall back to bundled configs."""
    path = tmp_path
    if case == 'missing':
        path = tmp_path / 'missing'
    elif case in ['file', 'both']:
        path = write_index_config(tmp_path, index_config_data)
    monkeypatch.setenv('INDEX_CONFIG_DIR', '' if case == 'blank_dir' else str(path))
    if case == 'both':
        monkeypatch.setenv('IMOEX_CONFIG_PATH', str(path))
    elif case == 'blank_file':
        monkeypatch.delenv('INDEX_CONFIG_DIR')
        monkeypatch.setenv('IMOEX_CONFIG_PATH', ' ')
    with pytest.raises(ValueError):
        load_index_configs()
    with pytest.raises(ValueError):
        strategies.prepare_strategy('INDEX', 'IMOEX')
    assert strategies.create_strategy(strategies.prepare_strategy('ACCOUNT', '0004')).src == '0004'


@pytest.mark.parametrize('name', [
    'OTHER', 'imoex', 'INDEX-2', 'A_01', '\u0418\u043d\u0434\u0435\u043a\u0441',
])
def test_index_single_config_name_is_not_an_alias(tmp_path, index_config_data, monkeypatch, name):
    """The legacy single-file override also exposes the exact name in its JSON."""
    path = write_index_config(tmp_path, dict(index_config_data, name=name))
    monkeypatch.setenv('IMOEX_CONFIG_PATH', str(path))
    selected = strategies.create_strategy(strategies.prepare_strategy('INDEX', name))
    assert selected.config.name == name
    with pytest.raises(strategies.UnsupportedSourceError, match='IMOEX'):
        strategies.create_strategy(strategies.prepare_strategy('INDEX', 'IMOEX'))


@pytest.mark.parametrize('entrypoint', ['run', 'run_sync', 'cli', 'query', 'environment'])
@pytest.mark.parametrize('name', ['ALPHA', 'BETA'])
def test_configured_index_entrypoints(configured_indexes, monkeypatch, entrypoint, name):
    """All entrypoints pass the selected config into the unchanged index implementation."""
    assert configured_indexes.is_dir()
    monkeypatch.setenv('INVEST_TOKEN', 'test-token')
    monkeypatch.setenv('SRC_ACCOUNT', name)
    monkeypatch.setenv('ALGORITM', 'INDEX')
    monkeypatch.setenv('DST_ACCOUNT', '5')
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', 'INDEX', '-s', name, '-d', '5'])
    with patch.object(IndexStrategy, 'load_snapshot', autospec=True,
                      side_effect=TestException()) as snapshot, \
            patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'TInvestStrategyData',
                         autospec=True) as data_class, \
            patch.object(runner_module, 'print_all_portfolio', autospec=True), \
            patch.object(runner_module, 'configure_local_logging', autospec=True), \
            patch.object(serverless, 'configure_yc_logging', autospec=True):
        with pytest.raises(TestException):
            if entrypoint == 'cli':
                cli.main()
            elif entrypoint in ['query', 'environment']:
                query = {'algoritm': 'INDEX', 'src': name} if entrypoint == 'query' else {}
                cloud_entrypoint.handler({'queryStringParameters': query}, None)
            else:
                runner = runner_module.Runner(
                    'test-token', strategies.prepare_strategy('INDEX', name), '5')
                getattr(runner, entrypoint)()
        sdk_client.assert_called_once_with(token='test-token', target=INVEST_GRPC_API,
                                           interceptors=[ANY])
        assert snapshot.call_count == 1
        selected, passed_data = snapshot.call_args.args
        assert selected.config.name == name
        minimum = Decimal('101') if name == 'BETA' else Decimal(0)
        assert selected.config.min_position_value == minimum
        data_class.assert_called_once_with(sdk_client.return_value.__enter__.return_value)
        assert passed_data is data_class.return_value


@pytest.mark.parametrize('payload', [None, '{', '{"name": "OTHER"}'])
@pytest.mark.parametrize('entrypoint', ['run', 'run_sync', 'cli', 'cloud'])
def test_index_config_error_before_client(
        tmp_path, payload, entrypoint, invest_environment, client):
    """Invalid config aborts every public launch before constructing an SDK client."""
    path = tmp_path / 'invalid.json'
    if payload is not None:
        path.write_text(payload, encoding='utf-8')
    invest_environment.setenv('IMOEX_CONFIG_PATH', str(path))
    invest_environment.setenv('INVEST_TOKEN', 'test-token')
    invest_environment.setattr(
        sys, 'argv', ['main.py', '--algoritm', 'INDEX', '-s', 'IMOEX', '-d', '5'])
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(serverless, 'configure_yc_logging', autospec=True):
        with pytest.raises((FileNotFoundError, ValueError)):
            strategies.prepare_strategy('INDEX', 'IMOEX')
        with pytest.raises((FileNotFoundError, ValueError)):
            if entrypoint == 'cli':
                cli.main()
            elif entrypoint == 'cloud':
                cloud_entrypoint.handler(
                    {'queryStringParameters': {
                        'algoritm': 'INDEX', 'src': 'IMOEX', 'dst': '5'}}, None)
            else:
                runner = runner_module.Runner(
                    'test-token', strategies.prepare_strategy('INDEX', 'IMOEX'), '5')
                getattr(runner, entrypoint)()
        sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize('position, expected', [
    (None, False),
    (PositionData(account_id='other', money=[],
                  securities=[PositionsSecurities(blocked=0)]), False),
    (PositionData(account_id='5', securities=[], money=[]), False),
    (PositionData(account_id='5', money=[], securities=[PositionsSecurities(blocked=0)]), True),
    (PositionData(account_id='5', money=[], securities=[
        PositionsSecurities(blocked=0), PositionsSecurities(blocked=1)]), False),
    (PositionData(account_id='5', securities=[], money=[
        PositionsMoney(blocked_value=MoneyValue('RUB', 0, 0))]), True),
    (PositionData(account_id='5', securities=[PositionsSecurities(blocked=0)], money=[
        PositionsMoney(blocked_value=MoneyValue('RUB', 0, 0)),
        PositionsMoney(blocked_value=MoneyValue('USD', 0, 0))]), True),
    (PositionData(account_id='5', securities=[], money=[
        PositionsMoney(blocked_value=MoneyValue('RUB', 0, 0)),
        PositionsMoney(blocked_value=MoneyValue('USD', 1, 0))]), False),
    (PositionData(account_id='5', securities=[PositionsSecurities(blocked=0)], money=[
        PositionsMoney(blocked_value=MoneyValue('RUB', 0, 0)),
        PositionsMoney(blocked_value=MoneyValue('USD', 0, 1))]), False),
    (PositionData(account_id='5', securities=[], money=[
        PositionsMoney(blocked_value=MoneyValue('RUB', 0, 1)),
        PositionsMoney(blocked_value=MoneyValue('USD', 0, 0))]), False),
])
def test_index_event_predicate(client, index_sdk_config, position, expected, caplog):
    """Every security and currency must be unblocked, with at least one position."""
    client.operations_stream.positions_stream.side_effect = [iter([
        PositionsStreamResponse(position=position)])]
    strategy = IndexStrategy(index_sdk_config)
    accounts = strategy.event_accounts('5')
    assert accounts == ('5',)
    event = next(TInvestStrategyData(client).position_events(accounts))
    client.reset_mock()
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        assert strategy.should_rebalance(event, '5') is expected
    assert client.mock_calls == []
    assert caplog.records == []


@pytest.fixture(name='index_launch')
def fixture_index_launch(client, tmp_path, index_config_data, invest_environment):
    """Real index launches use a single affordable share and a destination to rotate."""
    path = write_index_config(tmp_path, index_config_data)
    invest_environment.setenv('IMOEX_CONFIG_PATH', str(path))
    invest_environment.setenv('INVEST_TOKEN', 'test-token')
    invest_environment.setenv('SRC_ACCOUNT', 'IMOEX')
    invest_environment.setenv('ALGORITM', 'INDEX')
    invest_environment.setenv('DST_ACCOUNT', '5')
    invest_environment.setattr(
        sys, 'argv', ['main.py', '--algoritm', 'INDEX', '-s', 'IMOEX', '-d', '5'])
    found = {
        'AFKS': FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='AFKS', uid='2', instrument_type='share', class_code='TQBR')]),
        '1': FindInstrumentResponse(instruments=[InstrumentShort(ticker='OLD', name='old share')]),
    }
    client.instruments.find_instrument.side_effect = lambda **kwargs: found[kwargs['query']]
    instruments = {
        uid: InstrumentResponse(instrument=Instrument(
            uid=uid, ticker=ticker, instrument_type='share', class_code='TQBR',
            name=ticker, lot=1, currency='rub', api_trade_available_flag=True,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
        for uid, ticker in [('1', 'OLD'), ('2', 'AFKS')]
    }
    client.instruments.get_instrument_by.side_effect = lambda **kwargs: instruments[kwargs['id']]
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(last_prices=[
        LastPrice(instrument_uid='2', price=Quotation(units=9, nano=0))])
    client.operations.get_portfolio.side_effect = None
    client.operations.get_portfolio.return_value = PortfolioResponse(positions=[
        PortfolioPosition(instrument_type='share', instrument_uid='1',
                          quantity=Quotation(units=5, nano=0),
                          current_price=MoneyValue('RUB', 4, 0)),
        PortfolioPosition(instrument_type='currency', instrument_uid='cash',
                          quantity=Quotation(units=80, nano=0),
                          current_price=MoneyValue('RUB', 1, 0)),
    ])
    client.users.get_accounts.return_value = GetAccountsResponse(accounts=[])
    client.operations_stream.positions_stream.side_effect = [iter([
        PositionsStreamResponse(position=None),
        PositionsStreamResponse(position=PositionData(
            account_id='5', money=[],
            securities=[PositionsSecurities(instrument_uid='1', blocked=0)])),
    ]), TestException()]
    return invest_environment


def test_index_config_success(tmp_path, index_config_data):
    """Preserve every reference field as Decimal without reapplying coefficients."""
    config = load_index_config(write_index_config(tmp_path, index_config_data))
    assert config == IndexConfig(
        name='IMOEX', max_lot_weight_error=Decimal('0.05'), reserve=Decimal('0.01'),
        instruments=[IndexInstrument(
            ticker='AFKS',
            effective_quantity=Decimal('1958950000'),
            free_float=Decimal('0.29'),
            weight_limit=Decimal('0.7'),
            reference_price=Decimal('7.079'),
            reference_weight=Decimal('0.25'),
            reference_index_capitalization=Decimal('13867407050'),
        )],
        allocation_drift_limits=(AllocationDriftRange(Decimal('0'), None, False, Decimal('0')),))
    assert isinstance(config.max_lot_weight_error, Decimal)
    assert config.min_position_value == Decimal(0)
    assert config.reserve == Decimal('0.01')
    assert all(isinstance(value, Decimal)
               for field, value in vars(config.instruments[0]).items() if field != 'ticker')


def test_index_config_packaged_reference():
    """All 44 rows retain the approved values and rounded reference percentages."""
    path = Path(__file__).resolve().parents[1] / 'autorepeater' / 'configs' / 'imoex.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    fields = (
        'ticker', 'reference_price', 'effective_quantity', 'free_float',
        'weight_limit', 'reference_weight', 'reference_index_capitalization',
    )
    assert [tuple(row[field] for field in fields) for row in raw['instruments']] == (
        IMOEX_REFERENCE_ROWS)
    config = load_index_config(path)
    assert config.name == 'IMOEX'
    assert config.max_lot_weight_error == Decimal('0.05')
    assert config.min_position_value == Decimal('3000')
    assert len(config.instruments) == 44
    assert sum(item.reference_weight for item in config.instruments) == Decimal('99.99')
    total = sum(item.reference_index_capitalization for item in config.instruments)
    for item in config.instruments:
        weight = item.reference_index_capitalization * 100 / total
        assert abs(weight - item.reference_weight) <= Decimal('0.005'), item.ticker
        if item.effective_quantity:
            assert abs(item.effective_quantity * item.reference_price
                       - item.reference_index_capitalization) <= Decimal('0.005'), item.ticker
    alrs = next(item for item in config.instruments if item.ticker == 'ALRS')
    assert alrs.effective_quantity == Decimal('0')
    assert alrs.reference_index_capitalization == Decimal('18960956715.12')
    assert alrs.reference_price == Decimal('18.93')


@pytest.mark.parametrize('payload', ['', '{', '{"name": "IMOEX",}', 'not json'])
def test_index_config_corrupt_file(tmp_path, payload):
    """Malformed JSON cannot become an empty composition."""
    path = tmp_path / 'index.json'
    path.write_text(payload, encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        load_index_config(path)


def test_index_config_missing_file(tmp_path):
    """File errors propagate instead of silently substituting a default base."""
    with pytest.raises(FileNotFoundError):
        load_index_config(tmp_path / 'missing.json')


@pytest.mark.parametrize('value', [
    None, True, False, 0, 0.0005, [], {}, '', 'invalid', 'NaN', 'sNaN',
    'Infinity', '-Infinity', '-0.0001', '1', '1.01',
])
def test_index_config_invalid_reserve(tmp_path, index_config_data, value):
    """Reserve is a finite decimal string in [0, 1), not a percentage number."""
    index_config_data['reserve'] = value
    with pytest.raises(ValueError, match='reserve'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize('value', ['0', '0.0005', '0.01', '0.999999999'])
def test_index_config_reserve(tmp_path, index_config_data, value):
    """Each config owns its reserve regardless of the number of instruments."""
    index_config_data['reserve'] = value
    config = load_index_config(write_index_config(tmp_path, index_config_data))
    assert config.reserve == Decimal(value)
    assert IndexStrategy(config).config.reserve == Decimal(value)


def test_bundled_reserves():
    """GOLD and OBLG retain a sandbox-tested 0.6% margin; TMON stays unchanged."""
    configs = load_index_configs()
    assert configs['IMOEX'].reserve == Decimal('0.01')
    assert configs['GOLD'].reserve == configs['OBLG'].reserve == Decimal('0.006')
    assert 'BOND' not in configs
    for name in ('GOLD', 'OBLG'):
        assert [item.ticker for item in configs[name].instruments] == [name]
    assert configs['TMON'].reserve == Decimal('0.0005')


@pytest.mark.parametrize('payload', [None, [], 'IMOEX', 1, True])
def test_index_config_invalid_root(tmp_path, payload):
    """The root must be an object."""
    with pytest.raises(ValueError, match='index config: expected an object'):
        load_index_config(write_index_config(tmp_path, payload))


@pytest.mark.parametrize('field', ['name', 'max_lot_weight_error', 'instruments', 'reserve'])
def test_index_config_missing_root_field(tmp_path, index_config_data, field):
    """All required top-level fields, including reserve, must be explicit."""
    del index_config_data[field]
    with pytest.raises(ValueError, match=field):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize('name', [None, 1, True, [], {}, '', 'IMOEX ', ' IMOEX',
                                  'my index', 'MY\tINDEX', 'MY\nINDEX'])
def test_index_config_invalid_name(tmp_path, index_config_data, name):
    """Names cannot be blank or contain whitespace."""
    index_config_data['name'] = name
    with pytest.raises(ValueError, match='name'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize('instruments', [None, [], {}, 'AFKS', 1, True])
def test_index_config_invalid_composition(tmp_path, index_config_data, instruments):
    """A composition is a nonempty array, not a partial or absent base."""
    index_config_data['instruments'] = instruments
    with pytest.raises(ValueError, match='instruments'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize('record', [None, [], 'AFKS', 1, True])
def test_index_config_invalid_record(tmp_path, index_config_data, record):
    """Every composition entry must be an object."""
    index_config_data['instruments'].append(record)
    with pytest.raises(ValueError, match=r'instruments\[1\]'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize(
    'field', ['ticker', 'effective_quantity', 'free_float', 'weight_limit',
              'reference_price', 'reference_weight', 'reference_index_capitalization'],
)
def test_index_config_missing_instrument_field(tmp_path, index_config_data, field):
    """Missing reference fields identify the offending field and entry."""
    del index_config_data['instruments'][0][field]
    with pytest.raises(ValueError, match=rf'instruments\[0\].*{field}'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize('ticker', [None, '', ' \t', 1, True, [], {}])
def test_index_config_invalid_ticker(tmp_path, index_config_data, ticker):
    """Tickers must be nonblank strings."""
    index_config_data['instruments'][0]['ticker'] = ticker
    with pytest.raises(ValueError, match='ticker'):
        load_index_config(write_index_config(tmp_path, index_config_data))


def test_index_config_duplicate_ticker(tmp_path, index_config_data):
    """Duplicate tickers are rejected even when the other fields differ."""
    duplicate = dict(index_config_data['instruments'][0], reference_price='8')
    index_config_data['instruments'].append(duplicate)
    with pytest.raises(ValueError, match='duplicate ticker: AFKS'):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize(
    'field', ['max_lot_weight_error', 'min_position_value', 'effective_quantity', 'free_float',
              'weight_limit', 'reference_price', 'reference_weight',
              'reference_index_capitalization'],
)
@pytest.mark.parametrize(
    'value', [None, 0, 1, 0.5, True, False, [], {}, '', 'invalid',
              'NaN', 'sNaN', 'Infinity', '-Infinity', float('nan'), float('inf')],
)
def test_index_config_invalid_decimal(tmp_path, index_config_data, field, value):
    """Every decimal must be a finite string; JSON numeric literals are invalid."""
    record = (index_config_data if field in ['max_lot_weight_error', 'min_position_value']
              else index_config_data['instruments'][0])
    record[field] = value
    with pytest.raises(ValueError, match=field):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize(
    'field, value',
    [
        ('max_lot_weight_error', '-0.01'), ('max_lot_weight_error', '1'),
        ('min_position_value', '-0.01'),
        ('max_lot_weight_error', '1.01'), ('effective_quantity', '-1'),
        ('free_float', '-0.1'), ('free_float', '0'), ('free_float', '1.01'),
        ('weight_limit', '-0.1'), ('weight_limit', '0'), ('weight_limit', '1.01'),
        ('reference_price', '0'), ('reference_price', '-1'),
        ('reference_weight', '-0.01'), ('reference_weight', '100.01'),
        ('reference_index_capitalization', '0'),
        ('reference_index_capitalization', '-1'),
    ],
)
def test_index_config_invalid_range(tmp_path, index_config_data, field, value):
    """Reject excluded endpoints and values outside each documented range."""
    record = (index_config_data if field in ['max_lot_weight_error', 'min_position_value']
              else index_config_data['instruments'][0])
    record[field] = value
    with pytest.raises(ValueError, match=field):
        load_index_config(write_index_config(tmp_path, index_config_data))


@pytest.mark.parametrize(
    'field, value',
    [
        ('max_lot_weight_error', '0'), ('max_lot_weight_error', '0.999999999'),
        ('min_position_value', '0'), ('min_position_value', '0.001'),
        ('min_position_value', '3000'),
        ('effective_quantity', '0'), ('effective_quantity', '0.001'),
        ('free_float', '0.000000001'), ('free_float', '1'),
        ('weight_limit', '0.000000001'), ('weight_limit', '1'),
        ('reference_price', '0.000000001'),
        ('reference_weight', '0'), ('reference_weight', '100'),
        ('reference_index_capitalization', '0.000000001'),
    ],
)
def test_index_config_valid_boundaries(tmp_path, index_config_data, field, value):
    """Allow included endpoints, zero reference quantity, and rounded percentages."""
    record = (index_config_data if field in ['max_lot_weight_error', 'min_position_value']
              else index_config_data['instruments'][0])
    record[field] = value
    config = load_index_config(write_index_config(tmp_path, index_config_data))
    result = (config if field in ['max_lot_weight_error', 'min_position_value']
              else config.instruments[0])
    assert getattr(result, field) == Decimal(value)


def index_calculation_case(rows, threshold='0.05'):
    """Explicit synthetic base: ticker, capitalization, unit price, lot size."""
    instruments = [IndexInstrument(
        ticker=ticker, effective_quantity=Decimal('0'), free_float=Decimal('0.25'),
        weight_limit=Decimal('0.5'), reference_price=Decimal(price),
        reference_weight=Decimal('0'), reference_index_capitalization=Decimal(capitalization))
        for ticker, capitalization, price, _ in rows]
    snapshot = {ticker: IndexQuote(f'uid-{ticker}', Decimal(price), lot)
                for ticker, _, price, lot in rows}
    return IndexConfig('IMOEX', Decimal(threshold), instruments, reserve=Decimal('0.01'),
                       allocation_drift_limits=(
            AllocationDriftRange(Decimal('0'), None, False, Decimal('0')),)), snapshot


def test_index_calculation_reference_weights():
    """Reference prices recover the approved weights, including zero reference quantities."""
    path = Path(__file__).resolve().parents[1] / 'autorepeater' / 'configs' / 'imoex.json'
    config = load_index_config(path)
    snapshot = {item.ticker: IndexQuote(f'uid-{item.ticker}', item.reference_price, 1)
                for item in config.instruments}
    budget = Decimal('1000000000000')
    result = calculate_index_target(config, snapshot, budget)
    assert len(result.passes) == 1
    assert len(result.target.quantities) == 44
    assert result.capitalizations == {
        item.ticker: item.reference_index_capitalization for item in config.instruments}
    for item in config.instruments:
        allocation = result.passes[0][item.ticker]
        assert abs(allocation.weight * 100 - item.reference_weight) <= Decimal('0.005')
        assert allocation.error <= config.max_lot_weight_error
    assert sum(result.target.quantities[uid] * price
               for uid, price in result.target.prices.items()) <= budget
    validate_target(result.target)


def test_index_calculation_price_formula_and_order():
    """Multiply then divide; rounded reference percentages and coefficients are not factors."""
    config, snapshot = index_calculation_case([('A', '1', '3', 1), ('B', '2', '3', 1)])
    result = calculate_index_target(config, snapshot, Decimal('900'))
    assert result.capitalizations == {'A': Decimal('1'), 'B': Decimal('2')}
    assert result.passes[0]['A'].weight == Decimal(1) / 3
    assert result.target == TargetPortfolio(
        {'uid-A': Decimal('100'), 'uid-B': Decimal('200')},
        {'uid-A': Decimal('3'), 'uid-B': Decimal('3')})
    snapshot['A'].price = Decimal('6')
    changed = calculate_index_target(config, snapshot, Decimal('900'))
    assert changed.capitalizations == {'A': Decimal('2'), 'B': Decimal('2')}
    assert changed.passes[0]['A'].weight == changed.passes[0]['B'].weight == Decimal('0.5')
    assert changed.target.quantities == {'uid-A': Decimal('75'), 'uid-B': Decimal('150')}


def test_index_calculation_units_prices_and_reserve():
    """The supplied budget is final; units, not lots, enter the engine's target."""
    config, snapshot = index_calculation_case([('A', '1', '5', 10), ('B', '1', '2', 5)])
    target = build_index_target(config, snapshot, Decimal('100'))
    assert target == TargetPortfolio(
        {'uid-A': Decimal('10'), 'uid-B': Decimal('25')},
        {'uid-A': Decimal('5'), 'uid-B': Decimal('2')})
    assert all(isinstance(quantity, Decimal) for quantity in target.quantities.values())
    assert sum(target.quantities[uid] * price for uid, price in target.prices.items()) == 100
    validate_target(target)


@pytest.mark.parametrize('budget, lots', [
    ('0.01', 0), ('29.999999999', 0), ('30', 1), ('45', 1),
    ('59.999999999', 1), ('60', 2), ('299', 9), ('300', 10),
])
def test_single_instrument_maximum_lots(budget, lots):
    """One configured ticker ignores tail filters and fills all affordable lots."""
    config, snapshot = index_calculation_case([('ONLY', '1', '3', 10)], threshold='0')
    config.min_position_value = Decimal('1000000')
    result = calculate_index_target(config, snapshot, Decimal(budget))
    assert not result.min_exclusions
    assert result.target.quantities == ({'uid-ONLY': Decimal(lots * 10)} if lots else {})
    assert result.target.prices == ({'uid-ONLY': Decimal('3')} if lots else {})
    assert Decimal(lots * 30) <= Decimal(budget) < Decimal((lots + 1) * 30)
    assert IndexStrategy(config).config.reserve == Decimal('0.01')


@pytest.fixture(name='single_fund')
def fixture_single_fund(client, request):
    """Real bundled config with one API-enabled ETF and an unavailable alternative."""
    name = request.param
    strategy = strategies.create_strategy(strategies.prepare_strategy('INDEX', name))
    assert strategy.config.name == name
    assert [item.ticker for item in strategy.config.instruments] == [name]
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=[
        InstrumentShort(ticker=name, uid='wrong', instrument_type='etf', class_code='SPEQ'),
        InstrumentShort(ticker=name, uid='fund', instrument_type='etf', class_code='TQBR'),
    ])
    client.instruments.find_instrument.side_effect = lambda **kwargs: (
        FindInstrumentResponse(instruments=[InstrumentShort(ticker=name, name=name)])
        if kwargs['query'] == 'fund' else client.instruments.find_instrument.return_value)
    client.instruments.get_instrument_by.side_effect = None
    client.instruments.get_instrument_by.return_value = InstrumentResponse(instrument=Instrument(
        uid='fund', ticker=name, name=name, instrument_type='etf', class_code='TQBR',
        currency='rub', lot=10, api_trade_available_flag=True,
        trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
    unavailable = InstrumentResponse(instrument=Instrument(
        uid='wrong', ticker=name, name=name, instrument_type='etf', class_code='SPEQ',
        currency='rub', lot=1, api_trade_available_flag=False))
    client.instruments.get_instrument_by.side_effect = lambda **kwargs: (
        unavailable if kwargs['id'] == 'wrong'
        else client.instruments.get_instrument_by.return_value)
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(last_prices=[
        LastPrice(instrument_uid='fund', price=Quotation(3, 0),
                  time=datetime(2026, 9, 29, 10, tzinfo=timezone.utc))])
    client.operations.get_portfolio.side_effect = None
    client.operations.get_portfolio.return_value = PortfolioResponse(positions=[
        PortfolioPosition(instrument_uid='fund', instrument_type='etf',
                          quantity=Quotation(20, 0), current_price=MoneyValue('rub', 3, 0)),
        PortfolioPosition(instrument_uid='cash', instrument_type='currency',
                          quantity=Quotation(240, 0), current_price=MoneyValue('rub', 1, 0)),
    ])
    return strategy


@pytest.mark.parametrize('single_fund', ['GOLD', 'OBLG', 'TMON'], indirect=True)
@pytest.mark.parametrize('case', ['wrong_board', 'wrong_type'])
def test_single_fund_invalid_metadata(single_fund, client, case):
    """Full metadata must describe the same board and type as the search candidate."""
    instrument = client.instruments.get_instrument_by.return_value.instrument
    if case == 'wrong_type':
        instrument.instrument_type = 'share'
    else:
        instrument.class_code = 'TQTF'
    with pytest.raises(ValueError, match='index instrument'):
        single_fund.load_snapshot(TInvestStrategyData(client))
    client.market_data.get_last_prices.assert_not_called()
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('minimum, selected, excluded', [
    ('0', ['A', 'B', 'C'], []), ('20', ['A', 'B', 'C'], []),
    ('20.0001', ['A', 'B'], ['C']), ('30', ['A', 'B'], ['C']),
    ('30.0001', ['A'], ['B', 'C']), ('50', ['A'], ['B', 'C']),
    ('50.0001', [], ['A', 'B', 'C']),
])
def test_index_calculation_minimum_full_index(minimum, selected, excluded):
    """Inclusive minimum uses full-index weights once, with no currency restriction."""
    config, snapshot = index_calculation_case([
        ('C', '20', '1', 1), ('B', '30', '1', 1), ('A', '50', '1', 1)])
    config.min_position_value = Decimal(minimum)
    for quote in snapshot.values():
        quote.currency = 'usd'
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert result.min_exclusions == excluded
    assert list(result.target.quantities) == [f'uid-{ticker}' for ticker in selected]
    assert [list(rows) for rows in result.passes] == ([selected] if selected else [])
    reversed_config = replace(config, instruments=config.instruments[::-1])
    assert calculate_index_target(reversed_config, snapshot, Decimal('100')) == result
    if selected:
        assert sum(result.target.quantities.values()) == Decimal('100')
    else:
        assert result.target == TargetPortfolio({}, {})


def test_index_calculation_minimum_before_renormalization():
    """C would exceed the minimum after removing D, but must never enter the prefix."""
    config, snapshot = index_calculation_case([
        ('D', '10', '1', 1), ('C', '20', '1', 1),
        ('B', '35', '1', 1), ('A', '35', '1', 1)])
    config.min_position_value = Decimal('21')
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert list(result.passes[0]) == ['A', 'B']
    assert result.min_exclusions == ['C', 'D']
    assert result.target.quantities == {'uid-A': Decimal('50'), 'uid-B': Decimal('50')}
    assert all(row.weight == Decimal('0.5') for row in result.passes[0].values())


def test_index_calculation_minimum_then_tail_cut():
    """Later renormalizations cannot revive either a minimum or an error exclusion."""
    config, snapshot = index_calculation_case([
        ('A', '60', '6', 1), ('B', '25', '100', 1),
        ('C', '10', '1', 1), ('D', '5', '1', 1)])
    config.min_position_value = Decimal('10')
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert result.min_exclusions == ['D']
    assert [list(rows) for rows in result.passes] == [['A', 'B', 'C'], ['A']]
    assert result.passes[0]['B'].exclusion_reason == 'zero_lots'
    assert result.passes[0]['C'].exclusion_reason == 'prefix_tail'
    assert result.target.quantities == {'uid-A': Decimal('16')}


@pytest.mark.parametrize('price, error', [('21', '0.05'), ('22', '0.1'), ('24', '0.2')])
@pytest.mark.parametrize('offset, included', [('-0.001', False), ('0', True), ('0.001', True)])
def test_index_calculation_configurable_threshold(price, error, offset, included):
    """Below one ideal lot, inclusion uses the computed error <= the threshold."""
    threshold = Decimal(error) + Decimal(offset)
    config, snapshot = index_calculation_case(
        [('A', '80', '30', 1), ('B', '20', price, 1)], str(threshold))
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert result.passes[0]['B'].ideal_lots < 1
    assert result.passes[0]['B'].error == Decimal(error)
    assert result.target.quantities == (
        {'uid-A': Decimal('2'), 'uid-B': Decimal('1')} if included else {'uid-A': Decimal('3')})
    assert result.target.prices == (
        {'uid-A': Decimal('30'), 'uid-B': Decimal(price)} if included else {'uid-A': Decimal('30')})
    assert result.passes[0]['B'].exclusion_reason == ('' if included else 'weight_error')


def test_index_calculation_decimal_boundary_is_not_corrected():
    """The accepted finite-precision artifact can exclude a mathematical 5% boundary."""
    config, snapshot = index_calculation_case([('A', '1', '21', 1), ('B', '2', '3', 1)])
    result = calculate_index_target(config, snapshot, Decimal('60'))
    assert result.passes[0]['A'].error == Decimal('0.05000000000000000000000000011')
    assert result.passes[0]['A'].exclusion_reason == 'weight_error'
    assert result.target.quantities == {'uid-B': Decimal('20')}


@pytest.mark.parametrize(
    'budget, ideal, lots, expected',
    [('20', '0.5', [0, 0], {}),
     ('60', '1.5', [1, 2], {'uid-A': Decimal('1'), 'uid-B': Decimal('2')}),
     ('100', '2.5', [2, 2], {'uid-A': Decimal('2'), 'uid-B': Decimal('2')})],
)
def test_index_calculation_half_even(budget, ideal, lots, expected):
    """Half-lots round to even; equal cancellation penalties cancel ticker A first."""
    config, snapshot = index_calculation_case([('A', '1', '20', 1), ('B', '1', '20', 1)], '0.5')
    result = calculate_index_target(config, snapshot, Decimal(budget))
    assert [row.ideal_lots for row in result.passes[0].values()] == [Decimal(ideal)] * 2
    assert [row.lots for row in result.passes[0].values()] == lots
    assert result.target.quantities == expected
    assert len(result.passes) == 1
    if not expected:
        assert not result.target.prices
        assert [row.exclusion_reason for row in result.passes[0].values()] == [
            'zero_lots', 'prefix_tail']


def test_index_calculation_budget_and_ticker_tiebreak():
    """Independent rounding costs 24 > 20; equal penalties cancel ticker A first."""
    config, snapshot = index_calculation_case([('B', '1', '6', 1), ('A', '1', '6', 1)], '0.5')
    result = calculate_index_target(config, snapshot, Decimal('20'))
    assert result.target.quantities == {'uid-A': Decimal('1'), 'uid-B': Decimal('2')}
    assert [row.lots for row in result.passes[0].values()] == [1, 2]
    assert sum(result.target.quantities[uid] * price
               for uid, price in result.target.prices.items()) == Decimal('18')


def test_index_calculation_cancellation_penalty_priority():
    """Cancel the smaller relative-error increase, regardless of capitalization order."""
    config, snapshot = index_calculation_case([('A', '2', '24', 1), ('B', '3', '34', 1)], '0.5')
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert result.target.quantities == {'uid-A': Decimal('1'), 'uid-B': Decimal('2')}
    assert result.passes[0]['A'].actual_weight == Decimal('0.24')
    assert result.passes[0]['B'].actual_weight == Decimal('0.68')


def test_index_calculation_renormalization_and_larger_budget():
    """Drop C and keep B above one ideal lot; a larger-budget call restarts from all rows."""
    config, snapshot = index_calculation_case([
        ('A', '60', '6', 1), ('B', '30', '30', 1), ('C', '10', '100', 1)])
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert [list(rows) for rows in result.passes] == [['A', 'B', 'C'], ['A', 'B']]
    assert result.passes[0]['C'].exclusion_reason == 'zero_lots'
    assert result.passes[1]['B'].exclusion_reason == ''
    assert result.passes[1]['B'].ideal_lots > 1
    assert result.passes[1]['B'].error > config.max_lot_weight_error
    assert result.target == TargetPortfolio(
        {'uid-A': Decimal('11'), 'uid-B': Decimal('1')},
        {'uid-A': Decimal('6'), 'uid-B': Decimal('30')})
    larger = calculate_index_target(config, snapshot, Decimal('3000'))
    assert larger.target.quantities == {
        'uid-A': Decimal('300'), 'uid-B': Decimal('30'), 'uid-C': Decimal('3')}
    assert len(larger.passes) == 1
    assert calculate_index_target(config, snapshot, Decimal('100')) == result


def test_index_calculation_failure_cuts_tail():
    """The first failure cuts the whole suffix, even if a fresh subset could fit."""
    config, snapshot = index_calculation_case([('A', '80', '85', 1), ('B', '20', '15', 1)])
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert len(result.passes) == 1
    assert [row.lots for row in result.passes[0].values()] == [1, 1]
    assert result.passes[0]['A'].ideal_lots < 1
    assert result.passes[0]['B'].ideal_lots > 1
    assert [row.exclusion_reason for row in result.passes[0].values()] == [
        'weight_error', 'prefix_tail']
    assert result.target == TargetPortfolio({}, {})


@pytest.mark.parametrize('price, lots', [
    ('100', '1'), ('99', '1'), ('80', '1'), ('60', '1'), ('50', '2'), ('40', '2'), ('38', '2'),
])
def test_index_calculation_large_positions_ignore_threshold(price, lots):
    """Ideal >= 1 ignores even a zero threshold, including a cancelled ceil."""
    config, snapshot = index_calculation_case([('A', '1', price, 1)], '0')
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert result.target.quantities == {'uid-A': Decimal(lots)}
    assert result.passes[0]['A'].ideal_lots >= 1
    assert result.passes[0]['A'].exclusion_reason == ''
    if price not in ['50', '100']:
        assert result.passes[0]['A'].error > config.max_lot_weight_error


def test_index_calculation_cancellation_above_one_ignores_error():
    """A budget cancellation may exceed the error limit when ideal lots are >= 1."""
    config, snapshot = index_calculation_case([('A', '1', '6', 1), ('B', '1', '6', 1)], '0.25')
    result = calculate_index_target(config, snapshot, Decimal('20'))
    first, second = result.passes[0].values()
    assert 1 < first.ideal_lots < 2 and 1 < second.ideal_lots < 2
    assert (first.lots, first.error, first.exclusion_reason) == (1, Decimal('0.4'), '')
    assert (second.lots, second.error, second.exclusion_reason) == (
        2, Decimal('0.2'), '')
    assert result.target.quantities == {'uid-A': Decimal('1'), 'uid-B': Decimal('2')}


def test_index_calculation_cancels_multiple_ceils_to_zero():
    """Keep cancelling until affordable; zero lots stop the prefix regardless of threshold."""
    config, snapshot = index_calculation_case(
        [(ticker, '1', '10', 1) for ticker in ['D', 'C', 'B', 'A']], '0.9')
    result = calculate_index_target(config, snapshot, Decimal('24'))
    assert list(result.passes[0]) == ['A', 'B', 'C', 'D']
    assert [row.lots for row in result.passes[0].values()] == [0, 0, 1, 1]
    assert result.passes[0]['A'].exclusion_reason == 'zero_lots'
    assert result.target == TargetPortfolio({}, {})
    assert len(result.passes) == 1


def test_index_calculation_prefix_never_reintroduces_suitable_tail():
    """A suitable smaller share leaves with the first failure and cannot return."""
    config, snapshot = index_calculation_case([
        ('C', '10', '10', 1), ('B', '30', '100', 1), ('A', '60', '6', 1)])
    result = calculate_index_target(config, snapshot, Decimal('100'))
    assert [list(rows) for rows in result.passes] == [['A', 'B', 'C'], ['A']]
    assert result.passes[0]['C'].error == 0
    assert result.passes[0]['B'].exclusion_reason == 'zero_lots'
    assert result.passes[0]['C'].exclusion_reason == 'prefix_tail'
    assert result.target.quantities == {'uid-A': Decimal('16')}


@pytest.mark.parametrize('budget', ['1', '100', '3000', '152460', '154000'])
def test_index_calculation_order_and_invariants(budget):
    """Every pass is deterministic and affordable; survivors meet the configured error."""
    config, snapshot = index_calculation_case([
        ('C', '10', '100', 1), ('A', '60', '2', 3), ('B', '30', '0.6', 10)])
    budget = Decimal(budget)
    result = calculate_index_target(config, snapshot, budget)
    reversed_config = IndexConfig(
        config.name, config.max_lot_weight_error, config.instruments[::-1], reserve=config.reserve,
        allocation_drift_limits=(
                AllocationDriftRange(Decimal('0'), None, False, Decimal('0')),))
    reversed_snapshot = dict(reversed(list(snapshot.items())))
    assert calculate_index_target(reversed_config, reversed_snapshot, budget) == result
    assert len(result.passes) <= len(config.instruments)
    for rows in result.passes:
        assert sum(row.lots * snapshot[ticker].price * snapshot[ticker].lot
                   for ticker, row in rows.items()) <= budget
    for ticker, quote in snapshot.items():
        if quote.uid in result.target.quantities:
            assert result.target.quantities[quote.uid] % quote.lot == 0
            assert result.target.quantities[quote.uid] > 0
            allocation = result.passes[-1][ticker]
            assert allocation.ideal_lots >= 1 or allocation.error <= config.max_lot_weight_error
    validate_target(result.target)


@pytest.mark.parametrize(
    'budget', [Decimal('0'), Decimal('-1'), Decimal('NaN'), Decimal('sNaN'),
               Decimal('Infinity'), Decimal('-Infinity'), None, '100', 100, 100.0])
def test_index_calculation_invalid_budget(budget):
    """A bad budget fails before even inspecting an incomplete snapshot."""
    config, _ = index_calculation_case([('A', '1', '1', 1)])
    with pytest.raises(ValueError, match='budget'):
        build_index_target(config, {}, budget)


def test_index_calculation_incomplete_snapshot():
    """Missing constituents are errors, never a partial target."""
    config, snapshot = index_calculation_case([('A', '1', '1', 1), ('B', '1', '1', 1)])
    del snapshot['B']
    with pytest.raises(ValueError, match='snapshot: B'):
        build_index_target(config, snapshot, Decimal('100'))


@pytest.mark.parametrize('price', [Decimal('0'), Decimal('-1'), Decimal('NaN'), Decimal('sNaN'),
                                   Decimal('Infinity'), Decimal('-Infinity'), None, '1', 1, 1.0])
def test_index_calculation_invalid_price(price):
    """Every constituent needs a positive finite Decimal price before allocation."""
    config, snapshot = index_calculation_case([('A', '1', '1', 1)])
    snapshot['A'].price = price
    with pytest.raises(ValueError, match='price: A'):
        build_index_target(config, snapshot, Decimal('100'))


@pytest.mark.parametrize('lot', [0, -1, None, '1', Decimal('1'), 1.0, 1.5, True, False])
def test_index_calculation_invalid_lot(lot):
    """The SDK lot size must be a positive integer, not a coercible value."""
    config, snapshot = index_calculation_case([('A', '1', '1', 1)])
    snapshot['A'].lot = lot
    with pytest.raises(ValueError, match='lot: A'):
        build_index_target(config, snapshot, Decimal('100'))


@pytest.mark.parametrize('uid', ['', None, 1, 'uid-A'])
def test_index_calculation_invalid_uid(uid):
    """Invalid or duplicate UIDs must not overwrite part of the target."""
    config, snapshot = index_calculation_case([('A', '1', '1', 1), ('B', '1', '1', 1)])
    snapshot['B'].uid = uid
    with pytest.raises(ValueError, match='UID: B'):
        build_index_target(config, snapshot, Decimal('100'))


@pytest.fixture(name='index_sdk_config')
def fixture_index_sdk_config(client):
    """Complete two-share SDK responses with deliberately reversed quote order."""
    config, _ = index_calculation_case([('A', '1', '10', 2), ('B', '1', '20', 1)])
    client.instruments.find_instrument.side_effect = [
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='A-extra', instrument_type='share',
                            class_code='TQBR', uid='ignored'),
            InstrumentShort(ticker='A', instrument_type='bond', class_code='TQBR', uid='bond'),
            InstrumentShort(ticker='A', instrument_type='share', class_code='TQBR', uid='uid-A',
                            api_trade_available_flag=False)]),
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='B', instrument_type='share', class_code='TQBR', uid='uid-B')]),
    ]
    client.instruments.get_instrument_by.side_effect = [
        InstrumentResponse(instrument=Instrument(
            uid='uid-A', ticker='A', instrument_type='share',
            class_code='TQBR', lot=2, currency='rub',
            api_trade_available_flag=True,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING)),
        InstrumentResponse(instrument=Instrument(
            uid='uid-B', ticker='B', instrument_type='share',
            class_code='TQBR', lot=1, currency='rub', api_trade_available_flag=True,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_BREAK_IN_TRADING)),
    ]
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(last_prices=[
        LastPrice(instrument_uid='uid-B', price=Quotation(units=20, nano=0),
                  time=datetime(2026, 9, 28, 10, tzinfo=timezone.utc)),
        LastPrice(instrument_uid='uid-A', price=Quotation(units=10, nano=0),
                  time=datetime(2026, 9, 29, 10, tzinfo=timezone.utc)),
    ])
    return config


def test_index_snapshot_complete_and_pure_target(client, index_sdk_config):
    """Full API permission controls selection; session breaks do not change the target."""
    strategy = IndexStrategy(index_sdk_config)
    assert client.mock_calls == []
    snapshot = strategy.load_snapshot(TInvestStrategyData(client))
    assert snapshot == {
        'A': IndexQuote('uid-A', Decimal('10'), 2, 'rub',
                        datetime(2026, 9, 29, 10, tzinfo=timezone.utc)),
        'B': IndexQuote('uid-B', Decimal('20'), 1, 'rub',
                        datetime(2026, 9, 28, 10, tzinfo=timezone.utc)),
    }
    calls = list(client.mock_calls)
    target = strategy.build_target(snapshot, Decimal('200'))
    assert target.quantities == {'uid-A': Decimal('8'), 'uid-B': Decimal('5')}
    assert target.prices == {'uid-A': Decimal('10'), 'uid-B': Decimal('20')}
    assert client.mock_calls == calls
    assert client.instruments.mock_calls == [
        call.find_instrument(query='A'),
        call.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid-A'),
        call.find_instrument(query='B'),
        call.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid-B'),
    ]
    assert client.market_data.mock_calls == [call.get_last_prices(instrument_id=['uid-A', 'uid-B'])]
    assert client.operations.mock_calls == []
    assert client.operations_stream.mock_calls == []
    client.orders.post_order.assert_not_called()


def test_index_snapshot_refreshes_all_data(client, index_sdk_config):
    """A later snapshot reloads every ticker, UID, lot, currency, price and timestamp."""
    strategy = IndexStrategy(index_sdk_config)
    first = strategy.load_snapshot(TInvestStrategyData(client))
    client.instruments.find_instrument.side_effect = [
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='A', instrument_type='share', class_code='TQBR', uid='new-A')]),
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='B', instrument_type='share', class_code='TQBR', uid='uid-B')]),
    ]
    client.instruments.get_instrument_by.side_effect = [
        InstrumentResponse(instrument=Instrument(
            uid='new-A', ticker='A', instrument_type='share',
            class_code='TQBR', lot=4, currency='usd', api_trade_available_flag=True)),
        InstrumentResponse(instrument=Instrument(
            uid='uid-B', ticker='B', instrument_type='share',
            class_code='TQBR', lot=1, currency='rub', api_trade_available_flag=True)),
    ]
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(last_prices=[
        LastPrice(instrument_uid='new-A', price=Quotation(units=20, nano=1),
                  time=datetime(2026, 9, 29, 11, tzinfo=timezone.utc)),
        LastPrice(instrument_uid='uid-B', price=Quotation(units=20, nano=0),
                  time=datetime(2026, 9, 29, 11, tzinfo=timezone.utc)),
    ])
    second = strategy.load_snapshot(TInvestStrategyData(client))
    assert first['A'].price == Decimal('10')
    assert first['A'].uid == 'uid-A'
    assert second['A'] == IndexQuote('new-A', Decimal('20.000000001'), 4, 'usd',
                                     datetime(2026, 9, 29, 11, tzinfo=timezone.utc))
    assert second['B'].time == datetime(2026, 9, 29, 11, tzinfo=timezone.utc)
    target = strategy.build_target(second, Decimal('250'))
    assert target.quantities == {'new-A': Decimal('8'), 'uid-B': Decimal('4')}
    assert client.instruments.find_instrument.call_args_list == [
        call(query='A'),
        call(query='B'),
    ] * 2
    assert client.instruments.get_instrument_by.call_args_list == [
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
        for uid in ['uid-A', 'uid-B', 'new-A', 'uid-B']]
    assert client.market_data.get_last_prices.call_args_list == [
        call(instrument_id=['uid-A', 'uid-B']), call(instrument_id=['new-A', 'uid-B'])]
    # A failed fresh read must not silently reuse the last successful price.
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=[])
    with pytest.raises(ValueError, match='index instrument: A'):
        strategy.load_snapshot(TInvestStrategyData(client))
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('matches', [
    [],
    [InstrumentShort(ticker='a', instrument_type='share', class_code='TQBR', uid='wrong-case')],
    [InstrumentShort(ticker='A-extra', instrument_type='share', class_code='TQBR', uid='partial')],
    [InstrumentShort(ticker=' A', instrument_type='share', class_code='TQBR', uid='space')],
    [InstrumentShort(ticker='A', instrument_type='bond', class_code='TQBR', uid='bond')],
])
def test_index_snapshot_requires_exact_supported_instrument(client, index_sdk_config, matches):
    """Other tickers and unsupported instrument types cannot replace a missing share."""
    client.instruments.find_instrument.side_effect = None
    client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=matches)
    with pytest.raises(ValueError, match=r'index instrument: A available via API'):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.instruments.find_instrument.assert_called_once_with(query='A')
    client.instruments.get_instrument_by.assert_not_called()
    client.market_data.get_last_prices.assert_not_called()
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('uid', ['', None, 123, 'uid-A'])
def test_index_snapshot_invalid_or_duplicate_uid(client, index_sdk_config, uid):
    """Two different tickers must never collapse into one target UID."""
    client.instruments.find_instrument.side_effect = [
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='A', instrument_type='share', class_code='TQBR', uid='uid-A')]),
        FindInstrumentResponse(instruments=[
            InstrumentShort(ticker='B', instrument_type='share', class_code='TQBR', uid=uid)]),
    ]
    with pytest.raises(ValueError, match=r'invalid or duplicate index UID: B \(TQBR\)'):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.instruments.get_instrument_by.assert_called_once_with(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid-A')
    client.market_data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('uid', ''), ('uid', 'other'), ('ticker', 'B'), ('instrument_type', 'bond'),
    ('class_code', 'PTEQ'), ('class_code', 'SPEQ'), ('class_code', ''),
    ('class_code', None), ('class_code', 'tqbr'),
    ('lot', 0), ('lot', -1), ('lot', None), ('lot', True), ('lot', Decimal('2')),
])
def test_index_snapshot_invalid_metadata(client, index_sdk_config, field, value):
    """Metadata must describe the selected share and provide a positive integer lot."""
    instrument = Instrument(uid='uid-A', ticker='A', instrument_type='share',
                            class_code='TQBR', lot=2, currency='rub', api_trade_available_flag=True)
    setattr(instrument, field, value)
    client.instruments.get_instrument_by.side_effect = None
    client.instruments.get_instrument_by.return_value = InstrumentResponse(instrument=instrument)
    with pytest.raises(ValueError, match=r'index (instrument metadata|lot): A \(.*TQBR\)'):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.instruments.get_instrument_by.assert_called_once_with(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid-A')
    client.market_data.get_last_prices.assert_not_called()
    client.orders.post_order.assert_not_called()


def test_index_snapshot_missing_metadata(client, index_sdk_config):
    """An absent instrument is a data error, never an empty snapshot."""
    client.instruments.get_instrument_by.side_effect = None
    client.instruments.get_instrument_by.return_value = InstrumentResponse(instrument=None)
    with pytest.raises(ValueError, match=r'instrument uid-A: instrument is missing'):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.market_data.get_last_prices.assert_not_called()


@pytest.mark.parametrize('uids,error', [
    ([], 'missing index price: A'),
    (['uid-A'], 'missing index price: B'),
    (['uid-A', 'uid-A'], 'duplicate index quote UID: uid-A'),
    (['uid-A', 'unknown'], 'index quote UID: unknown'),
])
def test_index_snapshot_incomplete_or_ambiguous_prices(client, index_sdk_config, uids, error):
    """Incomplete or conflicting batch responses must abort the entire snapshot."""
    client.market_data.get_last_prices.return_value = GetLastPricesResponse(last_prices=[
        LastPrice(instrument_uid=uid, price=Quotation(units=10, nano=0)) for uid in uids])
    with pytest.raises(ValueError, match=error):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.market_data.get_last_prices.assert_called_once_with(instrument_id=['uid-A', 'uid-B'])
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('field,value', [
    ('price', None), ('units', 0), ('units', -1),
    ('units', Decimal('NaN')), ('units', Decimal('Infinity')),
    ('units', Decimal('sNaN')), ('units', None), ('nano', 'invalid'),
])
def test_index_snapshot_invalid_price(client, index_sdk_config, field, value):
    """Missing, nonpositive and nonfinite quotes cannot reach the calculator."""
    quote = client.market_data.get_last_prices.return_value.last_prices[0]
    if field == 'price':
        quote.price = value
    else:
        setattr(quote.price, field, value)
    if field == 'price':
        message = 'instrument uid-B: price is missing'
    elif not isinstance(value, Decimal) and value in (0, -1):
        message = 'index price: B'
    elif isinstance(value, Decimal):
        message = 'instrument uid-B price.units is not finite'
    else:
        message = f'instrument uid-B price.{field} is invalid'
    with pytest.raises(ValueError, match=message):
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('service,method', [
    ('instruments', 'find_instrument'), ('instruments', 'get_instrument_by'),
    ('market_data', 'get_last_prices'),
])
def test_index_snapshot_request_error_propagates(client, index_sdk_config, service, method):
    """The engine receives an own transport error with the SDK failure as its cause."""
    error = RequestError(code=StatusCode.UNAVAILABLE, details='index unavailable', metadata=())
    getattr(getattr(client, service), method).side_effect = error
    with pytest.raises(DataAccessError) as caught:
        IndexStrategy(index_sdk_config).load_snapshot(TInvestStrategyData(client))
    assert caught.value.__cause__ is error
    client.orders.post_order.assert_not_called()


@pytest.fixture(name='public_index_snapshot')
def fixture_public_index_snapshot(tmp_path):
    """Replay the public sandbox capture with its own frozen base, entirely offline."""
    report = json.loads((Path(__file__).parent / 'data' / 'imoex_snapshot.json').read_text(
        encoding='utf-8'), parse_float=Decimal)
    policy = index_config_module.read_index_document(
        Path(__file__).resolve().parents[1] / 'autorepeater/configs/imoex.json')
    config = load_index_config(write_index_config(
        tmp_path, {**report['config'], 'reserve': '0.01',
                   'allocation_drift_limits': policy['allocation_drift_limits']}))
    snapshot = {ticker: IndexQuote(
        uid=row['uid'], price=Decimal(row['price']), lot=row['lot'], currency=row['currency'],
        time=datetime.fromisoformat(row['time'])) for ticker, row in report['snapshot'].items()}
    return report, config, snapshot


@pytest.fixture(name='public_prefix_expectations')
def fixture_public_prefix_expectations():
    """New policy expectations are separate from the immutable historical capture."""
    data = Path(__file__).parent / 'data'
    expectations = json.loads((data / 'imoex_prefix_expectations.json').read_text(encoding='utf-8'))
    assert expectations['capture_sha256'] == hashlib.sha256(
        (data / expectations['capture']).read_bytes()).hexdigest()
    assert expectations['policy'] == 'full_index_minimum_then_capitalization_prefix_budget_first_v2'
    assert expectations['lot_limit'] == '1'
    assert expectations['min_position_value'] == '3000'
    return expectations


def test_index_public_snapshot_provenance(public_index_snapshot, public_prefix_expectations):
    """Keep all 44 quotes and the full comparison matrix tied to the sandbox capture."""
    report, config, snapshot = public_index_snapshot
    assert report['source']['environment'] == 'sandbox'
    assert report['source']['endpoint'] == INVEST_GRPC_API_SANDBOX
    assert report['source']['class_code'] == 'TQBR'
    assert report['snapshot_started_at'] == '2026-09-29T13:45:31.223369+00:00'
    assert report['snapshot_received_at'] == '2026-09-29T13:45:44.092784+00:00'
    assert min(quote.time for quote in snapshot.values()) == datetime.fromisoformat(
        report['source']['quote_time_min']) == datetime.fromisoformat(
            '2026-09-29T13:45:07.838243+00:00')
    assert max(quote.time for quote in snapshot.values()) == datetime.fromisoformat(
        report['source']['quote_time_max']) == datetime.fromisoformat(
            '2026-09-29T13:45:44.064600+00:00')
    assert len(snapshot) == len(config.instruments) == 44
    assert set(snapshot) == {item.ticker for item in config.instruments}
    assert len({quote.uid for quote in snapshot.values()}) == 44
    assert config.max_lot_weight_error == Decimal('0.05')
    assert config.min_position_value == Decimal(0)
    assert 'min_position_value' not in report['config']
    assert all(isinstance(row['price'], str) for row in report['snapshot'].values())
    assert all(quote.currency == 'rub' and quote.price > 0 and quote.lot > 0
               for quote in snapshot.values())
    assert len(report['comparisons']) == 40
    assert len(public_prefix_expectations['comparisons']) == 40
    assert {(row['gross_value'], row['reserve'], row['budget'], row['max_lot_weight_error'])
            for row in public_prefix_expectations['comparisons']} == {
                (row['gross_value'], row['reserve'], row['budget'], row['max_lot_weight_error'])
                for row in report['comparisons']}
    assert {(row['gross_value'], row['reserve'], row['budget'], row['max_lot_weight_error'])
            for row in report['comparisons']} == {
                (*scenario, threshold)
                for scenario in [('100000', '0.01', '99000.00'), ('154000', '0.01', '152460.00'),
                                 ('300000', '0.01', '297000.00'), (None, '0', '154000')]
                for threshold in ['0.01', '0.02', '0.03', '0.04', '0.05',
                                  '0.06', '0.07', '0.08', '0.1', '0.15']}


@pytest.mark.parametrize('scenario', [
    ('100000', '99000.00'), ('154000', '152460.00'),
    ('300000', '297000.00'), (None, '154000'),
])
@pytest.mark.parametrize('threshold', [
    '0.01', '0.02', '0.03', '0.04', '0.05', '0.06', '0.07', '0.08', '0.1', '0.15',
])
def test_index_public_snapshot_replay(public_index_snapshot, public_prefix_expectations,
                                      scenario, threshold):
    """Independent minimum-prefix expectations replay all 40 captured-price scenarios."""
    config, snapshot = public_index_snapshot[1:]
    budget = Decimal(scenario[1])
    expected = next(row for row in public_prefix_expectations['comparisons']
                    if row['gross_value'] == scenario[0]
                    and row['max_lot_weight_error'] == threshold)
    assert budget == Decimal(expected['budget'])
    if scenario[0] is not None:
        assert budget == Decimal(scenario[0]) * (1 - Decimal(expected['reserve']))
    config = replace(config, max_lot_weight_error=Decimal(threshold),
                     min_position_value=Decimal(expected['min_position_value']),
                     reserve=Decimal(expected['reserve']))
    result = calculate_index_target(config, snapshot, budget)
    quantities = {snapshot[ticker].uid: Decimal(quantity)
                  for ticker, quantity in expected['quantities'].items()}
    assert result.target.quantities == quantities
    assert result.target.prices == {snapshot[ticker].uid: snapshot[ticker].price
                                    for ticker in expected['quantities']}
    assert IndexStrategy(config).build_target(
        snapshot, Decimal(scenario[0]) if scenario[0] is not None else budget) == result.target
    validate_target(result.target)
    assert len(quantities) == expected['selected_count']
    cost = sum((quantity * result.target.prices[uid]
                for uid, quantity in quantities.items()), Decimal(0))
    assert cost == Decimal(expected['cost'])
    assert budget - cost == Decimal(expected['cash'])
    assert 0 <= cost <= budget
    assert list(result.passes[0]) == expected['initial_prefix']
    assert expected['selected_ranks'] == list(range(1, len(quantities) + 1))
    comparison = calibration.compare_target(config, snapshot, {
        'gross_value': Decimal(scenario[0]) if scenario[0] is not None else None,
        'reserve': Decimal(expected['reserve']), 'budget': budget,
    }, config.max_lot_weight_error)
    assert comparison['initial_prefix'] == expected['initial_prefix']
    assert comparison['selected_ranks'] == expected['selected_ranks']
    assert comparison['selected_count'] == expected['selected_count']
    assert comparison['cost'] == cost
    for allocations in result.passes:
        assert sum((row.lots * snapshot[ticker].price * snapshot[ticker].lot
                    for ticker, row in allocations.items()), Decimal(0)) <= budget
    for ticker, allocation in result.passes[-1].items():
        assert allocation.exclusion_reason == ''
        assert allocation.lots > 0
        assert quantities[snapshot[ticker].uid] == Decimal(allocation.lots) * snapshot[ticker].lot
        assert quantities[snapshot[ticker].uid] % snapshot[ticker].lot == 0
        assert allocation.error == (
            abs(allocation.actual_weight - allocation.weight) / allocation.weight)
        assert allocation.ideal_lots >= 1 or allocation.error <= config.max_lot_weight_error


def test_index_public_snapshot_main_exclusions(public_index_snapshot):
    """Minimum 3000 leaves 15 candidates; ideal >= 1 keeps OZON despite error above 5%."""
    _, config, snapshot = public_index_snapshot
    config = replace(config, min_position_value=Decimal('3000'))
    result = calculate_index_target(config, snapshot, Decimal('152460.00'))
    prefix = ['LKOH', 'SBER', 'GAZP', 'YDEX', 'TATN', 'T', 'NVTK', 'GMKN',
              'OZON', 'ROSN', 'X5', 'VTBR', 'SBERP', 'PLZL', 'SNGSP']
    assert [list(rows) for rows in result.passes] == [prefix]
    assert list(result.target.quantities) == [snapshot[ticker].uid for ticker in prefix]
    assert len(result.min_exclusions) == 29
    ozon = result.passes[0]['OZON']
    assert ozon.ideal_lots == Decimal('1.860375443290773144563823019')
    assert ozon.error == Decimal('0.07505181667107385199854053392')
    assert ozon.lots == 2
    assert all(row.exclusion_reason == '' for row in result.passes[0].values())
    for ticker in ['SBER', 'TATN']:
        row = result.passes[0][ticker]
        assert row.lots == round(row.ideal_lots) - 1
    packaged = load_index_config(
        Path(__file__).resolve().parents[1] / 'autorepeater/configs/imoex.json')
    assert packaged == config
    assert IndexStrategy(packaged).build_target(snapshot, Decimal('154000')) == result.target


@pytest.fixture(name='calibration_cli')
def fixture_calibration_cli(client, index_sdk_config, monkeypatch, tmp_path):
    """Use real snapshot loading with SDK autospecs and only synthetic credentials."""
    client.instruments.shares.return_value = SharesResponse(instruments=[
        Share(uid=f'uid-{ticker}', ticker=ticker, name=ticker, class_code='TQBR',
              lot=lot, currency='rub', api_trade_available_flag=True)
        for ticker, lot in [('A', 2), ('B', 1)]])
    path = write_index_config(
        tmp_path, json.loads(json.dumps(asdict(index_sdk_config), default=str)))
    monkeypatch.setattr(calibration.os, 'environ', {
        'READ_ONLY_INVEST_TOKEN': 'synthetic-read-only-token', 'IMOEX_CONFIG_PATH': str(path),
    })
    with patch('t_tech.invest.Client', autospec=True) as constructor, \
            patch('autorepeater.runner.Runner', autospec=True) as runner, \
            patch('autorepeater.repeater.AutoRepeater', autospec=True) as engine:
        constructor.return_value.__enter__.return_value = client
        monkeypatch.setattr(calibration, 'Client', constructor)
        yield constructor
        runner.assert_not_called()
        engine.assert_not_called()
        for service in (client.orders, client.operations, client.users, client.operations_stream):
            assert service.mock_calls == []


@pytest.mark.parametrize('defaults', [True, False])
def test_index_calibration_cli(defaults, calibration_cli, client, tmp_path, monkeypatch, caplog):
    """One snapshot serves every comparison; the saved public data reproduces targets."""
    output = tmp_path / 'snapshot.json'
    arguments = ['--output', str(output)] if defaults else [
        '--gross-values', '200', '--budgets', '100',
        '--thresholds', '0.05', '0.2',
    ]
    with caplog.at_level(25, logger='tinkoffBot'):
        if defaults:
            monkeypatch.delitem(sys.modules, 'scripts.check_imoex_strategy')
            monkeypatch.setattr(sys, 'argv', ['scripts.check_imoex_strategy', *arguments])
            runpy.run_module('scripts.check_imoex_strategy', run_name='__main__')
        else:
            calibration.main(arguments)
    calibration_cli.assert_called_once_with('synthetic-read-only-token', target=INVEST_GRPC_API,
                                            interceptors=[ANY])
    calibration_cli.return_value.__enter__.assert_called_once_with()
    calibration_cli.return_value.__exit__.assert_called_once_with(None, None, None)
    assert client.instruments.mock_calls == [
        call.find_instrument(query='A'),
        call.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_ALL),
        call.find_instrument(query='B'),
    ]
    assert client.market_data.mock_calls == [call.get_last_prices(instrument_id=['uid-A', 'uid-B'])]
    messages = [record.message for record in caplog.records if record.levelno == 25]
    assert len(messages) == 1
    report = json.loads(messages[0])
    assert 'synthetic-read-only-token' not in messages[0]
    assert set(report) == {
        'snapshot_started_at', 'snapshot_received_at', 'config', 'snapshot', 'comparisons'}
    assert (datetime.fromisoformat(report['snapshot_started_at'])
            <= datetime.fromisoformat(report['snapshot_received_at']))
    assert report['snapshot'] == {
        'A': {'uid': 'uid-A', 'price': '10', 'lot': 2, 'currency': 'rub',
              'time': '2026-09-29T10:00:00+00:00'},
        'B': {'uid': 'uid-B', 'price': '20', 'lot': 1, 'currency': 'rub',
              'time': '2026-09-28T10:00:00+00:00'},
    }
    budgets = ['99000.00', '152460.00', '297000.00', '154000'] if defaults else ['198.00', '100']
    assert [(item['budget'], item['max_lot_weight_error']) for item in report['comparisons']] == [
        (budget, threshold) for budget in budgets
        for threshold in (['0.03', '0.05', '0.1'] if defaults else ['0.05', '0.2'])]
    if defaults:
        assert output.read_text(encoding='utf-8') == messages[0] + '\n'
        assert report['comparisons'][3]['gross_value'] == '154000'
        assert report['comparisons'][3]['reserve'] == '0.01'
        assert report['comparisons'][-1]['gross_value'] is None
        assert report['comparisons'][-1]['reserve'] == '0'
    else:
        assert not output.exists()
    config = load_index_config(write_index_config(tmp_path, report['config']))
    snapshot = {ticker: IndexQuote(
        uid=row['uid'], price=Decimal(row['price']), lot=row['lot'], currency=row['currency'],
        time=datetime.fromisoformat(row['time'])) for ticker, row in report['snapshot'].items()}
    for comparison in report['comparisons']:
        target = build_index_target(
            replace(config, max_lot_weight_error=Decimal(comparison['max_lot_weight_error'])),
            snapshot, Decimal(comparison['budget']))
        assert {snapshot[row['ticker']].uid: Decimal(row['quantity'])
                for row in comparison['rows'] if row['final_lots']} == target.quantities
        assert (Decimal(comparison['cost']) + Decimal(comparison['cash'])
                == Decimal(comparison['budget']))


def test_index_calibration_uses_injected_clock(
        calibration_cli: Mock, client: Mock, caplog: pytest.LogCaptureFixture) -> None:
    """Timestamp boundaries are deterministic and enclose snapshot reads."""
    started = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    received = datetime(2026, 10, 5, 12, 0, 1, tzinfo=timezone.utc)

    def sample_time() -> datetime:
        if not client.market_data.mock_calls:
            calibration_cli.return_value.__enter__.assert_not_called()
            return started
        calibration_cli.return_value.__exit__.assert_called_once_with(None, None, None)
        return received

    clock = Mock(side_effect=sample_time)
    with caplog.at_level(25, logger='tinkoffBot'):
        calibration.main([], clock=clock)
    clock.assert_has_calls([call(), call()])
    assert clock.call_count == 2
    report = json.loads(caplog.messages[-1])
    assert report['snapshot_started_at'] == started.isoformat()
    assert report['snapshot_received_at'] == received.isoformat()


def test_index_calibration_sandbox(calibration_cli, client):
    """Sandbox selects the SDK endpoint and still only reads one public snapshot."""
    calibration.main(['--sandbox'])
    calibration_cli.assert_called_once_with(
        'synthetic-read-only-token', target=INVEST_GRPC_API_SANDBOX, interceptors=[ANY])
    calibration_cli.return_value.__enter__.assert_called_once_with()
    calibration_cli.return_value.__exit__.assert_called_once_with(None, None, None)
    assert client.market_data.mock_calls == [call.get_last_prices(instrument_id=['uid-A', 'uid-B'])]


@pytest.mark.parametrize('name', [None, 'BETA', 'unknown', 'beta', '4', '', ' BETA'])
def test_index_calibration_selects_config_name(
        name, calibration_cli, tmp_path, monkeypatch, caplog, capsys):
    """With several configs calibration requires an exact index name, never an account."""
    data = json.loads((tmp_path / 'index.json').read_text(encoding='utf-8'))
    write_index_config(tmp_path, dict(data, name='ALPHA'))
    write_index_config(tmp_path, dict(data, name='BETA', min_position_value='123'), 'second.json')
    monkeypatch.delenv('IMOEX_CONFIG_PATH')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))
    arguments = [] if name is None else ['--src', name]
    if name != 'BETA':
        with pytest.raises(SystemExit) as error:
            calibration.main(arguments)
        assert error.value.code == 2
        assert '--src must name a configured index strategy' in capsys.readouterr().err
        calibration_cli.assert_not_called()
        return
    with caplog.at_level(25, logger='tinkoffBot'):
        calibration.main(arguments)
    report = json.loads(next(record.message for record in caplog.records if record.levelno == 25))
    assert report['config']['name'] == 'BETA'
    assert report['config']['min_position_value'] == '123'
    calibration_cli.assert_called_once_with('synthetic-read-only-token', target=INVEST_GRPC_API,
                                            interceptors=[ANY])


def test_index_calibration_single_renamed_config(calibration_cli, tmp_path, caplog):
    """A sole config is selected by its JSON name without an IMOEX fallback."""
    data = json.loads((tmp_path / 'index.json').read_text(encoding='utf-8'))
    write_index_config(tmp_path, dict(data, name='RENAMED'))
    with caplog.at_level(25, logger='tinkoffBot'):
        calibration.main([])
    report = json.loads(next(record.message for record in caplog.records if record.levelno == 25))
    assert report['config']['name'] == 'RENAMED'
    calibration_cli.assert_called_once_with('synthetic-read-only-token', target=INVEST_GRPC_API,
                                            interceptors=[ANY])


@pytest.mark.parametrize('single_fund', ['GOLD', 'OBLG', 'TMON'], indirect=True)
@pytest.mark.parametrize('expected_reserve, budget, lots', [
    ('0.0005', '299.8500', 9), ('0.5', '150.0', 5), ('0', '300', 10),
])
def test_single_fund_calibration(single_fund, client, monkeypatch, caplog, tmp_path,
                                 expected_reserve, budget, lots):
    """Calibration uses config reserve on gross inputs and leaves available budgets alone."""
    config = replace(single_fund.config, reserve=Decimal(expected_reserve))
    path = write_index_config(tmp_path, json.loads(json.dumps(asdict(config), default=str)))
    monkeypatch.setenv('IMOEX_CONFIG_PATH', str(path))
    monkeypatch.setenv('READ_ONLY_INVEST_TOKEN', 'synthetic-read-only-token')
    with patch.object(calibration, 'Client', autospec=True) as sdk_client, \
            caplog.at_level(25, logger='tinkoffBot'):
        sdk_client.return_value.__enter__.return_value = client
        calibration.main(['--src', single_fund.config.name, '--gross-values', '300',
                          '--budgets', '45'])
    report = json.loads(next(record.message for record in caplog.records if record.levelno == 25))
    for row in report['comparisons']:
        assert row['reserve'] == (expected_reserve if row['gross_value'] is not None else '0')
        assert row['budget'] == (budget if row['gross_value'] is not None else '45')
        assert row['rows'][0]['final_lots'] == (lots if row['gross_value'] is not None else 1)
    for service in [client.orders, client.operations, client.users, client.operations_stream]:
        assert service.mock_calls == []


def test_index_calibration_exclusion_report():
    """Excluded trial lots are distinct from final holdings; ranks refer to the full index."""
    config, snapshot = index_calculation_case([
        ('C', '10', '100', 1), ('B', '30', '30', 1), ('A', '60', '6', 1)])
    scenario = {'gross_value': None, 'reserve': Decimal(0), 'budget': Decimal('100')}
    report = calibration.compare_target(config, snapshot, scenario, Decimal('0.05'))
    assert report['selected_count'] == 2
    assert report['selected_ranks'] == [1, 2]
    assert (report['cost'], report['cash']) == (Decimal('96'), Decimal('4'))
    first, second, third = report['rows']
    assert [(row['ticker'], row['rank'], row['allocation_pass'], row['final_lots'])
            for row in report['rows']] == [('A', 1, 2, 11), ('B', 2, 2, 1), ('C', 3, 1, 0)]
    assert first['full_weight'] == Decimal('0.6')
    assert first['actual_weight'] == Decimal('0.66')
    assert first['full_weight_error'] == Decimal('0.1')
    assert first['last_allocation'] == {
        'weight': Decimal(60) / 90, 'ideal_lots': Decimal(100) * (Decimal(60) / 90) / 6,
        'lots': 11, 'actual_weight': Decimal('0.66'),
        'error': abs(Decimal('0.66') - Decimal(60) / 90) / (Decimal(60) / 90),
        'exclusion_reason': '',
    }
    assert second['last_allocation']['lots'] > 0
    assert second['last_allocation']['exclusion_reason'] == ''
    assert second['last_allocation']['ideal_lots'] > 1
    assert second['last_allocation']['error'] > config.max_lot_weight_error
    assert third['last_allocation']['exclusion_reason'] == 'zero_lots'
    assert second['actual_weight'] == Decimal('0.3')
    assert third['actual_weight'] == Decimal(0)
    assert second['full_weight_error'] == Decimal(0)
    assert third['full_weight_error'] == Decimal(1)
    empty = calibration.compare_target(config, snapshot, scenario, Decimal(0))
    assert (empty['selected_count'], empty['selected_ranks'], empty['cost'], empty['cash']) == (
        2, [1, 2], Decimal('96'), Decimal('4'))
    assert config.max_lot_weight_error == Decimal('0.05')


@pytest.mark.parametrize('minimum, count', [('21', 2), ('51', 0)])
def test_index_calibration_minimum_report(minimum, count):
    """No allocation is fabricated for a minimum exclusion, even for an empty prefix."""
    config, snapshot = index_calculation_case([
        ('C', '20', '1', 1), ('B', '30', '1', 1), ('A', '50', '1', 1)])
    config.min_position_value = Decimal(minimum)
    report = calibration.compare_target(
        config, snapshot, {'budget': Decimal('100')}, Decimal('0.05'))
    assert report['min_position_value'] == Decimal(minimum)
    assert report['selected_count'] == count
    assert report['initial_prefix'] == ['A', 'B', 'C'][:count]
    assert [row['full_target_value'] for row in report['rows']] == [
        Decimal('50'), Decimal('30'), Decimal('20')]
    assert report['cost'] + report['cash'] == Decimal('100')
    for row in report['rows'][count:]:
        assert row['exclusion_reason'] == 'min_position_value'
        assert row['last_allocation'] is None
        assert row['allocation_pass'] is None
        assert row['quantity'] == row['final_lots'] == 0
        assert row['full_weight_error'] == Decimal(1)
    if count:
        first = report['rows'][0]
        assert first['full_weight'] == Decimal('0.5')
        assert first['last_allocation']['weight'] == Decimal('0.625')


def test_index_calibration_cli_minimum(calibration_cli, tmp_path, caplog):
    """CLI reports the JSON minimum and preserves it in every comparison."""
    config, _ = index_calculation_case([('A', '1', '10', 2), ('B', '1', '20', 1)])
    config.min_position_value = Decimal('51')
    config.reserve = Decimal('0')
    write_index_config(tmp_path, json.loads(json.dumps(asdict(config), default=str)))
    with caplog.at_level(25, logger='tinkoffBot'):
        calibration.main(['--gross-values', '100', '--budgets', '100'])
    report = json.loads(next(record.message for record in caplog.records if record.levelno == 25))
    assert report['config']['min_position_value'] == '51'
    assert all(row['min_position_value'] == '51' and row['selected_count'] == 0
               and row['cost'] == '0' and row['cash'] == '100'
               for row in report['comparisons'])
    calibration_cli.assert_called_once_with('synthetic-read-only-token', target=INVEST_GRPC_API,
                                            interceptors=[ANY])


@pytest.mark.parametrize('arguments, message', [
    (['--budgets', 'no'], 'expected a decimal number'),
    (['--budgets', 'NaN'], 'expected a finite decimal number'),
    (['--thresholds', 'Infinity'], 'expected a finite decimal number'),
    (['--budgets', '0'], 'must be positive'),
    (['--gross-values', '-1'], 'must be positive'),
    (['--reserve', '1'], 'unrecognized arguments: --reserve'),
    (['--thresholds', '-0.1'], 'must be in [0, 1)'),
    ([], 'READ_ONLY_INVEST_TOKEN is required'),
    (['--sandbox'], 'READ_ONLY_INVEST_TOKEN is required'),
])
def test_index_calibration_invalid_input(arguments, message, calibration_cli, monkeypatch, capsys):
    """Missing credentials and invalid inputs fail before any SDK access."""
    monkeypatch.setattr(calibration.os, 'environ', {'INVEST_TOKEN': 'must-not-be-used'})
    with pytest.raises(SystemExit) as error:
        calibration.main(arguments)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    calibration_cli.assert_not_called()


def test_index_calibration_sdk_failure(calibration_cli, client, tmp_path, caplog):
    """A failed snapshot closes the client and never saves a partial calibration."""
    failure = RequestError(StatusCode.UNAVAILABLE, 'market data unavailable', None)
    client.market_data.get_last_prices.side_effect = failure
    output = tmp_path / 'snapshot.json'
    with pytest.raises(DataAccessError) as error:
        calibration.main(['--output', str(output)])
    assert error.value.__cause__ is failure
    assert not output.exists()
    assert not [record for record in caplog.records if record.levelno == 25]
    assert calibration_cli.return_value.__exit__.call_count == 1
    assert calibration_cli.return_value.__exit__.call_args.args[:2] == (
        DataAccessError, error.value)


def test_index_calibration_import_is_inert(calibration_cli):
    """Importing the module must not even look for a token or create a strategy."""
    with patch.object(calibration.os, 'environ', Mock(spec_set=dict)) as environment:
        runpy.run_path(calibration.__file__)
        environment.get.assert_not_called()
    calibration_cli.assert_not_called()


def test_index_calibration_rejects_nonpublic_objects():
    """Serialization accepts explicit report data, not arbitrary SDK objects."""
    with pytest.raises(TypeError, match='unsupported calibration value: object'):
        reporting.format_index_calibration({'unexpected': object()})
