"""tests"""
import pytest

from tinkoff.invest import MoneyValue
from tinkoff.invest import Instrument
from tinkoff.invest import PortfolioPosition
from tinkoff.invest import Quotation

from app.autorepeater import money_to_string
from app.autorepeater import no_money_to_string
from app.autorepeater import blocked_to_string
from app.autorepeater import currency_to_float

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
    """money to string"""
    position = PortfolioPosition(
        current_price=money,
        quantity=Quotation(
            units=quantity_units,
            nano=quantity_nano,
        ),
    )
    result = currency_to_float(position)

    assert result == expected
