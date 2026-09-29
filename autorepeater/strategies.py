"""Pure source validation and explicit strategy registration."""
from autorepeater.account_strategy import AccountStrategy


NAMED_STRATEGIES = {}


class UnsupportedSourceError(ValueError):
    """The source is missing or has no registered strategy."""


def validate_src(src):
    """Accept an ASCII account number or an exact registered name without constructing it."""
    if src is None or (isinstance(src, str) and not src.strip()):
        raise UnsupportedSourceError('src is required')
    if isinstance(src, str):
        if src.isascii() and src.isdecimal():
            return
        if src in NAMED_STRATEGIES:
            return
    raise UnsupportedSourceError(f'unsupported src: {src}')


def create_strategy(src):
    """Construct a validated source without opening an SDK client."""
    validate_src(src)
    if src.isascii() and src.isdecimal():
        return AccountStrategy(src)
    return NAMED_STRATEGIES[src](src)
