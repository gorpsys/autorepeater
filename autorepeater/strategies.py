"""Select account, configured index, or explicitly registered strategies."""
from autorepeater.account_strategy import AccountStrategy
from autorepeater.index_config import load_index_configs
from autorepeater.index_strategy import IndexStrategy


NAMED_STRATEGIES = {}


class UnsupportedSourceError(ValueError):
    """The source is missing or has no registered strategy."""


def _resolve_src(src):
    """Resolve a factory and its input without constructing it or accessing the SDK."""
    if src is None or (isinstance(src, str) and not src.strip()):
        raise UnsupportedSourceError('src is required')
    if isinstance(src, str):
        if src.isascii() and src.isdecimal():
            return AccountStrategy, src
        configs = load_index_configs()
        collisions = configs.keys() & NAMED_STRATEGIES.keys()
        if collisions:
            raise ValueError(f'duplicate strategy name: {sorted(collisions)[0]}')
        if src in configs:
            return IndexStrategy, configs[src]
        if src in NAMED_STRATEGIES:
            return NAMED_STRATEGIES[src], src
    raise UnsupportedSourceError(f'unsupported src: {src}')


def validate_src(src):
    """Validate an account or exact configured/registered name without constructing it."""
    _resolve_src(src)


def create_strategy(src):
    """Construct a validated source without opening an SDK client."""
    factory, argument = _resolve_src(src)
    return factory(argument)
