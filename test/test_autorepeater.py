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
