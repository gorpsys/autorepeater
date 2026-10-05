"""Helpers for money, quantities, and portfolio position formatting."""
from collections.abc import Mapping
from decimal import Decimal
from typing import TypeVar

KeyT = TypeVar('KeyT')


def format_decimal(value):
    """Format Decimal to string with proper formatting
    Args:
        value: Decimal value to format
    Returns:
        str: Formatted string representation of Decimal
    """
    # Форматируем Decimal в строку, избегая научной нотации
    formatted = format(value, 'f')  # Используем 'f' для десятичного формата
    # Проверяем, есть ли точка в числе
    if '.' in formatted:
        # Разделяем на целую и дробную части
        integer_part, fractional_part = formatted.split('.')
        # Убираем лишние нули в дробной части
        fractional_part = fractional_part.rstrip('0')
        # Если дробная часть пустая, добавляем .0
        if not fractional_part:
            return integer_part + '.0'
        return integer_part + '.' + fractional_part
    # Если число целое, добавляем .0
    return formatted + '.0'


def format_decimal_map(values: Mapping[KeyT, Decimal]) -> str:
    """Format diagnostic money/quantity maps without Decimal repr or exponents."""
    return '{' + ', '.join(f'{key}: {format_decimal(value)}'
                          for key, value in values.items()) + '}'


def money_to_string(money):
    """convert money to human-readable string"""
    result = money.currency
    value = (Decimal(money.units) +
             Decimal(money.nano) / Decimal('1000000000'))
    formatted = format_decimal(value)
    result += ' - ' + formatted
    return result


def blocked_to_string(blocked):
    """convert blocked money to human-readable string"""
    result = 'blocked ' + money_to_string(blocked)
    return result


def no_money_to_string(share):
    """convert no money instrument to human-readable string"""
    result = share.name + '(' + share.ticker + ')'
    return result


def currency_to_decimal(position):
    """convert position full price value to Decimal"""
    price = (Decimal(position.current_price.units) +
             Decimal(position.current_price.nano) / Decimal('1000000000'))
    quantity = (Decimal(position.quantity.units) +
                Decimal(position.quantity.nano) / Decimal('1000000000'))
    # Округляем результат до 9 знаков после запятой (максимальная точность
    # nano)
    return (price * quantity).quantize(Decimal('0.000000001'))


def currency_to_decimal_price(position):
    """convert position price value to Decimal"""
    return (Decimal(position.current_price.units) +
            Decimal(position.current_price.nano) / Decimal('1000000000'))


def currency_to_string(position):
    """convert position price value to human-readable string"""
    result = position.current_price.currency
    value = currency_to_decimal(position)

    formatted = format_decimal(value)

    result += ' - ' + formatted
    return result


def get_quantity_position(position):
    """get quantity from position as Decimal"""
    return (Decimal(position.quantity.units) +
            Decimal(position.quantity.nano) / Decimal('1000000000'))
