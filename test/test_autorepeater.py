# pylint: disable=R0903
"""tests"""
import pytest

from tinkoff.invest import MoneyValue
from tinkoff.invest import Instrument
from tinkoff.invest import PortfolioPosition
from tinkoff.invest import Quotation
from tinkoff.invest import PositionData
from tinkoff.invest import PositionsMoney
from tinkoff.invest import PositionsSecurities
from tinkoff.invest import OrderDirection
from tinkoff.invest import OrderType
from tinkoff.invest import FindInstrumentResponse
from tinkoff.invest import InstrumentResponse
from tinkoff.invest import PortfolioResponse
from tinkoff.invest import InstrumentShort
from tinkoff.invest import GetAccountsResponse
from tinkoff.invest import Account
from tinkoff.invest import AccountType
from tinkoff.invest import AccountStatus
from tinkoff.invest import InstrumentIdType
from tinkoff.invest import SecurityTradingStatus
from tinkoff.invest import PostOrderResponse
from tinkoff.invest import RequestError

from app.autorepeater import money_to_string
from app.autorepeater import no_money_to_string
from app.autorepeater import blocked_to_string
from app.autorepeater import currency_to_float
from app.autorepeater import currency_to_string
from app.autorepeater import currency_to_float_price
from app.autorepeater import get_quantity_position
from app.autorepeater import check_triggers
from app.autorepeater import get_max_sum_positions_price
from app.autorepeater import OrderParams
from app.autorepeater import AutoRepeater
from app.autorepeater import THRESHOLD
from app.autorepeater import DST_MONEY_RESERVED
from app.autorepeater import GetInstrumentException


class TestException(Exception):
    """TestException исключение для остановки бесконечного цикла в тесте mainflow"""


@pytest.mark.parametrize(
    'currency, units, nano, expected',
    [
        ('RUB', 1, 500000000, 'RUB - 1.5'),
        ('RUB', -1, -500000000, 'RUB - -1.5'),
        ('RUB', 0, 0, 'RUB - 0.0'),
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
    ]
)
def test_no_money_to_string(instrument, expected):
    """instrument to string"""
    result = no_money_to_string(instrument)

    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, 3.0),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, -2.25),
        (MoneyValue('RUB', 0, 0), 5, 500000000, 0),
    ]
)
def test_currency_to_float(money, quantity_units, quantity_nano, expected):
    """currency to float"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_float(position)

    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, 1.5),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, -1.5),
        (MoneyValue('RUB', 0, 0), 5, 500000000, 0),
    ]
)
def test_currency_to_float_price(money, quantity_units, quantity_nano, expected):
    """currency price to float"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_float_price(position)

    assert result == expected


@pytest.mark.parametrize(
    'money, quantity_units, quantity_nano, expected',
    [
        (MoneyValue('RUB', 1, 500000000), 2, 0, 'RUB - 3.0'),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, 'RUB - -2.25'),
        (MoneyValue('RUB', 0, 0), 5, 500000000, 'RUB - 0.0'),
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
        (MoneyValue('RUB', 1, 500000000), 2, 0, 2.0),
        (MoneyValue('RUB', -1, -500000000), 1, 500000000, 1.5),
        (MoneyValue('RUB', 0, 0), 5, 500000000, 5.5),
    ]
)
def test_get_quantity_position(money, quantity_units, quantity_nano, expected):
    """quantity position"""
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
        ('1', [], [], False),
        ('2', [], [PositionsMoney(blocked_value=MoneyValue(units=1))], False),
        ('1', [PositionsSecurities(blocked=0)], [PositionsMoney()], True),
        ('2', [], [PositionsMoney(blocked_value=MoneyValue(units=0, nano=0))], True),
    ]
)
def test_check_triggers(account_id, position_money, position_securities, expected):
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
    ]
)
def test_get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                     src_positions, dst_positions, expected):
    """get_max_sum_positions_price"""

    result = get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                         src_positions, dst_positions)

    assert result == expected

class FakeClient:
    """FakeClient mock для клиента тинькофф инвестиций"""
    class FakeInstruments:
        """FakeInstruments mock для работы с инструментами тинькофф инвестиций"""
        def find_instrument(self, query):
            """find_instrument mock для поиска инструмента"""
            names = {
                "1": "share1",
                "2": "etf2"
            }
            tickers = {
                "1": "SHR",
                "2": "ETF"
            }
            try:
                return FindInstrumentResponse(
                    instruments=[InstrumentShort(
                        name=names[query], ticker=tickers[query])]
                )
            except KeyError:
                return FindInstrumentResponse(
                    instruments=[]
                )

# pylint: disable=W0622,C0103
        def get_instrument_by(self, id_type, id):
            """get_instrument_by mock для получения инструмента по его id"""
            assert id_type == InstrumentIdType.INSTRUMENT_ID_TYPE_UID
            names = {
                "1": "share1",
                "2": "etf2"
            }
            tickers = {
                "1": "SHR",
                "2": "ETF"
            }
            try:
                return InstrumentResponse(
                    instrument=Instrument(
                        name=names[id],
                        ticker=tickers[id],
                        trading_status=SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
                        lot=1
                        )
                        )
            except KeyError:
                assert False
# pylint: enable=W0622,C0103

    class FakeOperations:
        """FakeOperations mock для работы с операциями тинькофф инвестиций"""
        def get_portfolio(self, account_id):
            """get_portfolio mock для получения данных о составе инструментов на брокерском счёте"""
            if account_id == '1':
                return PortfolioResponse(
                    positions=[
                        PortfolioPosition(
                            instrument_type='currency',
                            current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                            quantity=Quotation(units=2, nano=0)
                            )])
            if account_id == '4':
                return PortfolioResponse(
                    positions=[
                        PortfolioPosition(
                            instrument_type='share',
                            instrument_uid='1',
                            current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                            quantity=Quotation(units=2, nano=0)
                            )])
            if account_id == '5':
                return PortfolioResponse(
                    positions=[
                        PortfolioPosition(
                            instrument_type='currency',
                            current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
                            quantity=Quotation(units=2, nano=0)
                            )])
            return PortfolioResponse(positions=[])

    class FakeUsers:
        """FakeUsers mock для работы с аккаунтами тинькофф инвестиций"""
        def get_accounts(self):
            """get_accounts mock для получения списка брокерских счетов"""
            return GetAccountsResponse(accounts=[Account(id='1',
                                                         type=AccountType.ACCOUNT_TYPE_TINKOFF,
                                                         name='account name',
                                                         status=AccountStatus.ACCOUNT_STATUS_OPEN),
                                                 Account(id='2',
                                                         type=AccountType.ACCOUNT_TYPE_TINKOFF,
                                                         name='account name',
                                                         status=AccountStatus.ACCOUNT_STATUS_OPEN)
                                                 ])

#pylint: disable=R0917
    class FakeOrders:
        """FakeOrders mock для работы с заявками тинькофф инвестиций"""
        def post_order(
            self,
            quantity,
            direction,
            account_id,
            order_type,
            instrument_id,
        ):
            """post_order mock для отправки заявки"""
            assert order_type == OrderType.ORDER_TYPE_BESTPRICE
            if account_id == '1':
                if direction == OrderDirection.ORDER_DIRECTION_BUY:
                    assert instrument_id == '2'
                    assert quantity == 50
                elif direction == OrderDirection.ORDER_DIRECTION_SELL:
                    assert instrument_id == '1'
                    assert quantity == 100
                else:
                    assert False
            elif account_id == '5':
                if direction == OrderDirection.ORDER_DIRECTION_BUY:
                    assert instrument_id == '1'
                    assert quantity == 2
                elif direction == OrderDirection.ORDER_DIRECTION_SELL:
                    assert instrument_id == '1'
                    assert quantity == 100
                else:
                    assert False

            return PostOrderResponse()

    class FakeOperationsStream:
        """FakeOperationsStream mock для работы с потоком операций тинькофф инвестиций"""
        count_operations: int

        def positions_stream(self, accounts):
            """positions_stream mock для получения операций по списку счетов"""
            self.count_operations = self.count_operations+1
            assert accounts == ['4', '5']
            # 2 запуска и кидаем исключение, что бы не уйти в бесконечный цикл
            if (self.count_operations) <= 2:
                return []
            if self.count_operations == 3:
                raise RequestError(
                    code='1', details='details', metadata='metadata')
            raise TestException()

        def __init__(self):
            self.count_operations = 0

    instruments: FakeInstruments
    operations:  FakeOperations
    operations_stream: FakeOperationsStream
    users:       FakeUsers
    orders:      FakeOrders

    def __init__(self):
        self.instruments = FakeClient.FakeInstruments()
        self.operations = FakeClient.FakeOperations()
        self.operations_stream = FakeClient.FakeOperationsStream()
        self.users = FakeClient.FakeUsers()
        self.orders = FakeClient.FakeOrders()
#pylint: enable=R0917

@pytest.fixture(name='client')
def client_tinvest():
    """client_tinvest - фикстура создаёт и возвращает mock клиента"""
    return FakeClient()

@pytest.fixture(name='auto_repeater')
def auto_repeater_fixture(client):
    """auto_repeater_fixture - фикстура создаёт и возвращает основной класс передав ему клиента"""
    return AutoRepeater(client)

def test_init(auto_repeater):
    """test_init"""
    assert auto_repeater.client is not None
    assert auto_repeater.debug is False
    assert auto_repeater.threshold == THRESHOLD
    assert auto_repeater.reserve == DST_MONEY_RESERVED


def test_set_debug(auto_repeater):
    """test_set_debug"""
    auto_repeater.set_debug(True)
    assert auto_repeater.debug is True
    auto_repeater.set_debug(False)
    assert auto_repeater.debug is False


def test_set_threshold(auto_repeater):
    """test_set_threshold"""
    auto_repeater.set_threshold(0.01)
    assert auto_repeater.threshold == 0.01


def test_set_reserve(auto_repeater):
    """test_set_reserve"""
    auto_repeater.set_reserve(0.05)
    assert auto_repeater.reserve == 0.05

# pylint: disable=R0917
@pytest.mark.parametrize(
    'instrument_type, price, quantity, uid, expected',
    [
        (
            "currency",
            MoneyValue(
                currency="USD",
                units=100,
                nano=0
            ),
            Quotation(
                units=1,
                nano=0
            ),
            "",
            "USD - 100.0"
        ),
        (
            "share",
            MoneyValue(
                currency="USD",
                units=100,
                nano=0
            ),
            Quotation(
                units=2,
                nano=0
            ),
            "1",
            "share1(SHR) - 2.0 - USD - 200.0"
        ),
    ]
)
def test_postiton_to_string(auto_repeater, instrument_type, price, quantity, uid, expected):
    """test_postiton_to_string"""
    position = PortfolioPosition(
        instrument_type=instrument_type,
        current_price=price,
        quantity=quantity,
        instrument_uid=uid,
    )
    result = auto_repeater.postiton_to_string(position)
    assert result == expected
# pylint: enable=R0917


def test_get_instrument(auto_repeater):
    """test_get_instrument"""
    instrument_id = "1"
    result = auto_repeater.get_instrument(instrument_id)
    assert result is not None


def test_get_instrument_fail(auto_repeater):
    """test_get_instrument_fail"""
    instrument_id = "none_id"
    with pytest.raises(GetInstrumentException):
        auto_repeater.get_instrument(instrument_id)


def test_print_portfolio_by_account(auto_repeater: AutoRepeater, capfd: pytest.CaptureFixture[str]):
    """test_print_portfolio_by_account"""
    account = Account(name='Account 1', id='1')
    auto_repeater.print_portfolio_by_account(account)

    out, err = capfd.readouterr()
    assert out == '''Account 1 (1)
------------
RUB - 2.4
total: 2.4
============
'''
    assert not err


def test_print_all_portfolio(auto_repeater, capfd):
    """test_print_all_portfolio"""
    auto_repeater.print_all_portfolio()

    out, err = capfd.readouterr()
    assert out == '''account name (1)
------------
RUB - 2.4
total: 2.4
============
account name (2)
------------
total: 0.0
============
'''
    assert not err


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

    assert result[2] == 0.995
    assert result[3] == 2.388


def test_calc_sell_positions(auto_repeater):
    """test_calc_sell_positions"""
    dst_positions = {'1': PortfolioPosition(
        instrument_type='share',
        instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
        quantity=Quotation(units=100, nano=0)
    ),
        '2': PortfolioPosition(
        instrument_type='share',
        instrument_uid='2',
        current_price=MoneyValue(currency='RUB', units=2, nano=200000000),
        quantity=Quotation(units=50, nano=0)
    )
    }

    target_positions = {
        '1': 50,
    }
    result = auto_repeater.calc_sell_positions(dst_positions, target_positions)
    assert result == [
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


def test_calc_buy_positions(auto_repeater):
    """test_calc_buy_positions"""
    src_positions = {'1': PortfolioPosition(
        instrument_type='share',
        instrument_uid='1',
        current_price=MoneyValue(currency='RUB', units=1, nano=200000000),
        quantity=Quotation(units=100, nano=0)
    ),
        '2': PortfolioPosition(
        instrument_type='share',
        instrument_uid='2',
        current_price=MoneyValue(currency='RUB', units=2, nano=200000000),
        quantity=Quotation(units=50, nano=0)
    )
    }

    dst_positions = {
        '2': PortfolioPosition(
            instrument_type='share',
            instrument_uid='2',
            current_price=MoneyValue(currency='RUB', units=2, nano=200000000),
            quantity=Quotation(units=50, nano=0)
        )
    }

    target_positions = {
        '1': 50,
        '2': 100
    }
    result = auto_repeater.calc_buy_positions(
        src_positions, dst_positions, target_positions)
    assert result == [
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


def test_post_orders(auto_repeater):
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


def test_sync_accounts(auto_repeater):
    """test_sync_accounts"""
    src_account_id = '4'
    dst_account_id = '5'
    auto_repeater.sync_accounts(src_account_id, dst_account_id)


def test_mainflow(auto_repeater):
    """test_mainflow"""
    src_account_id = '4'
    dst_account_id = '5'
    try:
        auto_repeater.mainflow(src_account_id, dst_account_id)
    except TestException:
        pass
