"""Local COMPOSITE schema and exact-name selection, independent of algorithms."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from autorepeater import reporting
from autorepeater.config_catalog import discover_candidates, select_candidate


@dataclass(frozen=True)
class CompositeComponent:
    """An opaque algorithm/source reference with its unnormalized budget share."""
    algoritm: str
    src: str
    weight: Decimal


@dataclass(frozen=True)
class CompositeConfig:
    """A named composition in declared preparation and calculation order."""
    name: str
    components: tuple[CompositeComponent, ...]


def _check_keys(data, allowed, context):
    unknown = data.keys() - allowed
    if unknown:
        raise ValueError(f'{context}: unknown fields: {", ".join(sorted(unknown))}')


def _name(value, label):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError(f'{label}: expected a nonempty name without whitespace')
    return value


def _load_component(data, position):
    context = f'components[{position}]'
    if not isinstance(data, dict):
        raise ValueError(f'{context}: expected an object')
    _check_keys(data, {'algoritm', 'src', 'weight'}, context)
    algoritm = _name(data.get('algoritm'), f'{context}.algoritm')
    src = data.get('src')
    if not isinstance(src, str) or not src.strip():
        raise ValueError(f'{context}.src: expected a nonempty string')
    raw = data.get('weight')
    if not isinstance(raw, str):
        raise ValueError(f'{context}.weight: expected a decimal string')
    try:
        weight = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f'{context}.weight: invalid decimal string') from error
    if not weight.is_finite() or not 0 < weight <= 1:
        raise ValueError(f'{context}.weight: non-finite or out-of-range value')
    return CompositeComponent(algoritm, src, weight)


def validate_composite_config(data):
    """Validate a parsed document without filesystem or registry access."""
    if not isinstance(data, dict):
        raise ValueError('composite config: expected an object')
    _check_keys(data, {'name', 'components'}, 'composite config')
    name = _name(data.get('name'), 'name')
    records = data.get('components')
    if not isinstance(records, list) or not records:
        raise ValueError('components: expected a nonempty array')
    components = tuple(_load_component(record, position) for position, record in enumerate(records))
    with localcontext() as context:
        context.prec = 28
        total = sum((component.weight for component in components), Decimal(0))
    if total > 1:
        raise ValueError('components: sum of weights must not exceed 1')
    return CompositeConfig(name, components)


def read_composite_document(path):
    """Parse one document without extracting names from invalid JSON."""
    with open(path, encoding='utf-8') as config_file:
        return json.load(config_file)


def load_composite_config(path):
    """Read and strictly validate a single composition."""
    return validate_composite_config(read_composite_document(path))


def _composite_paths():
    configured = os.environ.get('COMPOSITE_CONFIG_DIR')
    if configured is not None and not configured.strip():
        raise ValueError('composite config path must not be empty')
    root = (Path(configured) if configured is not None else
            Path(__file__).parent / 'configs/composite')
    try:
        if not root.is_dir():
            raise ValueError(f'composite config directory does not exist: {root}')
        paths = sorted(path for path in root.glob('*.json') if not path.name.startswith('.'))
    except OSError as error:
        raise ValueError(f'composite config directory {root}: {error}') from error
    if not paths:
        raise ValueError(f'no composite configs found: {root}')
    return paths


def _discover_candidates():
    return discover_candidates(
        _composite_paths(), read_composite_document, validate_composite_config)


def select_composite_config(name):
    """Select an exact name, warning about foreign errors without substituting them."""
    return select_candidate(_discover_candidates(), name, reporting.print_config_warning)
