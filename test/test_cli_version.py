"""Version output never requires strategy selectors, credentials or API calls."""
import sys
from unittest.mock import create_autospec

import pytest

import main as cli
from autorepeater import __version__


@pytest.mark.parametrize('flag', ['--version', '-v'])
def test_version_exits_before_strategy_or_credentials(flag, monkeypatch, capsys):
    """Both aliases are terminal actions, not trading runs."""
    monkeypatch.setattr(sys, 'argv', ['main.py', flag])
    monkeypatch.delenv('INVEST_TOKEN', raising=False)
    runner = create_autospec(cli.Runner, spec_set=True)
    prepare = create_autospec(cli.prepare_strategy, spec_set=True)
    monkeypatch.setattr(cli, 'Runner', runner)
    monkeypatch.setattr(cli, 'prepare_strategy', prepare)
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 0
    assert capsys.readouterr().out == f'autorepeater {__version__}\n'
    runner.assert_not_called()
    prepare.assert_not_called()
