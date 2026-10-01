"""Local COMPOSITE schema and exact-name selection, independent of algorithms."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from autorepeater import reporting
from autorepeater.strategy_contract import UnsupportedSourceError


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


@dataclass
class _Candidate:
    path: Path
    name: str | None
    config: CompositeConfig | None
    error: ValueError | None


def _discover_candidates():
    candidates = []
    for path in _composite_paths():
        name = None
        config = None
        error = None
        try:
            document = read_composite_document(path)
            if isinstance(document, dict) and isinstance(document.get('name'), str):
                name = document['name']
            config = validate_composite_config(document)
        except (OSError, ValueError) as cause:
            label = f'{path} ({name})' if name is not None else str(path)
            error = ValueError(f'{label}: {cause}')
        candidates.append(_Candidate(path, name, config, error))
    return candidates


def _duplicate_message(name, candidates):
    paths = ', '.join(str(candidate.path) for candidate in candidates)
    return f'duplicate strategy name: {name} ({paths})'


def select_composite_config(name):
    """Select an exact name, warning about foreign errors without substituting them."""
    candidates = _discover_candidates()
    catalog = {}
    for candidate in candidates:
        if candidate.name is not None:
            catalog.setdefault(candidate.name, []).append(candidate)
        if candidate.error is not None and candidate.name != name:
            reporting.print_config_warning(str(candidate.error))
    for candidate_name, matches in catalog.items():
        if candidate_name != name and len(matches) > 1:
            reporting.print_config_warning(_duplicate_message(candidate_name, matches))
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
