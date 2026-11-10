"""The shared SDK converter preserves the established strategy numeric contract."""
from decimal import Decimal, InvalidOperation, Overflow, ROUND_DOWN, localcontext
from types import SimpleNamespace

import pytest
from t_tech.invest import Quotation

from autorepeater.cash_facts import MoneyAmount
from autorepeater.tinvest_values import decimal_value


@pytest.mark.parametrize('units,nano,expected', [
    (0, 0, '0'), (0, 1, '0.000000001'), (0, 999999999, '0.999999999'),
    (1, 1, '1.000000001'), (-1, -1, '-1.000000001'),
    (0, -999999999, '-0.999999999'),
    (9223372036854775807, 999999999, '9223372036854775807.999999999'),
    (-9223372036854775808, -999999999, '-9223372036854775808.999999999'),
])
def test_decimal_value_preserves_exact_sdk_values(units, nano, expected):
    """Signed quotations retain all nano digits, including int64 endpoints."""
    assert decimal_value(Quotation(units=units, nano=nano), 'quote') == Decimal(expected)


@pytest.mark.parametrize('units,nano,expected', [
    (1, -1, '0.999999999'), (-1, 1, '-0.999999999'),
    (0, 1000000000, '1'), (0, -1000000000, '-1'),
    ('2', '500000000', '2.5'), (Decimal('1.5'), Decimal('0.5'), '1.5000000005'),
    (0.5, 0, '0.5'),
])
def test_legacy_acceptance_does_not_weaken_strict_money(units, nano, expected):
    """Compatibility applies to this converter, not strict monetary paths."""
    assert decimal_value(SimpleNamespace(units=units, nano=nano), 'quote') == Decimal(expected)
    with pytest.raises(ValueError, match='^invalid money units/nano$'):
        MoneyAmount.from_units_nano('rub', units, nano)


@pytest.mark.parametrize('value,message', [
    (None, 'quote is missing'),
    (True, 'quote.units is invalid'),
    (SimpleNamespace(nano=0), 'quote.units is invalid'),
    (SimpleNamespace(units=0), 'quote.nano is invalid'),
    (SimpleNamespace(units=None, nano=0), 'quote.units is invalid'),
    (SimpleNamespace(units=0, nano=None), 'quote.nano is invalid'),
    (SimpleNamespace(units=True, nano=0), 'quote.units is invalid'),
    (SimpleNamespace(units=0, nano=False), 'quote.nano is invalid'),
])
def test_missing_and_bool_parts_keep_message_and_no_cause(value, message):
    """Pre-conversion failures identify the same field without inventing a cause."""
    with pytest.raises(ValueError) as caught:
        decimal_value(value, 'quote')
    assert str(caught.value) == message
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('field', ['units', 'nano'])
@pytest.mark.parametrize('part,cause_type', [('invalid', InvalidOperation), (object(), TypeError)])
def test_malformed_parts_keep_conversion_cause(field, part, cause_type):
    """Decimal conversion remains the direct cause of a contextual ValueError."""
    value = SimpleNamespace(units=0, nano=0)
    setattr(value, field, part)
    with pytest.raises(ValueError) as caught:
        decimal_value(value, 'quote')
    assert str(caught.value) == f'quote.{field} is invalid'
    assert isinstance(caught.value.__cause__, cause_type)
    assert caught.value.__cause__.__cause__ is None


@pytest.mark.parametrize('field', ['units', 'nano'])
@pytest.mark.parametrize('part', [
    Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity'),
])
def test_nonfinite_parts_keep_message_and_no_cause(field, part):
    """Nonfinite components fail before arithmetic without a conversion cause."""
    value = SimpleNamespace(units=0, nano=0)
    setattr(value, field, part)
    with pytest.raises(ValueError) as caught:
        decimal_value(value, 'quote')
    assert str(caught.value) == f'quote.{field} is not finite'
    assert caught.value.__cause__ is None


def test_decimal_context_rounding_is_preserved():
    """The converter keeps its existing caller-context arithmetic, not exact_sum."""
    value = Quotation(units=12345, nano=678901234)
    with localcontext() as context:
        context.prec = 6
        assert decimal_value(value, 'quote') == Decimal('12345.7')
        context.rounding = ROUND_DOWN
        assert decimal_value(value, 'quote') == Decimal('12345.6')


@pytest.mark.parametrize('trap', [True, False])
def test_arithmetic_overflow_keeps_trapping_behavior(trap):
    """Trapped Overflow propagates; a nontrapping infinite result stays contextual."""
    value = SimpleNamespace(units=Decimal('9E+3'), nano=Decimal('9E+12'))
    with localcontext() as context:
        context.Emax = 3
        context.traps[Overflow] = trap
        with pytest.raises(Overflow if trap else ValueError) as caught:
            decimal_value(value, 'quote')
    if not trap:
        assert str(caught.value) == 'quote is not finite'
    assert caught.value.__cause__ is None
