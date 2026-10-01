"""Keep local strategy configuration paths independent of the caller's shell."""
import pytest


@pytest.fixture(autouse=True)
def strategy_path_environment(monkeypatch):
    """Tests opt into external config paths explicitly."""
    for name in ('ACCOUNT_CONFIG_PATH', 'COMPOSITE_CONFIG_DIR',
                 'INDEX_CONFIG_DIR', 'IMOEX_CONFIG_PATH'):
        monkeypatch.delenv(name, raising=False)
