"""Local ACCOUNT settings, independent of index catalogs and the SDK."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autorepeater import reporting
from autorepeater.config_catalog import discover_candidates, select_candidate


@dataclass(frozen=True)
class AccountConfig:
    """Named source and leaf settings fixed before SDK access."""
    name: str
    source_account_id: str
    reserve: Decimal
    allocation_drift_limit: Decimal


def _fraction(data: dict[str, object], field: str) -> Decimal:
    raw = data.get(field)
    label = f'account config.{field}'
    if not isinstance(raw, str):
        raise ValueError(f'{label}: expected a decimal string')
    try:
        value = Decimal(raw)
        if not value.is_finite() or not 0 <= value < 1:
            raise ValueError(f'{label}: non-finite or out-of-range value')
    except InvalidOperation as error:
        raise ValueError(f'{label}: invalid decimal string') from error
    return value


def validate_account_config(data: object) -> AccountConfig:
    """Validate a parsed document without accessing files or services."""
    if not isinstance(data, dict):
        raise ValueError('account config: expected an object')
    name = data.get('name')
    if not isinstance(name, str) or not name or any(char.isspace() for char in name):
        raise ValueError('account config.name: expected a nonempty name without whitespace')
    source = data.get('source_account_id')
    if not isinstance(source, str) or not source or any(char.isspace() for char in source):
        raise ValueError(
            'account config.source_account_id: expected a nonempty string without whitespace')
    return AccountConfig(name, source, _fraction(data, 'reserve'),
                         _fraction(data, 'allocation_drift_limit'))


def _account_paths() -> list[Path]:
    """Discover only this algorithm's immediate, nonhidden documents."""
    configured = os.environ.get('ACCOUNT_CONFIG_PATH')
    directory = os.environ.get('ACCOUNT_CONFIG_DIR')
    if configured is not None and directory is not None:
        raise ValueError('use either ACCOUNT_CONFIG_DIR or ACCOUNT_CONFIG_PATH, not both')
    if any(value is not None and not value.strip() for value in (configured, directory)):
        raise ValueError('account config path must not be empty')
    if configured is not None:
        return [Path(configured)]
    root = Path(directory) if directory is not None else Path(__file__).parent / 'configs/account'
    try:
        if not root.is_dir():
            raise ValueError(f'account config directory does not exist: {root}')
        paths = sorted(path for path in root.glob('*.json') if not path.name.startswith('.'))
    except OSError as error:
        raise ValueError(f'account config directory {root}: {error}') from error
    if not paths:
        raise ValueError(f'no account configs found: {root}')
    return paths


def read_account_document(path: str | Path) -> object:
    """Parse one JSON document, without guessing names from malformed text."""
    with open(path, encoding='utf-8') as config_file:
        return json.load(config_file)


def load_account_config(path: str | Path) -> AccountConfig:
    """Read and validate one explicit document."""
    return validate_account_config(read_account_document(path))


def select_account_config(name: str) -> AccountConfig:
    """Select an exact name and isolate foreign errors through the shared catalog."""
    candidates = discover_candidates(
        _account_paths(), read_account_document, validate_account_config)
    return select_candidate(candidates, name, reporting.print_config_warning)
