"""Shared units/nano conversion for the established SDK adapter paths."""
from decimal import Decimal, InvalidOperation


NANO_FACTOR = Decimal('1000000000')


def decimal_value(value: object, context: str) -> Decimal:
    """Convert an SDK units/nano value and identify malformed source fields."""
    if value is None:
        raise ValueError(f'{context} is missing')

    units = getattr(value, 'units', None)
    nano = getattr(value, 'nano', None)
    converted = []
    for field_name, part in (('units', units), ('nano', nano)):
        if part is None or isinstance(part, bool):
            raise ValueError(f'{context}.{field_name} is invalid')
        try:
            decimal_part = Decimal(part)
        except (TypeError, ValueError, InvalidOperation) as error:
            raise ValueError(f'{context}.{field_name} is invalid') from error
        if not decimal_part.is_finite():
            raise ValueError(f'{context}.{field_name} is not finite')
        converted.append(decimal_part)

    result = converted[0] + converted[1] / NANO_FACTOR
    if not result.is_finite():
        raise ValueError(f'{context} is not finite')
    return result
