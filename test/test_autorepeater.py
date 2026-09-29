# pylint: disable=R0913, R0917, too-many-lines
"""tests"""
import inspect
import json
import logging
import runpy
import sys
from decimal import Decimal, DivisionByZero
from unittest.mock import Mock, call, create_autospec, patch

import pytest
from grpc import StatusCode

from t_tech.invest import MoneyValue
from t_tech.invest import Instrument
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
from t_tech.invest import SecurityTradingStatus
from t_tech.invest import PostOrderResponse
from t_tech.invest import RequestError
from t_tech.invest.services import InstrumentsService
from t_tech.invest.services import OperationsService
from t_tech.invest.services import UsersService
from t_tech.invest.services import OrdersService
from t_tech.invest.services import OperationsStreamService

from autorepeater.constants import DST_MONEY_RESERVED
from autorepeater.constants import THRESHOLD
from autorepeater.account_strategy import AccountStrategy
from autorepeater.money import blocked_to_string
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_decimal_price
from autorepeater.money import currency_to_string
from autorepeater.money import get_quantity_position
from autorepeater.money import money_to_string
from autorepeater.money import no_money_to_string
from autorepeater.orders import OrderParams
from autorepeater.orders import get_max_sum_positions_price
from autorepeater.portfolio import get_portfolio
from autorepeater.portfolio import TargetPortfolio
from autorepeater.portfolio import validate_target
from autorepeater.repeater import AutoRepeater
from autorepeater.reporting import GetInstrumentException
from autorepeater.triggers import check_triggers
from autorepeater import logging_config
from autorepeater import reporting
from autorepeater import runner as runner_module
from autorepeater import serverless
from autorepeater import strategies
from autorepeater.runner import RunnerParams
import handler as cloud_entrypoint
import main as cli


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
        ('1', [], [], False),  # Пустые позиции
        # Заблокированные деньги
        ('2', [], [PositionsMoney(blocked_value=MoneyValue(units=1))], False),
        # Разблокированные ценные бумаги
        ('1', [PositionsSecurities(blocked=0)], [PositionsMoney()], True),
        # Разблокированные деньги
        ('2', [], [PositionsMoney(blocked_value=MoneyValue(units=0, nano=0))], True),
        ('3', [], [], False),  # Неизвестный аккаунт
        # Заблокированные ценные бумаги
        ('1', [PositionsSecurities(blocked=1)], [PositionsMoney()], False),
        # Частично заблокированные деньги
        ('2', [], [PositionsMoney(blocked_value=MoneyValue(units=0, nano=1))], False),
        ('1', [PositionsSecurities(blocked=0), PositionsSecurities(blocked=1)], [
         PositionsMoney()], False),  # Смешанные блокировки
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
    position = PositionData(
        account_id=account_id,
        money=position_money,
        securities=position_securities,
    )
    result = check_triggers(position, src_account, dst_account)

    assert result == expected


@pytest.mark.parametrize(
    'sell_orders_params, buy_orders_params, dst_positions, src_positions, expected',
    [
        ([], [], {}, {}, 0),
        (
            [
                OrderParams
                (
                    instrument_id='1',
                    quantity=1,
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE
                )
            ],
            [],
            {
                '1': PortfolioPosition
                (
                    current_price=MoneyValue('RUB', 1, 500000000)
                )
            },
            {},
            1.5
        ),
        (
            [],
            [
                OrderParams
                (
                    instrument_id='1',
                    quantity=1,
                    direction=OrderDirection.ORDER_DIRECTION_BUY,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE
                )
            ],
            {},
            {
                '1': PortfolioPosition
                (
                    current_price=MoneyValue('RUB', 1, 500000000)
                )
            },
            1.5
        ),
        (
            [
                OrderParams
                (
                    instrument_id='1',
                    quantity=1,
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE
                )
            ],
            [
                OrderParams
                (
                    instrument_id='1',
                    quantity=1,
                    direction=OrderDirection.ORDER_DIRECTION_BUY,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE
                )
            ],
            {
                '1': PortfolioPosition(current_price=MoneyValue('RUB', 1, 500000000))
            },
            {
                '1': PortfolioPosition(current_price=MoneyValue('RUB', 1, 500000000))
            },
            1.5
        ),
    ],
    ids=[
        'empty_orders',
        'sell_only',
        'buy_only',
        'both_orders'
    ]
)
def test_get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                     src_positions, dst_positions, expected):
    """get_max_sum_positions_price"""
    result = get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                         src_positions, dst_positions)
    assert result == expected


@pytest.fixture(name='client')
def client_tinvest():
    """SDK services enforce signatures; response data stays explicit."""
    services = {
        'instruments': InstrumentsService,
        'operations': OperationsService,
        'users': UsersService,
        'orders': OrdersService,
        'operations_stream': OperationsStreamService,
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
    return AutoRepeater(client)


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


def test_init(auto_repeater):
    """test_init"""
    # Проверка базовой инициализации
    assert auto_repeater.client is not None
    assert auto_repeater.debug is False
    assert auto_repeater.threshold == Decimal(THRESHOLD)
    assert auto_repeater.reserve == Decimal(DST_MONEY_RESERVED)

    # Проверка инициализации с невалидными значениями
    with pytest.raises(ValueError):
        auto_repeater.set_threshold(-1)
    with pytest.raises(ValueError):
        auto_repeater.set_reserve(-1)
    with pytest.raises(ValueError):
        auto_repeater.set_reserve(1.1)  # Резерв не может быть больше 100%


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


def test_set_threshold(auto_repeater):
    """test_set_threshold"""
    # Проверка установки порога
    auto_repeater.set_threshold(0.01)
    assert auto_repeater.threshold == Decimal('0.01')

    # Проверка установки нулевого порога
    auto_repeater.set_threshold(0)
    assert auto_repeater.threshold == Decimal('0')

    # Проверка установки максимального порога
    auto_repeater.set_threshold(1.0)
    assert auto_repeater.threshold == Decimal('1.0')

    # Проверка с невалидными значениями
    with pytest.raises(ValueError):
        auto_repeater.set_threshold(-0.1)  # Отрицательный порог
    with pytest.raises(ValueError):
        auto_repeater.set_threshold(1.1)  # Порог больше 100%
    with pytest.raises(TypeError):
        auto_repeater.set_threshold("0.01")  # Не число


def test_set_reserve(auto_repeater):
    """test_set_reserve"""
    # Проверка установки резерва
    auto_repeater.set_reserve(0.05)
    assert auto_repeater.reserve == Decimal('0.05')

    # Проверка установки нулевого резерва
    auto_repeater.set_reserve(0)
    assert auto_repeater.reserve == Decimal('0')

    # Проверка установки максимального резерва
    auto_repeater.set_reserve(1.0)
    assert auto_repeater.reserve == Decimal('1.0')

    # Проверка с невалидными значениями
    with pytest.raises(ValueError):
        auto_repeater.set_reserve(-0.1)  # Отрицательный резерв
    with pytest.raises(ValueError):
        auto_repeater.set_reserve(1.1)  # Резерв больше 100%
    with pytest.raises(TypeError):
        auto_repeater.set_reserve("0.05")  # Не число


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


def test_calc_ratio(auto_repeater):
    """test_calc_ratio"""
    src_account_id = '4'
    dst_account_id = '5'
    result = auto_repeater.calc_ratio(src_account_id, dst_account_id)
    assert len(result[0]) == 1
    assert result[0]['1'].current_price.units == 1
    assert result[0]['1'].current_price.nano == 200000000
    assert result[0]['1'].current_price.currency == 'RUB'
    assert result[0]['1'].instrument_type == 'share'
    assert result[0]['1'].quantity.units == 2
    assert result[0]['1'].quantity.nano == 0
    assert result[0]['1'].instrument_uid == '1'

    assert result[1] == {}

    assert result[2] == Decimal('0.99')
    assert result[3] == Decimal('2.376')


def test_calc_sell_positions(auto_repeater, client):
    """test_calc_sell_positions"""
    test_cases = [
        # Базовый случай
        {
            'dst_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(
                        currency='RUB', units=1, nano=200000000),
                    quantity=Quotation(units=100, nano=0)
                ),
                '2': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='2',
                    current_price=MoneyValue(
                        currency='RUB', units=2, nano=200000000),
                    quantity=Quotation(units=50, nano=0)
                )
            },
            'target_positions': {'1': 50},
            'expected': [
                OrderParams(
                    instrument_id='1',
                    quantity=50,
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE),
                OrderParams(
                    instrument_id='2',
                    quantity=50,
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE),
            ]
        },
        # Пустые позиции
        {
            'dst_positions': {},
            'target_positions': {},
            'expected': []
        },
        # Нет позиций для продажи
        {
            'dst_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(currency='RUB', units=1, nano=0),
                    quantity=Quotation(units=0, nano=0)
                )
            },
            'target_positions': {'1': 0},
            'expected': []
        },
        # Продажа всех позиций
        {
            'dst_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(currency='RUB', units=1, nano=0),
                    quantity=Quotation(units=100, nano=0)
                )
            },
            'target_positions': {'1': 0},
            'expected': [
                OrderParams(
                    instrument_id='1',
                    quantity=100,
                    direction=OrderDirection.ORDER_DIRECTION_SELL,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE)
            ]
        }
    ]

    for case in test_cases:
        result = auto_repeater.calc_sell_positions(
            case['dst_positions'], case['target_positions'])
        assert result == case['expected']

    assert client.instruments.get_instrument_by.call_args_list == [
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
    ]


def test_calc_buy_positions(auto_repeater, client):
    """test_calc_buy_positions"""
    test_cases = [
        # Базовый случай
        {
            'src_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(
                        currency='RUB', units=1, nano=200000000),
                    quantity=Quotation(units=100, nano=0)
                ),
                '2': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='2',
                    current_price=MoneyValue(
                        currency='RUB', units=2, nano=200000000),
                    quantity=Quotation(units=50, nano=0)
                )
            },
            'dst_positions': {
                '2': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='2',
                    current_price=MoneyValue(
                        currency='RUB', units=2, nano=200000000),
                    quantity=Quotation(units=50, nano=0)
                )
            },
            'target_positions': {
                '1': 50,
                '2': 100
            },
            'expected': [
                OrderParams(
                    instrument_id='1',
                    quantity=50,
                    direction=OrderDirection.ORDER_DIRECTION_BUY,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE),
                OrderParams(
                    instrument_id='2',
                    quantity=50,
                    direction=OrderDirection.ORDER_DIRECTION_BUY,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE),
            ]
        },
        # Пустые позиции
        {
            'src_positions': {},
            'dst_positions': {},
            'target_positions': {},
            'expected': []
        },
        # Нет позиций для покупки
        {
            'src_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(currency='RUB', units=1, nano=0),
                    quantity=Quotation(units=0, nano=0)
                )
            },
            'dst_positions': {},
            'target_positions': {'1': 0},
            'expected': []
        },
        # Покупка всех позиций
        {
            'src_positions': {
                '1': PortfolioPosition(
                    instrument_type='share',
                    instrument_uid='1',
                    current_price=MoneyValue(currency='RUB', units=1, nano=0),
                    quantity=Quotation(units=100, nano=0)
                )
            },
            'dst_positions': {},
            'target_positions': {'1': 100},
            'expected': [
                OrderParams(
                    instrument_id='1',
                    quantity=100,
                    direction=OrderDirection.ORDER_DIRECTION_BUY,
                    order_type=OrderType.ORDER_TYPE_BESTPRICE)
            ]
        }
    ]

    for case in test_cases:
        result = auto_repeater.calc_buy_positions(
            case['src_positions'],
            case['dst_positions'],
            case['target_positions'])
        assert result == case['expected']

    assert client.instruments.get_instrument_by.call_args_list == [
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
    ]


def test_post_orders(auto_repeater, client):
    """test_post_orders"""
    dst_account_id = '1'
    orders_params_sell = [
        OrderParams(
            instrument_id='1',
            quantity=100,
            direction=OrderDirection.ORDER_DIRECTION_SELL,
            order_type=OrderType.ORDER_TYPE_BESTPRICE
        )
    ]

    orders_params_buy = [
        OrderParams(
            instrument_id='2',
            quantity=50,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_type=OrderType.ORDER_TYPE_BESTPRICE
        )
    ]
    auto_repeater.post_orders(
        dst_account_id, orders_params_sell, orders_params_buy)
    assert client.orders.post_order.call_args_list == [
        call(instrument_id='1', quantity=100,
             direction=OrderDirection.ORDER_DIRECTION_SELL,
             account_id='1', order_type=OrderType.ORDER_TYPE_BESTPRICE),
        call(instrument_id='2', quantity=50,
             direction=OrderDirection.ORDER_DIRECTION_BUY,
             account_id='1', order_type=OrderType.ORDER_TYPE_BESTPRICE),
    ]


def test_sync_accounts(auto_repeater, client):
    """test_sync_accounts"""
    src_account_id = '4'
    dst_account_id = '5'
    auto_repeater.sync_accounts(src_account_id, dst_account_id)
    client.instruments.get_instrument_by.assert_called_once_with(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1')
    client.orders.post_order.assert_called_once_with(
        instrument_id='1', quantity=2,
        direction=OrderDirection.ORDER_DIRECTION_BUY,
        account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)


@pytest.mark.parametrize(
    'debug, threshold, held_quantity, expected_quantity',
    [
        (True, Decimal('0'), 99, 0),
        (False, Decimal('0.0101'), 99, 0),
        (False, Decimal('0.01'), 99, 0),
        (False, Decimal('0.0099'), 99, 1),
        (False, Decimal('0'), 100, 0),
    ],
    ids=['debug', 'below_threshold', 'exact_threshold', 'above_threshold', 'no_changes'],
)
def test_sync_accounts_submission_conditions(
        auto_repeater, client, debug, threshold, held_quantity, expected_quantity):
    """A one-ruble deficit in a 100-ruble portfolio tests the exact 1% boundary."""
    src_positions = {'1': PortfolioPosition(
        instrument_type='share', instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=100, nano=0))}
    dst_positions = {'1': PortfolioPosition(
        instrument_type='share', instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=held_quantity, nano=0))}
    client.operations.get_portfolio.side_effect = [
        PortfolioResponse(positions=list(src_positions.values())),
        PortfolioResponse(positions=[*dst_positions.values(), PortfolioPosition(
            instrument_type='currency',
            current_price=MoneyValue(currency='RUB', units=1, nano=0),
            quantity=Quotation(units=100 - held_quantity, nano=0))]),
    ]
    auto_repeater.set_reserve(Decimal('0'))
    auto_repeater.set_threshold(threshold)
    auto_repeater.set_debug(debug)

    # Suppression scenarios still have a real order to suppress, except no_changes.
    target_positions = {'1': Decimal('100')}
    assert auto_repeater.calc_sell_positions(dst_positions, target_positions) == []
    assert auto_repeater.calc_buy_positions(
        src_positions, dst_positions, target_positions) == (
            [OrderParams('1', 1, OrderDirection.ORDER_DIRECTION_BUY,
                         OrderType.ORDER_TYPE_BESTPRICE)] if held_quantity == 99 else [])

    auto_repeater.sync_accounts('4', '5')

    assert client.operations.get_portfolio.call_args_list == [
        call(account_id='4'), call(account_id='5')]
    if expected_quantity:
        client.orders.post_order.assert_called_once_with(
            instrument_id='1', quantity=expected_quantity,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)
    else:
        client.orders.post_order.assert_not_called()


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


@pytest.mark.parametrize('quantities, prices', [
    ({}, {}),
    ({'1': Decimal('2'), '2': Decimal('0')}, {'1': Decimal('1.25'), '2': Decimal('0')}),
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


@pytest.mark.parametrize('src', ['4', '0004', '0', '123456789012345678901234567890'])
def test_strategy_account_selection_without_sdk(src, client):
    """Account identifiers retain leading zeros and have no new length restriction."""
    with patch('t_tech.invest.Client', autospec=True) as sdk_client, \
            patch.object(strategies, 'AccountStrategy', wraps=AccountStrategy) as account_factory:
        assert strategies.validate_src(src) is None
        account_factory.assert_not_called()
        strategy = strategies.create_strategy(src)
        direct = AccountStrategy(src)

    assert isinstance(strategy, AccountStrategy)
    assert strategy.src == direct.src == src
    account_factory.assert_called_once_with(src)
    sdk_client.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize('src, message', [
    (None, 'src is required'), ('', 'src is required'), (' \t\n', 'src is required'),
    ('IMOEX', 'unsupported src: IMOEX'), ('unknown', 'unsupported src: unknown'),
    (' 123 ', 'unsupported src:  123 '), ('123\n', 'unsupported src: 123\n'),
    ('\u0661\u0662\u0663', 'unsupported src: \u0661\u0662\u0663'),
    ('\uff11\uff12\uff13', 'unsupported src: \uff11\uff12\uff13'),
    ('\u00b2', 'unsupported src: \u00b2'), ('+123', 'unsupported src: +123'),
    ('-123', 'unsupported src: -123'), ('12.3', 'unsupported src: 12.3'),
    (123, 'unsupported src: 123'), ([], 'unsupported src: []'),
])
def test_strategy_rejects_unsupported_source_without_construction(src, message):
    """Neither validation nor selection normalizes input or falls back to SDK accounts."""
    with patch.object(strategies, 'AccountStrategy', autospec=True) as account_factory, \
            patch('t_tech.invest.Client', autospec=True) as sdk_client:
        for select in [strategies.validate_src, strategies.create_strategy]:
            with pytest.raises(strategies.UnsupportedSourceError) as exc_info:
                select(src)
            assert isinstance(exc_info.value, ValueError)
            assert str(exc_info.value) == message

    account_factory.assert_not_called()
    sdk_client.assert_not_called()


def test_strategy_named_registration_is_exact_and_validation_is_pure(monkeypatch):
    """Only a registered factory constructs the named strategy, once and with unchanged src."""
    assert not strategies.NAMED_STRATEGIES
    strategy = Mock(spec_set=['load_snapshot', 'build_target', 'events'])
    factory = Mock(return_value=strategy)
    monkeypatch.setitem(strategies.NAMED_STRATEGIES, 'TEST', factory)

    with patch.object(strategies, 'AccountStrategy', autospec=True) as account_factory, \
            patch('t_tech.invest.Client', autospec=True) as sdk_client:
        assert strategies.validate_src('TEST') is None
        factory.assert_not_called()
        assert strategies.create_strategy('TEST') is strategy
        for src in ['test', ' TEST', 'TEST ']:
            with pytest.raises(strategies.UnsupportedSourceError):
                strategies.create_strategy(src)

    factory.assert_called_once_with('TEST')
    account_factory.assert_not_called()
    sdk_client.assert_not_called()


def test_strategy_numeric_source_precedes_registry(monkeypatch):
    """A registry entry cannot replace the account interpretation of ASCII digits."""
    factory = Mock()
    monkeypatch.setitem(strategies.NAMED_STRATEGIES, '0004', factory)

    assert strategies.create_strategy('0004').src == '0004'
    factory.assert_not_called()


def test_account_strategy_fractional_target(client, fractional_portfolios, caplog):
    """Source cash is reported but excluded; quantities and prices share one snapshot."""
    src_positions, _ = fractional_portfolios
    strategy = AccountStrategy('4')
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        snapshot = strategy.load_snapshot(client)

    assert snapshot == (src_positions, Decimal('204'))
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='4'),
        call.instruments.find_instrument(query='1'),
        call.instruments.find_instrument(query='2'),
    ]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging_config.IMPORTANT, message) for message in [
            'src account', 'share1(SHR) - 10.5 - RUB - 42.0',
            'etf2(ETF) - 20.25 - RUB - 162.0', 'RUB - 999.0', 'total: 204.000000000']]

    client.reset_mock()
    with patch('t_tech.invest.Client', autospec=True) as sdk_client:
        target = strategy.build_target(snapshot, Decimal('153'))
    assert target == TargetPortfolio(
        {'1': Decimal('7.875'), '2': Decimal('15.1875')},
        {'1': Decimal('4'), '2': Decimal('8')})
    validate_target(target)
    assert client.mock_calls == []
    sdk_client.assert_not_called()


def test_account_strategy_target_preserves_division_before_multiplication(client):
    """Decimal precision makes computing ratio first observably different from weights."""
    snapshot = ({
        '1': PortfolioPosition(current_price=MoneyValue('RUB', 1, 0), quantity=Quotation(3, 0)),
        '2': PortfolioPosition(current_price=MoneyValue('RUB', 0, 1), quantity=Quotation(0, 0)),
        '3': PortfolioPosition(current_price=MoneyValue('RUB', 0, 0), quantity=Quotation(0, 1)),
    }, Decimal('3'))

    target = AccountStrategy('4').build_target(snapshot, Decimal('1'))

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
    strategy = AccountStrategy('0004')
    first = strategy.load_snapshot(client)
    second = strategy.load_snapshot(client)

    assert strategy.build_target(first, Decimal('8')) == TargetPortfolio(
        {'1': Decimal('2')}, {'1': Decimal('4')})
    assert strategy.build_target(second, Decimal('30')) == TargetPortfolio(
        {'2': Decimal('6')}, {'2': Decimal('5')})
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='0004'),
        call.instruments.find_instrument(query='1'),
        call.operations.get_portfolio(account_id='0004'),
        call.instruments.find_instrument(query='2')]


def test_account_strategy_load_error(client, caplog):
    """A failed source read propagates its error instead of becoming a liquidation target."""
    error = RequestError(code=StatusCode.UNAVAILABLE, details='source unavailable', metadata=())
    client.operations.get_portfolio.side_effect = error

    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME), \
            pytest.raises(RequestError) as exc_info:
        AccountStrategy('4').load_snapshot(client)

    assert exc_info.value is error
    assert client.mock_calls == [call.operations.get_portfolio(account_id='4')]
    assert [record.getMessage() for record in caplog.records] == ['src account']


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
    strategy = AccountStrategy('4')
    snapshot = strategy.load_snapshot(client)
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
def test_account_strategy_events(client, position, expected, caplog):
    """One call consumes one subscription and reports only skipped SDK responses."""
    event = PositionsStreamResponse(position=position)
    client.operations_stream.positions_stream.side_effect = [iter([event])]

    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        assert list(AccountStrategy('4').events(client, '5')) == [expected]

    assert client.mock_calls == [call.operations_stream.positions_stream(accounts=['4', '5'])]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == (
        [] if expected else [(logging_config.IMPORTANT, str(event))])


def test_account_strategy_empty_events(client):
    """An exhausted stream returns control to the engine without resubscribing itself."""
    client.operations_stream.positions_stream.side_effect = [iter(())]

    assert not list(AccountStrategy('0004').events(client, '5'))
    assert client.mock_calls == [call.operations_stream.positions_stream(accounts=['0004', '5'])]


@pytest.mark.parametrize('during_iteration', [False, True])
def test_account_strategy_event_errors(client, during_iteration):
    """Errors opening or reading a stream reach the engine after at most one subscription."""
    error = RequestError(code=StatusCode.UNAVAILABLE, details='stream unavailable', metadata=())

    def interrupted_stream():
        yield PositionsStreamResponse(position=PositionData(
            account_id='4', money=[], securities=[
                PositionsSecurities(instrument_uid='1', blocked=0)]))
        raise error

    client.operations_stream.positions_stream.side_effect = [
        interrupted_stream() if during_iteration else error]
    events = AccountStrategy('4').events(client, '5')
    if during_iteration:
        assert next(events) is True
    with pytest.raises(RequestError) as exc_info:
        next(events)

    assert exc_info.value is error
    assert client.mock_calls == [call.operations_stream.positions_stream(accounts=['4', '5'])]


def test_calc_ratio_fractional_portfolios(auto_repeater, client, fractional_portfolios):
    """Keep fractional holdings, exclude source cash and reserve destination value once."""
    src_positions, dst_positions = fractional_portfolios
    auto_repeater.set_reserve(Decimal('0.1'))

    assert auto_repeater.calc_ratio('4', '5') == (
        src_positions, dst_positions, Decimal('0.75'), Decimal('153'))
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='4'),
        call.instruments.find_instrument(query='1'),
        call.instruments.find_instrument(query='2'),
        call.operations.get_portfolio(account_id='5'),
        call.instruments.find_instrument(query='1'),
        call.instruments.find_instrument(query='2'),
    ]


def test_calc_orders_fractional_portfolios(auto_repeater, client, fractional_portfolios):
    """Lot rounding and threshold valuation retain separate source/destination prices."""
    src_positions, dst_positions = fractional_portfolios
    target_positions = {'1': Decimal('7.875'), '2': Decimal('15.1875')}

    sell_orders = auto_repeater.calc_sell_positions(dst_positions, target_positions)
    buy_orders = auto_repeater.calc_buy_positions(src_positions, dst_positions, target_positions)

    assert sell_orders == [OrderParams(
        '1', 1, OrderDirection.ORDER_DIRECTION_SELL, OrderType.ORDER_TYPE_BESTPRICE)]
    assert buy_orders == [OrderParams(
        '2', 3, OrderDirection.ORDER_DIRECTION_BUY, OrderType.ORDER_TYPE_BESTPRICE)]
    # Existing valuation multiplies price by lots, without multiplying by lot size.
    assert get_max_sum_positions_price(
        sell_orders, [], src_positions, dst_positions) == Decimal('3')
    assert get_max_sum_positions_price(
        [], buy_orders, src_positions, dst_positions) == Decimal('24')
    assert get_max_sum_positions_price(
        sell_orders, buy_orders, src_positions, dst_positions) == Decimal('24')
    assert client.mock_calls == [
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
    ]


@pytest.mark.parametrize('threshold, submit', [(Decimal('0.15'), True), (Decimal('0.16'), False)])
def test_sync_accounts_fractional_portfolios(
        auto_repeater, client, fractional_portfolios, threshold, submit):
    """Fully calculate both sides before submitting sales, then purchases, above threshold."""
    src_positions, dst_positions = fractional_portfolios
    auto_repeater.set_reserve(Decimal('0.1'))
    auto_repeater.set_threshold(threshold)

    with patch.object(auto_repeater, 'calc_sell_positions', autospec=True,
                      side_effect=auto_repeater.calc_sell_positions) as calc_sell, \
            patch.object(auto_repeater, 'calc_buy_positions', autospec=True,
                         side_effect=auto_repeater.calc_buy_positions) as calc_buy:
        auto_repeater.sync_accounts('4', '5')
        target_positions = {'1': Decimal('7.875'), '2': Decimal('15.1875')}
        calc_sell.assert_called_once_with(dst_positions, target_positions)
        calc_buy.assert_called_once_with(src_positions, dst_positions, target_positions)

    expected_calls = [
        call.operations.get_portfolio(account_id='4'),
        call.instruments.find_instrument(query='1'),
        call.instruments.find_instrument(query='2'),
        call.operations.get_portfolio(account_id='5'),
        call.instruments.find_instrument(query='1'),
        call.instruments.find_instrument(query='2'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
    ]
    if submit:
        expected_calls.extend([
            call.orders.post_order(
                instrument_id='1', quantity=1, direction=OrderDirection.ORDER_DIRECTION_SELL,
                account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE),
            call.orders.post_order(
                instrument_id='2', quantity=3, direction=OrderDirection.ORDER_DIRECTION_BUY,
                account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE),
        ])
    assert client.mock_calls == expected_calls


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


@pytest.mark.parametrize('closed_instrument', ['1', '2'], ids=['sell_closed', 'buy_closed'])
def test_sync_accounts_non_trading_instrument(
        auto_repeater, client, rotation_portfolios, closed_instrument):
    """Closed instruments are excluded from calculation and submission."""
    instruments = {
        uid: InstrumentResponse(instrument=Instrument(
            uid=uid, name=f'instrument {uid}', ticker=f'TEST{uid}', lot=1,
            trading_status=(SecurityTradingStatus.SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING
                            if uid == closed_instrument else
                            SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING)))
        for uid in ('1', '2')
    }
    client.instruments.get_instrument_by.side_effect = lambda **kwargs: instruments[kwargs['id']]
    auto_repeater.set_reserve(Decimal('0'))
    src_positions, dst_positions = rotation_portfolios
    target_positions = {'2': Decimal('50')}

    assert auto_repeater.calc_sell_positions(dst_positions, target_positions) == (
        [] if closed_instrument == '1' else [OrderParams(
            '1', 100, OrderDirection.ORDER_DIRECTION_SELL, OrderType.ORDER_TYPE_BESTPRICE)])
    assert auto_repeater.calc_buy_positions(src_positions, dst_positions, target_positions) == (
        [] if closed_instrument == '2' else [OrderParams(
            '2', 50, OrderDirection.ORDER_DIRECTION_BUY, OrderType.ORDER_TYPE_BESTPRICE)])

    auto_repeater.sync_accounts('4', '5')

    if closed_instrument == '1':
        client.orders.post_order.assert_called_once_with(
            instrument_id='2', quantity=50,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)
    else:
        client.orders.post_order.assert_called_once_with(
            instrument_id='1', quantity=100,
            direction=OrderDirection.ORDER_DIRECTION_SELL,
            account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)


def test_sync_accounts_sale_error_stops_purchase(auto_repeater, client, rotation_portfolios):
    """The SDK sale error propagates before any purchase can be submitted."""
    auto_repeater.set_reserve(Decimal('0'))
    src_positions, dst_positions = rotation_portfolios
    assert auto_repeater.calc_buy_positions(
        src_positions, dst_positions, {'2': Decimal('50')}) == [OrderParams(
            '2', 50, OrderDirection.ORDER_DIRECTION_BUY, OrderType.ORDER_TYPE_BESTPRICE)]
    error = RequestError(code=StatusCode.UNAVAILABLE, details='sale unavailable', metadata=())
    client.orders.post_order.side_effect = error

    with pytest.raises(RequestError) as raised:
        auto_repeater.sync_accounts('4', '5')

    assert raised.value is error
    client.orders.post_order.assert_called_once_with(
        instrument_id='1', quantity=100,
        direction=OrderDirection.ORDER_DIRECTION_SELL,
        account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)


@pytest.mark.parametrize('failed_account', ['4', '5'], ids=['source', 'destination'])
def test_sync_accounts_portfolio_error(auto_repeater, client, rotation_portfolios, failed_account):
    """A failed portfolio request never reaches order calculation or submission."""
    src_positions, _ = rotation_portfolios
    error = RequestError(code=StatusCode.UNAVAILABLE, details='portfolio unavailable', metadata=())
    client.operations.get_portfolio.side_effect = (
        [error] if failed_account == '4' else
        [PortfolioResponse(positions=list(src_positions.values())), error])

    with pytest.raises(RequestError) as raised:
        auto_repeater.sync_accounts('4', '5')

    assert raised.value is error
    assert client.operations.get_portfolio.call_args_list == (
        [call(account_id='4')] if failed_account == '4' else
        [call(account_id='4'), call(account_id='5')])
    client.instruments.get_instrument_by.assert_not_called()
    client.orders.post_order.assert_not_called()


def test_sync_accounts_purchase_calculation_error(auto_repeater, client, rotation_portfolios):
    """A calculated sale must not be sent when the subsequent purchase lookup fails."""
    auto_repeater.set_reserve(Decimal('0'))
    _, dst_positions = rotation_portfolios
    assert auto_repeater.calc_sell_positions(dst_positions, {'2': Decimal('50')}) == [OrderParams(
        '1', 100, OrderDirection.ORDER_DIRECTION_SELL, OrderType.ORDER_TYPE_BESTPRICE)]
    client.reset_mock()
    error = RequestError(code=StatusCode.UNAVAILABLE, details='instrument unavailable', metadata=())
    client.instruments.get_instrument_by.side_effect = [
        InstrumentResponse(instrument=Instrument(
            uid='1', name='share1', ticker='SHR', lot=1,
            trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING)),
        error,
    ]

    with pytest.raises(RequestError) as raised:
        auto_repeater.sync_accounts('4', '5')

    assert raised.value is error
    assert client.mock_calls == [
        call.operations.get_portfolio(account_id='4'),
        call.instruments.find_instrument(query='2'),
        call.operations.get_portfolio(account_id='5'),
        call.instruments.find_instrument(query='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1'),
        call.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='2'),
    ]
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('src_positions', [
    [],
    [PortfolioPosition(
        instrument_type='currency', instrument_uid='cash',
        current_price=MoneyValue(currency='RUB', units=1, nano=0),
        quantity=Quotation(units=100, nano=0))],
    [PortfolioPosition(
        instrument_type='share', instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=0, nano=0),
        quantity=Quotation(units=100, nano=0))],
], ids=['empty', 'cash_only', 'zero_price'])
def test_sync_accounts_zero_source_value(auto_repeater, client, src_positions):
    """A zero-value source raises instead of silently liquidating the destination."""
    client.operations.get_portfolio.side_effect = [
        PortfolioResponse(positions=src_positions),
        PortfolioResponse(positions=[PortfolioPosition(
            instrument_type='share', instrument_uid='2',
            current_price=MoneyValue(currency='RUB', units=2, nano=0),
            quantity=Quotation(units=50, nano=0))]),
    ]

    with pytest.raises(DivisionByZero):
        auto_repeater.sync_accounts('4', '5')

    assert client.operations.get_portfolio.call_args_list == [
        call(account_id='4'), call(account_id='5')]
    client.instruments.get_instrument_by.assert_not_called()
    client.orders.post_order.assert_not_called()


def test_mainflow(auto_repeater, client):
    """test_mainflow"""
    src_account_id = '4'
    dst_account_id = '5'
    with pytest.raises(TestException):
        auto_repeater.mainflow(src_account_id, dst_account_id)
    assert client.operations_stream.positions_stream.call_args_list == [
        call(accounts=['4', '5']),
        call(accounts=['4', '5']),
        call(accounts=['4', '5']),
        call(accounts=['4', '5']),
    ]
    client.instruments.get_instrument_by.assert_called_once_with(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='1')
    client.orders.post_order.assert_called_once_with(
        instrument_id='1', quantity=2,
        direction=OrderDirection.ORDER_DIRECTION_BUY,
        account_id='5', order_type=OrderType.ORDER_TYPE_BESTPRICE)


@pytest.mark.parametrize(
    'position, expected_sync_calls',
    [
        (PositionData(
            account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=0)],
            money=[]), [call('4', '5'), call('4', '5')]),
        (PositionData(
            account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=1)],
            money=[]), [call('4', '5')]),
        (PositionData(
            account_id='5', securities=[], money=[PositionsMoney(
                available_value=MoneyValue(currency='RUB', units=100, nano=0),
                blocked_value=MoneyValue(currency='RUB', units=0, nano=0))]),
         [call('4', '5'), call('4', '5')]),
        (PositionData(
            account_id='5', securities=[], money=[PositionsMoney(
                available_value=MoneyValue(currency='RUB', units=100, nano=0),
                blocked_value=MoneyValue(currency='RUB', units=0, nano=1))]),
         [call('4', '5')]),
    ],
    ids=['source_unblocked', 'source_blocked', 'destination_unblocked', 'destination_blocked'],
)
def test_mainflow_position_events(auto_repeater, client, position, expected_sync_calls, caplog):
    """Only matching position events add a sync after the initial one."""
    event = PositionsStreamResponse(position=position)
    client.operations_stream.positions_stream.side_effect = [
        iter([event]),
        TestException(),
    ]

    with patch.object(auto_repeater, 'sync_accounts', autospec=True) as sync_accounts:
        with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME), \
                pytest.raises(TestException):
            auto_repeater.mainflow('4', '5')
        assert sync_accounts.call_args_list == expected_sync_calls

    assert client.operations_stream.positions_stream.call_args_list == [
        call(accounts=['4', '5']), call(accounts=['4', '5']),
    ]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == (
        [(logging_config.IMPORTANT, str(event))] if len(expected_sync_calls) == 1 else [])


def test_mainflow_stream_iteration_error(auto_repeater, client):
    """An error while reading a stream leads to a working new subscription."""
    def interrupted_stream():
        yield PositionsStreamResponse(position=PositionData(
            account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=1)],
            money=[]))
        raise RequestError(code=StatusCode.UNAVAILABLE, details='stream unavailable', metadata=())

    client.operations_stream.positions_stream.side_effect = [
        interrupted_stream(),
        iter([PositionsStreamResponse(position=PositionData(
            account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=0)],
            money=[]))]),
        TestException(),
    ]

    with patch.object(auto_repeater, 'sync_accounts', autospec=True) as sync_accounts:
        with pytest.raises(TestException):
            auto_repeater.mainflow('4', '5')
        assert sync_accounts.call_args_list == [call('4', '5'), call('4', '5')]

    assert client.operations_stream.positions_stream.call_args_list == [
        call(accounts=['4', '5']), call(accounts=['4', '5']), call(accounts=['4', '5']),
    ]


def test_mainflow_initial_sync_error(auto_repeater, client):
    """A failed initial sync still allows subscribing and syncing on an event."""
    client.operations_stream.positions_stream.side_effect = [
        iter([PositionsStreamResponse(position=PositionData(
            account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=0)],
            money=[]))]),
        TestException(),
    ]
    error = RequestError(code=StatusCode.UNAVAILABLE, details='sync unavailable', metadata=())

    with patch.object(auto_repeater, 'sync_accounts', autospec=True,
                      side_effect=[error, None]) as sync_accounts:
        with pytest.raises(TestException):
            auto_repeater.mainflow('4', '5')
        assert sync_accounts.call_args_list == [call('4', '5'), call('4', '5')]

    assert client.operations_stream.positions_stream.call_args_list == [
        call(accounts=['4', '5']), call(accounts=['4', '5']),
    ]


def test_mainflow_event_sync_error(auto_repeater, client, caplog):
    """A failed event sync abandons that stream and syncs again on a new subscription."""
    event = PositionsStreamResponse(position=PositionData(
        account_id='4', securities=[PositionsSecurities(instrument_uid='1', blocked=0)],
        money=[]))
    failed_stream = iter([event, event])
    client.operations_stream.positions_stream.side_effect = [
        failed_stream, iter([event]), TestException(),
    ]
    error = RequestError(code=StatusCode.UNAVAILABLE, details='event sync unavailable', metadata=())
    timeline = Mock()
    timeline.attach_mock(client.operations_stream.positions_stream, 'stream')

    with patch.object(auto_repeater, 'sync_accounts', autospec=True,
                      side_effect=[None, error, None]) as sync_accounts:
        timeline.attach_mock(sync_accounts, 'sync')
        with pytest.raises(TestException):
            auto_repeater.mainflow('4', '5')
        assert sync_accounts.call_args_list == [call('4', '5'), call('4', '5'), call('4', '5')]

    assert timeline.mock_calls == [
        call.sync('4', '5'),
        call.stream(accounts=['4', '5']),
        call.sync('4', '5'),
        call.stream(accounts=['4', '5']),
        call.sync('4', '5'),
        call.stream(accounts=['4', '5']),
    ]
    assert next(failed_stream) is event
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.ERROR, str(error))]
    client.orders.post_order.assert_not_called()


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


@pytest.mark.parametrize('failure', ['accounts', 'portfolio', 'instrument', 'unknown'])
def test_reporting_error(client, caplog, failure):
    """Display failures propagate at the same point without submitting orders."""
    error = (GetInstrumentException('error get instrument') if failure == 'unknown' else
             RequestError(code=StatusCode.UNAVAILABLE, details='display unavailable', metadata=()))
    client.users.get_accounts.return_value = GetAccountsResponse(
        accounts=[Account(id='4', name='source')])
    expected_calls = [call.users.get_accounts()]
    if failure == 'accounts':
        client.users.get_accounts.side_effect = error
    else:
        expected_calls.append(call.operations.get_portfolio(account_id='4'))
        if failure == 'portfolio':
            client.operations.get_portfolio.side_effect = error
        else:
            expected_calls.append(call.instruments.find_instrument(query='1'))
            client.instruments.find_instrument.side_effect = (
                None if failure == 'unknown' else error)
            client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=[])
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME), \
            pytest.raises(type(error)) as raised:
        reporting.print_all_portfolio(client)
    assert str(raised.value) == str(error)
    if failure != 'unknown':
        assert raised.value is error
    assert client.mock_calls == expected_calls
    assert caplog.messages == ([] if failure == 'accounts' else ['source (4)', '------------'])
    client.orders.post_order.assert_not_called()


@pytest.mark.usefixtures('fractional_portfolios')
def test_sync_reporting(auto_repeater, client, caplog):
    """Output preserves totals and sale/purchase order using only the two loaded snapshots."""
    auto_repeater.set_reserve(Decimal('0.1'))
    auto_repeater.set_threshold(Decimal('0'))
    before_submission = []
    before_loading = []
    portfolios = client.operations.get_portfolio.side_effect

    def load_portfolio(**_kwargs):
        before_loading.append(list(caplog.messages))
        return next(portfolios)

    def post_order(**_kwargs):
        before_submission.append(list(caplog.messages))
        return PostOrderResponse(order_id=f'order-{len(before_submission)}')

    client.orders.post_order.side_effect = post_order
    client.operations.get_portfolio.side_effect = load_portfolio
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        auto_repeater.sync_accounts('4', '5')

    sale = OrderParams('1', 1, OrderDirection.ORDER_DIRECTION_SELL, OrderType.ORDER_TYPE_BESTPRICE)
    purchase = OrderParams(
        '2', 3, OrderDirection.ORDER_DIRECTION_BUY, OrderType.ORDER_TYPE_BESTPRICE)
    expected = [
        'src account', 'share1(SHR) - 10.5 - RUB - 42.0', 'etf2(ETF) - 20.25 - RUB - 162.0',
        'RUB - 999.0', 'total: 204.000000000',
        'dst account', 'share1(SHR) - 10.25 - RUB - 30.75', 'etf2(ETF) - 1.125 - RUB - 7.875',
        'RUB - 131.375', 'total: 153.0000000000',
        'Продать: instrument 1(TEST1) 1 лотов', 'Купить: instrument 2(TEST2) 3 лотов',
        str(sale), 'order-1', str(purchase), 'order-2',
    ]
    assert caplog.messages == expected
    assert before_submission == [expected[:13], expected[:15]]
    assert before_loading == [expected[:1], expected[:6]]
    assert all(record.levelno == logging_config.IMPORTANT for record in caplog.records)
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id='4'), call(account_id='5')]
    assert client.orders.post_order.call_args_list == [
        call(instrument_id=order.instrument_id, quantity=order.quantity,
             direction=order.direction, account_id='5', order_type=order.order_type)
        for order in [sale, purchase]]


def test_reporting_orders_are_read_only(client, caplog):
    """Reporting orders needs only already calculated values and never sends them."""
    instrument = Instrument(name='share1', ticker='SHR')
    order = OrderParams('1', 2, OrderDirection.ORDER_DIRECTION_BUY, OrderType.ORDER_TYPE_BESTPRICE)
    with caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        reporting.print_sell(instrument, 1)
        reporting.print_buy(instrument, 2)
        reporting.print_order(order)
        reporting.print_order_result('order-id')
    assert caplog.messages == [
        'Продать: share1(SHR) 1 лотов', 'Купить: share1(SHR) 2 лотов', str(order), 'order-id']
    assert client.mock_calls == []


@pytest.mark.parametrize('method', ['run', 'run_sync'])
def test_runner_reporting_integration(method, client, caplog):
    """The real engine works in both modes; only run displays all accounts."""
    client.operations_stream.positions_stream.side_effect = [TestException()]
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'configure_local_logging', autospec=True), \
            caplog.at_level(logging_config.IMPORTANT, logger=logging_config.LOGGER_NAME):
        sdk_client.return_value.__enter__.return_value = client
        runner = runner_module.Runner('test-token', '4', '5', RunnerParams(True, None, None))
        if method == 'run':
            with pytest.raises(TestException):
                runner.run()
        else:
            runner.run_sync()
        sdk_client.return_value.__exit__.assert_called_once()

    expected_accounts = ['1', '2', '4', '5'] if method == 'run' else ['4', '5']
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id=account) for account in expected_accounts]
    assert client.users.get_accounts.call_args_list == ([call()] if method == 'run' else [])
    assert client.operations_stream.positions_stream.call_args_list == (
        [call(accounts=['4', '5'])] if method == 'run' else [])
    assert caplog.messages[-7:] == [
        'src account', 'share1(SHR) - 2.0 - RUB - 2.4', 'total: 2.400000000',
        'dst account', 'RUB - 2.4', 'total: 2.37600000000', 'Купить: share1(SHR) 2 лотов']
    client.orders.post_order.assert_not_called()


@pytest.mark.parametrize('method', ['run', 'run_sync'])
@pytest.mark.parametrize('src, dst', [('4', '5'), (None, '5'), ('4', None)])
def test_runner_modes(method, src, dst, client):
    """Runner applies parameters and selects the requested mode inside the client context."""
    params = RunnerParams(debug=True, threshold=0.01, reserve=0.02)
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'AutoRepeater', autospec=True) as repeater_class, \
            patch.object(runner_module, 'configure_local_logging', autospec=True) as configure:
        sdk_client.return_value.__enter__.return_value = client
        runner = runner_module.Runner('test-token', src, dst, params)
        getattr(runner, method)()

        configure.assert_called_once_with()
        sdk_client.assert_called_once_with(token='test-token', target=runner_module.INVEST_GRPC_API)
        sdk_client.return_value.__enter__.assert_called_once_with()
        sdk_client.return_value.__exit__.assert_called_once_with(None, None, None)
        repeater_class.assert_called_once_with(client)
        expected = [call.set_debug(True), call.set_threshold(0.01), call.set_reserve(0.02)]
        if src and dst:
            expected += [call.mainflow(src, dst) if method == 'run'
                         else call.sync_accounts(src, dst)]
        assert repeater_class.return_value.method_calls == expected
        assert client.mock_calls == ([
            call.users.get_accounts(),
            call.operations.get_portfolio(account_id='1'),
            call.operations.get_portfolio(account_id='2'),
        ] if method == 'run' else [])


@pytest.fixture(name='invest_environment')
def invest_environment_fixture(monkeypatch):
    """All entrypoint tests use controlled credentials and account parameters."""
    for name in ('INVEST_TOKEN', 't_token', 'SRC_ACCOUNT', 'DST_ACCOUNT'):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    'event, environment, expected',
    [
        ({'queryStringParameters': {
            'src': 'query-src', 'dst': 'query-dst', 'token': 'query-token'}},
         {'SRC_ACCOUNT': 'env-src', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token',
          't_token': 'legacy-token'}, ('query-src', 'query-dst', 'query-token')),
        ({'queryStringParameters': None},
         {'SRC_ACCOUNT': 'env-src', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token',
          't_token': 'legacy-token'}, ('env-src', 'env-dst', 'env-token')),
        ({'queryStringParameters': {'src': '', 'dst': '', 'token': ''}},
         {'SRC_ACCOUNT': 'env-src', 'DST_ACCOUNT': 'env-dst', 'INVEST_TOKEN': 'env-token'},
         ('env-src', 'env-dst', 'env-token')),
        ({}, {'t_token': 'legacy-token'},
         (serverless.DEFAULT_SRC_ACCOUNT, serverless.DEFAULT_DST_ACCOUNT, 'legacy-token')),
        (None, {'INVEST_TOKEN': 'env-token'},
         (serverless.DEFAULT_SRC_ACCOUNT, serverless.DEFAULT_DST_ACCOUNT, 'env-token')),
    ],
    ids=['query_priority', 'null_query', 'empty_query_values', 'legacy_token', 'no_event'],
)
def test_cloud_entrypoint(event, environment, expected, invest_environment):
    """The deployed entrypoint resolves parameters and performs exactly one sync."""
    for name, value in environment.items():
        invest_environment.setenv(name, value)
    src, dst, token = expected
    with patch.object(serverless, 'Runner', autospec=True) as runner_class, \
            patch.object(serverless, 'configure_yc_logging', autospec=True) as configure:
        result = cloud_entrypoint.handler(event, None)

        configure.assert_called_once_with()
        runner_class.assert_called_once_with(token=token, src=src, dst=dst)
        assert runner_class.return_value.method_calls == [call.run_sync()]
        assert result == {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'isBase64Encoded': False,
            'body': f'Success sync, {src} {dst}!',
        }


def test_cloud_missing_token(invest_environment):
    """Missing credentials fail before creating the runner."""
    invest_environment.delenv('INVEST_TOKEN', raising=False)
    with patch.object(serverless, 'Runner', autospec=True) as runner_class, \
            patch.object(serverless, 'configure_yc_logging', autospec=True):
        with pytest.raises(KeyError, match='t_token'):
            cloud_entrypoint.handler({}, None)
        runner_class.assert_not_called()


def test_cloud_sync_error(invest_environment):
    """A failed sync must not produce a successful HTTP response."""
    invest_environment.setenv('INVEST_TOKEN', 'test-token')
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
        ([], None, None, RunnerParams(debug=False, threshold=None, reserve=None)),
        (['-s', '4', '-d', '5', '--debug', '-t', '0.01', '-r', '0.02'],
         '4', '5', RunnerParams(debug=True, threshold=0.01, reserve=0.02)),
    ],
    ids=['defaults', 'all_options'],
)
def test_cli(arguments, src, dst, params, invest_environment):
    """Command-line options and environment credentials reach the local runner."""
    invest_environment.setenv('INVEST_TOKEN', 'cli-token')
    invest_environment.setattr(sys, 'argv', ['main.py', *arguments])
    with patch.object(cli, 'Runner', autospec=True) as runner_class:
        cli.main()
        runner_class.assert_called_once_with(token='cli-token', src=src, dst=dst, params=params)
        assert runner_class.return_value.method_calls == [call.run()]


def test_cli_script(invest_environment):
    """Executing main.py invokes the local runner without connecting to the API."""
    invest_environment.setenv('INVEST_TOKEN', 'cli-token')
    invest_environment.setattr(sys, 'argv', ['main.py'])
    with patch.object(runner_module, 'Runner', autospec=True) as runner_class:
        runpy.run_path('main.py', run_name='__main__')
        runner_class.assert_called_once_with(
            token='cli-token', src=None, dst=None,
            params=RunnerParams(debug=False, threshold=None, reserve=None))
        runner_class.return_value.run.assert_called_once_with()


@pytest.mark.parametrize('arguments', [[], ['--threshold', 'invalid']])
def test_cli_invalid_input(arguments, invest_environment):
    """Missing credentials and invalid CLI arguments cannot start a runner."""
    invest_environment.setattr(sys, 'argv', ['main.py', *arguments])
    with patch.object(cli, 'Runner', autospec=True) as runner_class:
        if arguments:
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
    [(logging_config.IMPORTANT, 'INFO'), (logging.WARNING, 'WARN'), (logging.CRITICAL, 'FATAL')],
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
    assert logger.level == logging_config.IMPORTANT
    assert logger.propagate is False
    record = logging.LogRecord(logger.name, level, __file__, 0, 'sync %s', ('complete',), None)
    result = json.loads(first_handler.format(record))
    assert result['message'] == 'sync complete'
    assert result['level'] == expected
    assert result['logger'] == logging_config.LOGGER_NAME
