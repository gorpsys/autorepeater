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
from tinkoff.invest import InstrumentShort

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
                Instrument(name='FinEx Акции американских компаний', ticker='IE00BD3QHZ91'),
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
    class FakeInstruments:
        def find_instrument(self, query):
            names={
                "1": "share1",
                "2": "etf2"
            }
            tickers={
                "1": "SHR",
                "2": "ETF"
            }
            try:
                return FindInstrumentResponse(
                    instruments=[InstrumentShort(name=names[query], ticker=tickers[query])]
                )
            except:
                return FindInstrumentResponse(
                    instruments=[]
                )

    instruments: FakeInstruments

    def __init__(self):
        self.instruments = FakeClient.FakeInstruments()

@pytest.fixture
def client():
    return FakeClient()

@pytest.fixture
def auto_repeater(client):
    return AutoRepeater(client)

def test_init(auto_repeater):
    assert auto_repeater.client is not None
    assert auto_repeater.debug is False
    assert auto_repeater.threshold == THRESHOLD
    assert auto_repeater.reserve == DST_MONEY_RESERVED


def test_set_debug(auto_repeater):
    auto_repeater.set_debug(True)
    assert auto_repeater.debug is True
    auto_repeater.set_debug(False)
    assert auto_repeater.debug is False

def test_set_threshold(auto_repeater):
    auto_repeater.set_threshold(0.01)
    assert auto_repeater.threshold == 0.01

def test_set_reserve(auto_repeater):
    auto_repeater.set_reserve(0.05)
    assert auto_repeater.reserve == 0.05

@pytest.mark.parametrize(
        'type, price, quantity, uid, expected',
        [
            (
                "currency",
                MoneyValue(
                    currency= "USD",
                    units= 100,
                    nano= 0
                ),
                Quotation(
                    units= 1,
                    nano= 0
                ),
                "",
                "USD - 100.0",
            ),
            (
                "share",
                MoneyValue(
                    currency= "USD",
                    units= 100,
                    nano= 0
                ),
                Quotation(
                    units= 2,
                    nano= 0
                ),
                "1",
                "share1(SHR) - 2.0 - USD - 200.0",
            ),
        ]
)
def test_postiton_to_string(auto_repeater, type, price, quantity, uid, expected):
    position = PortfolioPosition(
        instrument_type = type,
        current_price = price,
        quantity = quantity,
        instrument_uid = uid,
    )
    result = auto_repeater.postiton_to_string(position)
    assert result == expected
"""
def test_get_instrument(auto_repeater):
    instrument_id = "INSTRUMENT_ID"
    result = auto_repeater.get_instrument(instrument_id)
    assert result is not None

def test_print_portfolio_by_account(auto_repeater):
    account = {
        "name": "Account 1",
        "id": 1
    }
    auto_repeater.print_portfolio_by_account(account)
    assert "Account 1 (1)" in auto_repeater.client.operations.get_portfolio(account_id=1).accounts[0].name

def test_print_all_portfolio(auto_repeater):
    response = {
        "accounts": [
            {
                "name": "Account 1",
                "id": 1
            },
            {
                "name": "Account 2",
                "id": 2
            }
        ]
    }
    auto_repeater.print_all_portfolio()
    assert "Account 1 (1)" in auto_repeater.client.operations.get_portfolio(account_id=1).accounts[0].name
    assert "Account 2 (2)" in auto_repeater.client.operations.get_portfolio(account_id=2).accounts[0].name

def test_calc_ratio(auto_repeater):
    src_account_id = 1
    dst_account_id = 2
    result = auto_repeater.calc_ratio(src_account_id, dst_account_id)
    assert result[0] is not None
    assert result[1] is not None
    assert result[2] is not None
    assert result[3] is not None

def test_calc_sell_positions(auto_repeater):
    dst_positions = {
        "instrument_id": "INSTRUMENT_ID",
        "quantity": 100
    }
    target_positions = {
        "instrument_id": "INSTRUMENT_ID",
        "quantity": 50
    }
    result = auto_repeater.calc_sell_positions(dst_positions, target_positions)
    assert result is not None

def test_calc_buy_positions(auto_repeater):
 src_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 100
 }
 dst_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 50
 }
 target_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 150
 }
 result = auto_repeater.calc_buy_positions(src_positions, dst_positions, target_positions)
 assert result is not None

def test_post_orders(auto_repeater):
 dst_account_id = 1
 orders_params_sell = [
 {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 100,
 "direction": "SELL",
 "order_type": "BESTPRICE"
 }
 ]
 orders_params_buy = [
 {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 50,
 "direction": "BUY",
 "order_type": "BESTPRICE"
 }
 ]
 auto_repeater.post_orders(dst_account_id, orders_params_sell, orders_params_buy)
 assert orders_params_sell0 is not None
 assert orders_params_sell1 is not None
 assert orders_params_sell2 is not None
 assert orders_params_sell3 is not None
 assert orders_params_buy0 is not None
 assert orders_params_buy1 is not None
 assert orders_params_buy2 is not None
 assert orders_params_buy3 is not None

def test_sync_accounts(auto_repeater):
 src_account_id = 1
 dst_account_id = 2
 src_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 100
 }
 dst_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 50
 }
 target_positions = {
 "instrument_id": "INSTRUMENT_ID",
 "quantity": 75
 }
 auto_repeater.sync_accounts(src_account_id, dst_account_id)
 assert auto_repeater.client.operations_stream.positions_stream(accounts=[src_account_id, dst_account_id]) is not None

def test_mainflow(auto_repeater):
 src = 1
 dst = 2
 auto_repeater.mainflow(src, dst)
 assert auto_repeater.client.operations_stream.positions_stream(accounts=[src, dst]) is not None
"""