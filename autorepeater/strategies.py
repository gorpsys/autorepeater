"""Explicit algorithm registration and one-launch source preparation."""
from autorepeater.account_strategy import AccountStrategy, prepare_account_source
from autorepeater.index_strategy import IndexStrategy, prepare_index_source
from autorepeater.strategy_contract import AlgorithmDefinition, PreparedStrategy
from autorepeater.strategy_contract import UnsupportedSourceError, validate_strategy


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


def prepare_strategy(algoritm, src):
    """Delegate preparation once; do not construct a strategy or SDK client."""
    if not isinstance(algoritm, str) or not algoritm or algoritm.isspace():
        raise ValueError('algoritm is required')
    if algoritm not in ALGORITHMS:
        raise ValueError(f'unsupported algoritm: {algoritm}')
    if src is None or (isinstance(src, str) and not src.strip()):
        raise UnsupportedSourceError('src is required')
    definition = ALGORITHMS[algoritm]
    return PreparedStrategy(definition.create, definition.prepare_source(src), str(src))


def create_strategy(prepared):
    """Use the saved factory and data, then validate before opening Client."""
    if not isinstance(prepared, PreparedStrategy):
        raise TypeError('create_strategy requires PreparedStrategy')
    return validate_strategy(prepared.create(prepared.prepared_source))


register_algorithm('ACCOUNT', AlgorithmDefinition(prepare_account_source, AccountStrategy))
register_algorithm('INDEX', AlgorithmDefinition(prepare_index_source, IndexStrategy))
