"""Local ACCOUNT settings, independent of index catalogs and the SDK."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class AccountConfig:
    """Reserve as a fraction of destination value."""
    reserve: Decimal


def validate_account_config(data):
    """Validate a parsed document without accessing files or services."""
    if not isinstance(data, dict):
        raise ValueError('account config: expected an object')
    raw = data.get('reserve')
    if not isinstance(raw, str):
        raise ValueError('account config.reserve: expected a decimal string')
    try:
        reserve = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError('account config.reserve: invalid decimal string') from error
    if not reserve.is_finite() or not 0 <= reserve < 1:
        raise ValueError('account config.reserve: non-finite or out-of-range value')
    return AccountConfig(reserve)


def load_account_config():
    """Read the explicit path or the bundled settings once, with no fallback."""
    configured = os.environ.get('ACCOUNT_CONFIG_PATH')
    if configured is not None and not configured.strip():
        raise ValueError('account config path must not be empty')
    path = (Path(configured) if configured is not None else
            Path(__file__).parent / 'configs/account/account.json')
    try:
        with open(path, encoding='utf-8') as config_file:
            return validate_account_config(json.load(config_file))
    except (OSError, ValueError) as error:
        raise ValueError(f'account config {path}: {error}') from error
