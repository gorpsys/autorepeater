"""ACCOUNT settings are prepared once, independently of other algorithms."""
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from autorepeater import account_config, strategies, runner, serverless
from autorepeater.account_strategy import AccountStrategy, prepare_account_source
import main as cli


@pytest.fixture(autouse=True)
def account_environment(monkeypatch):
    """Use the bundled settings unless a test supplies its own path."""
    monkeypatch.delenv('ACCOUNT_CONFIG_PATH', raising=False)


@pytest.mark.parametrize('reserve', ['0', '0.01', '0.999999999'])
def test_valid_account_config(reserve):
    """A finite fraction, including zero, is preserved exactly."""
    config = account_config.validate_account_config({'reserve': reserve})
    assert config.reserve == Decimal(reserve)


@pytest.mark.parametrize('payload', [None, [], 'account', 1, True])
def test_invalid_account_root(payload):
    """Settings require an object."""
    with pytest.raises(ValueError, match='account config'):
        account_config.validate_account_config(payload)


@pytest.mark.parametrize('reserve', [
    None, True, False, 0, 0.01, [], {}, '', 'invalid', 'NaN', 'sNaN',
    'Infinity', '-Infinity', '-0.01', '1', '1.01',
])
def test_invalid_account_reserve(reserve):
    """Numbers, non-finite strings and fractions outside [0, 1) fail."""
    with pytest.raises(ValueError, match='reserve'):
        account_config.validate_account_config({'reserve': reserve})


def test_missing_account_reserve():
    """No constant supplies an omitted setting."""
    with pytest.raises(ValueError, match='reserve'):
        account_config.validate_account_config({})


def test_bundled_account_config_from_other_cwd(tmp_path, monkeypatch):
    """The default file belongs to the module, independent of cwd."""
    monkeypatch.chdir(tmp_path)
    prepared = prepare_account_source('00123')
    assert prepared.src == '00123'
    assert prepared.config.reserve == Decimal('0.01')
    with patch('builtins.open', side_effect=AssertionError('constructor I/O')), \
            patch.object(Path, 'open', side_effect=AssertionError('constructor I/O')):
        strategy = AccountStrategy(prepared)
    assert strategy.src == '00123'
    assert strategy.config is prepared.config
    assert strategy.config.reserve == Decimal('0.01')


def test_relative_account_path_is_frozen(tmp_path, monkeypatch):
    """Preparation reads once; a later launch sees mutations, construction does not."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', 'settings.json')
    path = tmp_path / 'settings.json'
    path.write_text('{"reserve":"0"}', encoding='utf-8')
    with patch('builtins.open', wraps=open) as read:
        prepared = strategies.prepare_strategy('ACCOUNT', '00123')
    assert read.call_count == 1
    path.write_text('{"reserve":"0.03"}', encoding='utf-8')
    with patch('builtins.open', side_effect=AssertionError('creation I/O')), \
            patch.object(Path, 'open', side_effect=AssertionError('creation I/O')):
        strategy = strategies.create_strategy(prepared)
    assert strategy.config.reserve == Decimal(0)
    assert strategies.create_strategy(
        strategies.prepare_strategy('ACCOUNT', '00123')).config.reserve == Decimal('0.03')


def test_account_does_not_read_foreign_configs(tmp_path, monkeypatch):
    """Broken or conflicting INDEX and COMPOSITE paths cannot affect ACCOUNT."""
    broken = tmp_path / 'broken.json'
    broken.write_text('{', encoding='utf-8')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(broken))
    monkeypatch.setenv('IMOEX_CONFIG_PATH', str(broken))
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(broken))
    with patch('autorepeater.index_config.read_index_document',
               side_effect=AssertionError('foreign config read')):
        strategy = strategies.create_strategy(strategies.prepare_strategy('ACCOUNT', '00123'))
    assert strategy.src == '00123'
    assert strategy.config.reserve == Decimal('0.01')


@pytest.mark.parametrize('path', ['', ' ', 'missing.json', '.'])
def test_invalid_account_path(tmp_path, monkeypatch, path):
    """Empty, missing and directory paths fail with a useful path diagnostic."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', path)
    with pytest.raises(ValueError, match='account config'):
        prepare_account_source('00123')


@pytest.mark.parametrize('payload', ['{', '', '{}', '[]', '{"reserve":true}',
                                         '{"reserve":0.01}', '{"reserve":"NaN"}'])
@pytest.mark.parametrize('entrypoint', ['prepare', 'cli', 'cloud'])
def test_invalid_settings_before_client(tmp_path, monkeypatch, payload, entrypoint):
    """Bad settings stop entrypoints before credentials, construction or SDK access."""
    path = tmp_path / 'account.json'
    path.write_text(payload, encoding='utf-8')
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', str(path))
    monkeypatch.setattr('sys.argv', ['main.py', '--algoritm', 'ACCOUNT', '-s', '00123'])
    with patch.object(runner, 'Client') as client, \
            patch.object(cli, 'Runner') as local, \
            patch.object(serverless, 'Runner') as cloud, \
            patch.object(serverless, 'configure_yc_logging'), \
            patch.object(serverless, 'get_param', side_effect=AssertionError('credentials read')):
        with pytest.raises(ValueError) as error:
            if entrypoint == 'prepare':
                strategies.prepare_strategy('ACCOUNT', '00123')
            elif entrypoint == 'cli':
                cli.main()
            else:
                serverless.handler({'queryStringParameters': {
                    'algoritm': 'ACCOUNT', 'src': '00123'}}, None)
    assert str(path) in str(error.value)
    client.assert_not_called()
    local.assert_not_called()
    cloud.assert_not_called()


def test_unreadable_account_config(tmp_path, monkeypatch):
    """Read errors retain the path and original cause."""
    path = tmp_path / 'account.json'
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', str(path))
    with patch('builtins.open', side_effect=PermissionError('cannot read')):
        with pytest.raises(ValueError, match='cannot read') as error:
            prepare_account_source('00123')
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, PermissionError)


def test_invalid_account_source_does_not_read_settings():
    """Validate the source before loading even the algorithm's own config."""
    with patch('builtins.open', side_effect=AssertionError('config read')):
        with pytest.raises(ValueError, match='unsupported src'):
            prepare_account_source('IMOEX')


def test_absolute_account_path(tmp_path, monkeypatch):
    """An explicit absolute path selects only the requested settings."""
    path = tmp_path / 'account.json'
    path.write_text(json.dumps({'reserve': '0.02'}), encoding='utf-8')
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', str(path))
    assert account_config.load_account_config().reserve == Decimal('0.02')
