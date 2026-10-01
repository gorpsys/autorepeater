"""Keep local strategy configuration paths independent of the caller's shell."""
import pytest


@pytest.fixture
def guarded_import_script():
    """Build a fresh-interpreter probe with shared SDK/schema and I/O guards."""
    def build(forbidden, body):
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


@pytest.fixture(autouse=True)
def strategy_path_environment(monkeypatch):
    """Tests opt into external config paths explicitly."""
    for name in ('ACCOUNT_CONFIG_PATH', 'COMPOSITE_CONFIG_DIR',
                 'INDEX_CONFIG_DIR', 'IMOEX_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
