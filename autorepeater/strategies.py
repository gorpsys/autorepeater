"""Explicit algorithm registration and one-launch source preparation."""
from autorepeater.account_strategy import AccountStrategy, prepare_account_source
from autorepeater.composite_strategy import CompositeStrategy, prepare_composite_source
from autorepeater.index_strategy import IndexStrategy, prepare_index_source
from autorepeater.strategy_contract import AlgorithmDefinition, PreparedStrategy
from autorepeater.strategy_contract import UnsupportedSourceError
from autorepeater.strategy_contract import create_strategy as create_prepared_strategy


ALGORITHMS = {}


def register_algorithm(name, definition):
    """Register an exact algorithm name without consulting any source catalog."""
    if not isinstance(name, str) or not name or any(char.isspace() for char in name):
        raise ValueError('algoritm name must be nonempty and without whitespace')
    if name in ALGORITHMS:
        raise ValueError(f'duplicate algoritm registration: {name}')
    if (not isinstance(definition, AlgorithmDefinition)
            or not callable(definition.prepare_source) or not callable(definition.create)):
        raise TypeError('algorithm definition requires callable prepare_source and create')
    ALGORITHMS[name] = definition


class _PreparationContext:  # pylint: disable=too-few-public-methods
    """Track active source pairs for one launch; completed siblings can be reused."""

    def __init__(self):
        self.path = []

    def prepare(self, algoritm, src):
        """Save one definition's prepared data, rejecting cycles in the active path."""
        if not isinstance(algoritm, str) or not algoritm or algoritm.isspace():
            raise ValueError('algoritm is required')
        if algoritm not in ALGORITHMS:
            raise ValueError(f'unsupported algoritm: {algoritm}')
        if src is None or (isinstance(src, str) and not src.strip()):
            raise UnsupportedSourceError('src is required')
        pair = (algoritm, src)
        if pair in self.path:
            chain = ' -> '.join(f'{algorithm}/{source}' for algorithm, source in self.path + [pair])
            raise ValueError(f'strategy preparation cycle: {chain}')
        definition = ALGORITHMS[algoritm]
        self.path.append(pair)
        try:
            return PreparedStrategy(
                definition.create, definition.prepare_source(src, self), str(src))
        finally:
            self.path.pop()


def prepare_strategy(algoritm, src):
    """Prepare a fresh launch tree without constructing strategies or an SDK client."""
    return _PreparationContext().prepare(algoritm, src)


def create_strategy(prepared):
    """Use the saved factory and data, then validate before opening Client."""
    return create_prepared_strategy(prepared)


register_algorithm('ACCOUNT', AlgorithmDefinition(prepare_account_source, AccountStrategy))
register_algorithm('INDEX', AlgorithmDefinition(prepare_index_source, IndexStrategy))
register_algorithm('COMPOSITE', AlgorithmDefinition(prepare_composite_source, CompositeStrategy))
