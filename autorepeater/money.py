"""Pure Decimal formatting helpers."""
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
