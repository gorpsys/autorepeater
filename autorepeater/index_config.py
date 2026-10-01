"""Local index composition and validation, without SDK access."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autorepeater import reporting
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


@dataclass
class _Candidate:
    path: Path
    name: str | None
    config: IndexConfig | None
    error: ValueError | None


def _discover_candidates():
    candidates = []
    for path in _index_paths():
        name = None
        config = None
        error = None
        try:
            document = read_index_document(path)
            if isinstance(document, dict) and isinstance(document.get('name'), str):
                name = document['name']
            config = validate_index_config(document)
        except (OSError, ValueError) as cause:
            label = f'{path} ({name})' if name is not None else str(path)
            error = ValueError(f'{label}: {cause}')
        candidates.append(_Candidate(path, name, config, error))
    return candidates


def _catalog(candidates):
    catalog = {}
    for candidate in candidates:
        if candidate.name is not None:
            catalog.setdefault(candidate.name, []).append(candidate)
    return catalog


def _duplicate_message(name, candidates):
    paths = ', '.join(str(candidate.path) for candidate in candidates)
    return f'duplicate strategy name: {name} ({paths})'


def _warn_foreign_candidates(candidates, catalog, selected):
    for candidate in candidates:
        if candidate.error is not None and candidate.name != selected:
            reporting.print_index_config_warning(str(candidate.error))
    for name, matches in catalog.items():
        if name != selected and len(matches) > 1:
            reporting.print_index_config_warning(_duplicate_message(name, matches))


def select_index_config(name=None):
    """Select one exact JSON name; isolate foreign errors and read each file once."""
    candidates = _discover_candidates()
    catalog = _catalog(candidates)
    if name is None:
        if len(candidates) != 1 or candidates[0].error is not None:
            raise UnsupportedSourceError('--src must name a configured index strategy')
        return candidates[0].config
    _warn_foreign_candidates(candidates, catalog, name)
    matches = catalog.get(name, []) if isinstance(name, str) else []
    if not matches:
        problems = ', '.join(str(item.path) for item in candidates if item.error is not None)
        detail = f'; problematic files: {problems}' if problems else ''
        raise UnsupportedSourceError(f'unsupported src: {name}{detail}')
    if len(matches) != 1:
        raise ValueError(_duplicate_message(name, matches))
    if matches[0].error is not None:
        raise matches[0].error
    return matches[0].config


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
