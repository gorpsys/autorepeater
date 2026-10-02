"""Local index composition and validation, without SDK access."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autorepeater import reporting
from autorepeater.config_catalog import discover_candidates, select_candidate
from autorepeater.strategy_contract import UnsupportedSourceError

@dataclass
class IndexInstrument:
    """Reference data; weights are percentages, coefficients are fractions."""
    ticker: str
    effective_quantity: Decimal
    free_float: Decimal
    weight_limit: Decimal
    reference_price: Decimal
    reference_weight: Decimal
    reference_index_capitalization: Decimal


@dataclass
class IndexConfig:
    """Composition, error limit for ideal lots < 1, and minimum target value."""
    name: str
    max_lot_weight_error: Decimal
    instruments: list[IndexInstrument]
    reserve: Decimal
    min_position_value: Decimal = Decimal(0)


_INSTRUMENT_RANGES = {
    'effective_quantity': lambda value: value >= 0,
    'free_float': lambda value: 0 < value <= 1,
    'weight_limit': lambda value: 0 < value <= 1,
    'reference_price': lambda value: value > 0,
    'reference_weight': lambda value: 0 <= value <= 100,
    'reference_index_capitalization': lambda value: value > 0,
}


def _decimal_field(data, field, context, valid_range):
    raw = data.get(field)
    label = f'{context}.{field}'
    if not isinstance(raw, str):
        raise ValueError(f'{label}: expected a decimal string')
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f'{label}: invalid decimal string') from error
    if not value.is_finite() or not valid_range(value):
        raise ValueError(f'{label}: non-finite or out-of-range value')
    return value


def _load_instrument(data, position):
    context = f'instruments[{position}]'
    if not isinstance(data, dict):
        raise ValueError(f'{context}: expected an object')
    ticker = data.get('ticker')
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError(f'{context}.ticker: expected a nonempty string')
    values = {
        field: _decimal_field(data, field, f'{context} ({ticker})', valid_range)
        for field, valid_range in _INSTRUMENT_RANGES.items()
    }
    return IndexInstrument(ticker=ticker, **values)


def validate_index_config(data):
    """Validate an already parsed document without filesystem access."""
    if not isinstance(data, dict):
        raise ValueError('index config: expected an object')
    name = data.get('name')
    if not isinstance(name, str) or not name or any(char.isspace() for char in name):
        raise ValueError('name: expected a nonempty name without whitespace')
    max_error = _decimal_field(
        data, 'max_lot_weight_error', 'index config', lambda value: 0 <= value < 1)
    minimum = _decimal_field(
        {'min_position_value': data.get('min_position_value', '0')},
        'min_position_value', 'index config', lambda value: value >= 0)
    reserve = _decimal_field(
        data, 'reserve', 'index config', lambda value: 0 <= value < 1)
    records = data.get('instruments')
    if not isinstance(records, list) or not records:
        raise ValueError('instruments: expected a nonempty array')
    instruments = []
    tickers = set()
    for position, record in enumerate(records):
        instrument = _load_instrument(record, position)
        if instrument.ticker in tickers:
            raise ValueError(f'duplicate ticker: {instrument.ticker}')
        tickers.add(instrument.ticker)
        instruments.append(instrument)
    return IndexConfig(data['name'], max_error, instruments, reserve, minimum)


def read_index_document(path):
    """Parse one JSON document without applying schema validation."""
    with open(path, encoding='utf-8') as config_file:
        return json.load(config_file)


def load_index_config(path):
    """Read and strictly validate one named index document."""
    return validate_index_config(read_index_document(path))


def _index_paths():
    single_path = os.environ.get('IMOEX_CONFIG_PATH')
    directory = os.environ.get('INDEX_CONFIG_DIR')
    if single_path is not None and directory is not None:
        raise ValueError('use either INDEX_CONFIG_DIR or IMOEX_CONFIG_PATH, not both')
    if any(value is not None and not value.strip() for value in (single_path, directory)):
        raise ValueError('index config path must not be empty')
    if single_path is not None:
        paths = [Path(single_path)]
    else:
        root = Path(directory) if directory is not None else Path(__file__).parent / 'configs'
        if not root.is_dir():
            raise ValueError(f'index config directory does not exist: {root}')
        paths = sorted(path for path in root.glob('*.json') if not path.name.startswith('.'))
        if not paths:
            raise ValueError(f'no index configs found: {root}')
    return paths


def _discover_candidates():
    return discover_candidates(_index_paths(), read_index_document, validate_index_config)


def select_index_config(name=None):
    """Select one exact JSON name; isolate foreign errors and read each file once."""
    candidates = _discover_candidates()
    if name is None:
        if len(candidates) != 1 or candidates[0].error is not None:
            raise UnsupportedSourceError('--src must name a configured index strategy')
        return candidates[0].config
    return select_candidate(candidates, name, reporting.print_index_config_warning)


def load_index_configs():
    """Strictly validate the complete set, reusing the parser and pure validator."""
    configs = {}
    paths = _index_paths()
    for path in paths:
        config = load_index_config(path)
        if config.name in configs:
            raise ValueError(f'duplicate strategy name: {config.name}')
        configs[config.name] = config
    return configs
