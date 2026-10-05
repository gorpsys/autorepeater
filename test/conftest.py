"""Keep local strategy configuration paths independent of the caller's shell."""
import json
from collections.abc import Callable

import pytest


@pytest.fixture(autouse=True)
def immediate_settlement_timeout(monkeypatch):
    """Ordinary fake snapshots do not settle with time; polling tests use a fake clock."""
    monkeypatch.setattr('autorepeater.execution.SETTLEMENT_TIMEOUT', 0)


@pytest.fixture
def guarded_import_script() -> Callable[[tuple[str, ...], str], str]:
    """Build a fresh-interpreter probe with shared SDK/schema and I/O guards."""
    def build(forbidden: tuple[str, ...], body: str) -> str:
        return f'''
import builtins
import pathlib
import socket
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith({forbidden!r}):
        raise AssertionError('forbidden dependency: ' + name)
    return original_import(name, *args, **kwargs)
def blocked_io(*args, **kwargs):
    raise AssertionError('I/O forbidden')
builtins.__import__ = guarded_import
builtins.open = blocked_io
pathlib.Path.open = blocked_io
pathlib.Path.glob = blocked_io
pathlib.Path.is_dir = blocked_io
socket.socket = blocked_io
{body}
'''
    return build


@pytest.fixture(autouse=True, name='strategy_path_environment')
def fixture_strategy_path_environment(monkeypatch):
    """Tests opt into external config paths explicitly."""
    for name in ('ACCOUNT_CONFIG_PATH', 'ACCOUNT_CONFIG_DIR', 'COMPOSITE_CONFIG_DIR',
                 'INDEX_CONFIG_DIR', 'IMOEX_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def named_accounts(tmp_path, monkeypatch, strategy_path_environment):  # pylint: disable=unused-argument
    """Explicit named sources for the legacy execution and entrypoint scenarios."""
    catalog = tmp_path / 'named-accounts'
    catalog.mkdir()
    for name in ('4', '0004', '0', '00123', '123456789012345678901234567890'):
        (catalog / f'{name}.json').write_text(json.dumps({
            'name': name, 'source_account_id': name, 'reserve': '0.01',
            'allocation_drift_limit': '0.0092'}), encoding='utf-8')
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(catalog))
