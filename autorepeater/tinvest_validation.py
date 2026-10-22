"""Shared strict scalar validation at the SDK read boundaries."""


def sdk_text(value, field):
    """Identifiers/currencies cannot be blank or contain whitespace."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError(f'{field} must be a nonempty string without whitespace')
    return value


def sdk_bool(value, field):
    """SDK permission flags are booleans, never truthy placeholders."""
    if not isinstance(value, bool):
        raise ValueError(f'{field} must be bool')
    return value


def sdk_count(value, field):
    """Own limits/blockers count integral lots, excluding bool and negatives."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{field} must be a nonnegative integer')
    return value
