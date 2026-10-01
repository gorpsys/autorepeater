# pylint: disable=too-many-arguments, too-many-positional-arguments
# pylint: disable=duplicate-code
"""Explicit algorithm selection and isolated, one-pass index preparation."""
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import pytest

from autorepeater import strategies
from autorepeater import index_config
from autorepeater import composite_config
from autorepeater import runner as runner_module
from autorepeater import serverless
from autorepeater.index_config import load_index_configs, select_index_config
from autorepeater.index_strategy import IndexQuote
from autorepeater.account_strategy import PreparedAccountSource
from autorepeater.account_config import AccountConfig
from autorepeater.strategy_contract import AlgorithmDefinition
from scripts import check_imoex_strategy as calibration
import main as cli


@pytest.fixture(autouse=True)
def selection_environment(monkeypatch):
    """Every selection has controlled paths and a private registry."""
    monkeypatch.delenv('INDEX_CONFIG_DIR', raising=False)
    monkeypatch.delenv('IMOEX_CONFIG_PATH', raising=False)
    monkeypatch.delenv('ACCOUNT_CONFIG_PATH', raising=False)
    monkeypatch.delenv('COMPOSITE_CONFIG_DIR', raising=False)
    for name in ('ALGORITM', 'SRC_ACCOUNT', 'DST_ACCOUNT', 'INVEST_TOKEN', 't_token'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(strategies, 'ALGORITHMS', dict(strategies.ALGORITHMS))


@pytest.fixture(name='documents')
def fixture_documents(tmp_path, monkeypatch):
    """Return a writer for small, independently valid index documents."""
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))

    def write(filename, name='GOOD', **changes):
        payload = {
            'name': name, 'max_lot_weight_error': '0.05', 'reserve': '0.01',
            'instruments': [{
                'ticker': 'ONE', 'effective_quantity': '1', 'free_float': '1',
                'weight_limit': '1', 'reference_price': '10',
                'reference_weight': '100', 'reference_index_capitalization': '10',
            }],
            **changes,
        }
        path = tmp_path / filename
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    return write


@pytest.mark.parametrize('algoritm', [None, '', ' ', 'account', ' ACCOUNT', 'INDEX ', 1, []])
def test_algorithm_required_and_exact(algoritm):
    """No source spelling implicitly selects a registered algorithm."""
    with pytest.raises(ValueError, match='algoritm'):
        strategies.prepare_strategy(algoritm, '123')


def test_registration_prepares_once_without_creating(monkeypatch):
    """The saved factory and opaque input survive later registry changes."""
    opaque = object()
    strategy = Mock(spec_set=[
        'load_snapshot', 'build_target', 'event_accounts', 'should_rebalance'])
    prepare = Mock(side_effect=lambda src, context: opaque)
    create = Mock(return_value=strategy)
    strategies.register_algorithm('CUSTOM', AlgorithmDefinition(prepare, create))
    monkeypatch.setenv('INDEX_CONFIG_DIR', '/missing-index')
    monkeypatch.setenv('IMOEX_CONFIG_PATH', '/also-missing')
    prepared = strategies.prepare_strategy('CUSTOM', 'own-source')
    create.assert_not_called()
    prepare.assert_called_once_with('own-source', ANY)
    monkeypatch.delitem(strategies.ALGORITHMS, 'CUSTOM')
    assert strategies.create_strategy(prepared) is strategy
    create.assert_called_once_with(opaque)
    prepare.assert_called_once_with('own-source', ANY)


def test_selected_valid_config_survives_foreign_errors(documents, tmp_path, caplog):
    """Foreign schema errors and unreadable JSON warn once and do not block GOOD."""
    good = documents('renamed.json')
    bad = documents('bad.json', 'BAD', reserve='NaN')
    broken = tmp_path / 'broken.json'
    broken.write_text('{"name": "GOOD",', encoding='utf-8')
    with patch('builtins.open', wraps=open) as reads:
        prepared = strategies.prepare_strategy('INDEX', 'GOOD')
        assert len(caplog.records) == 2
        strategy = strategies.create_strategy(prepared)
        assert len(caplog.records) == 2
    assert strategy.config.name == 'GOOD'
    assert sorted(str(item.args[0]) for item in reads.call_args_list) == sorted(
        map(str, [good, bad, broken]))
    assert any(str(bad) in record.message and 'reserve' in record.message
               and 'BAD' in record.message
               for record in caplog.records)
    assert any(str(broken) in record.message for record in caplog.records)
    with pytest.raises(ValueError):
        load_index_configs()


@pytest.mark.parametrize('consumer', ['create', 'runner'])
def test_prepared_config_does_not_reload_after_file_mutation(documents, consumer):
    """Creation uses the prepared document; only a new launch sees a changed file."""
    documents('one.json')
    prepared = strategies.prepare_strategy('INDEX', 'GOOD')
    documents('one.json', reserve='0.02')
    with patch.object(runner_module, 'configure_local_logging'), \
            patch.object(index_config, 'read_index_document',
                         side_effect=AssertionError('creation must not read files')):
        strategy = (strategies.create_strategy(prepared) if consumer == 'create'
                    else runner_module.Runner('token', prepared, 'dst').strategy)
    assert strategy.config.reserve == Decimal('0.01')
    next_prepared = strategies.prepare_strategy('INDEX', 'GOOD')
    assert strategies.create_strategy(next_prepared).config.reserve == Decimal('0.02')


@pytest.mark.parametrize('name', ['123456', 'ACCOUNT', 'INDEX'])
def test_config_namespace_independent_of_algorithms(documents, name):
    """Numeric names and algorithm names are valid INDEX sources."""
    documents('unrelated.json', name)
    assert strategies.create_strategy(
        strategies.prepare_strategy('INDEX', name)).config.name == name
    with pytest.raises(ValueError):
        strategies.prepare_strategy('ACCOUNT', 'IMOEX')


def test_duplicate_selected_name_counts_invalid_body(documents):
    """A readable duplicate cannot disappear because its body is invalid."""
    documents('one.json')
    documents('two.json', reserve='NaN')
    with pytest.raises(ValueError, match='duplicate.*GOOD'):
        strategies.prepare_strategy('INDEX', 'GOOD')


def test_foreign_duplicate_warns_once(documents, caplog):
    """Duplicate foreign names do not stop a unique selected index."""
    documents('one.json')
    documents('two.json', 'OTHER')
    documents('three.json', 'OTHER')
    strategies.prepare_strategy('INDEX', 'GOOD')
    assert len(caplog.records) == 1
    assert 'duplicate' in caplog.messages[0] and 'OTHER' in caplog.messages[0]


def test_foreign_invalid_duplicate_warns_once_per_problem(documents, caplog):
    """A foreign invalid body and readable duplicate each produce one diagnostic."""
    documents('good.json')
    documents('one.json', 'OTHER')
    documents('two.json', 'OTHER', reserve='NaN')
    strategies.create_strategy(strategies.prepare_strategy('INDEX', 'GOOD'))
    assert len(caplog.records) == 2
    assert sum('reserve' in message for message in caplog.messages) == 1
    assert sum(message.startswith('duplicate strategy name:')
               for message in caplog.messages) == 1


def test_missing_name_with_unreadable_files_lists_paths(documents, tmp_path):
    """Malformed JSON is never guessed from its filename or apparent name text."""
    documents('one.json')
    broken = tmp_path / 'MISSING.json'
    broken.write_text('{"name":"MISSING",', encoding='utf-8')
    with pytest.raises(ValueError) as error:
        strategies.prepare_strategy('INDEX', 'MISSING')
    assert 'MISSING' in str(error.value) and str(broken) in str(error.value)


def test_selected_schema_error_stops_before_factory_and_client(documents, monkeypatch):
    """The selected candidate's saved error is raised before construction."""
    path = documents('bad.json', reserve='NaN')
    definition = strategies.ALGORITHMS['INDEX']
    factory = Mock(wraps=definition.create)
    monkeypatch.setitem(strategies.ALGORITHMS, 'INDEX',
                        AlgorithmDefinition(definition.prepare_source, factory))
    with patch.object(runner_module, 'Client') as client:
        with pytest.raises(ValueError) as error:
            strategies.prepare_strategy('INDEX', 'GOOD')
        assert str(path) in str(error.value)
        assert 'GOOD' in str(error.value) and 'reserve' in str(error.value)
        factory.assert_not_called()
        client.assert_not_called()


@pytest.mark.parametrize('algoritm, src', [('ACCOUNT', '00123'), ('CUSTOM', 'own-format')])
def test_independent_algorithm_never_reads_index_settings(algoritm, src):
    """The meaning of src belongs to its registration, with no index environment access."""
    if algoritm == 'CUSTOM':
        strategies.register_algorithm('CUSTOM', AlgorithmDefinition(
            lambda value, context: value, Mock()))
    with patch.object(index_config, '_index_paths',
                      side_effect=AssertionError('index settings read')) as paths:
        prepared = strategies.prepare_strategy(algoritm, src)
    if algoritm == 'ACCOUNT':
        assert prepared.prepared_source == PreparedAccountSource(
            src, AccountConfig(Decimal('0.01')))
    else:
        assert prepared.prepared_source == src
    paths.assert_not_called()


@pytest.mark.parametrize('chosen', ['GOOD', 'BAD'])
def test_unreadable_foreign_file_is_independently_diagnosed(documents, chosen, caplog):
    """One file's read failure preserves the usable file and the problematic path."""
    good = documents('good.json')
    bad = documents('bad.json', 'BAD')
    with patch.object(index_config, 'read_index_document',
                      side_effect=[PermissionError('cannot read'),
                                   json.loads(good.read_text(encoding='utf-8'))]) as read:
        if chosen == 'GOOD':
            assert select_index_config(chosen).name == 'GOOD'
        else:
            with pytest.raises(ValueError) as error:
                select_index_config(chosen)
            assert str(bad) in str(error.value)
        assert read.call_count == 2
    assert len(caplog.records) == 1
    assert str(bad) in caplog.messages[0] and 'cannot read' in caplog.messages[0]


@pytest.mark.parametrize('name', [None, '', ' ', 'BAD NAME', 1])
def test_invalid_registration_name(name):
    """Registration cannot normalize invalid names into valid ones."""
    with pytest.raises(ValueError):
        strategies.register_algorithm(name, AlgorithmDefinition(Mock(), Mock()))


def test_duplicate_registration_rejected():
    """Even the same definition cannot silently replace an existing registration."""
    with pytest.raises(ValueError, match='duplicate'):
        strategies.register_algorithm('ACCOUNT', strategies.ALGORITHMS['ACCOUNT'])


@pytest.mark.parametrize('definition', [
    None, Mock(), AlgorithmDefinition(None, Mock()), AlgorithmDefinition(Mock(), None),
])
def test_invalid_registration_definition(definition):
    """Both registration operations must be callable."""
    with pytest.raises(TypeError, match='callable'):
        strategies.register_algorithm('CUSTOM', definition)


def test_old_creation_interface_is_removed():
    """Callers must prepare a source explicitly before creating or running it."""
    assert not hasattr(strategies, 'validate_src')
    assert not hasattr(strategies, 'NAMED_STRATEGIES')
    assert not hasattr(serverless, 'DEFAULT_SRC_ACCOUNT')
    with pytest.raises(TypeError, match='PreparedStrategy'):
        runner_module.Runner('token', '123', 'dst')


@pytest.mark.parametrize('entrypoint', ['cli', 'cloud', 'runner'])
@pytest.mark.parametrize('foreign', ['schema', 'syntax', 'missing_name', 'duplicate'])
def test_launch_prepares_and_warns_once(documents, tmp_path, monkeypatch, caplog,
                                      entrypoint, foreign):
    """All application launches use one preparation, read pass, and warning pass."""
    documents('good.json')
    if foreign == 'schema':
        documents('bad.json', 'BAD', reserve='NaN')
    elif foreign == 'syntax':
        (tmp_path / 'bad.json').write_text('{', encoding='utf-8')
    elif foreign == 'missing_name':
        (tmp_path / 'bad.json').write_text('{}', encoding='utf-8')
    else:
        documents('bad.json', 'BAD')
        documents('duplicate.json', 'BAD')
    monkeypatch.setenv('INVEST_TOKEN', 'synthetic-token')
    monkeypatch.setenv('SRC_ACCOUNT', 'GOOD')
    monkeypatch.setenv('ALGORITM', 'INDEX')
    monkeypatch.setenv('DST_ACCOUNT', 'dst')
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', 'INDEX', '-s', 'GOOD', '-d', 'dst'])
    selected_definition = strategies.ALGORITHMS['INDEX']
    prepare = Mock(wraps=selected_definition.prepare_source)
    create = Mock(wraps=selected_definition.create)
    monkeypatch.setitem(strategies.ALGORITHMS, 'INDEX', AlgorithmDefinition(prepare, create))
    with patch.object(index_config, 'read_index_document',
                      wraps=index_config.read_index_document) as read, \
            patch.object(runner_module, 'Client', autospec=True) as client, \
            patch.object(runner_module, 'configure_local_logging'), \
            patch.object(serverless, 'configure_yc_logging'), \
            patch.object(runner_module, 'print_all_portfolio'), \
            patch.object(runner_module.Runner, '_create_repeater', autospec=True) as engine:
        if entrypoint == 'cli':
            cli.main()
        elif entrypoint == 'cloud':
            result = serverless.handler({'queryStringParameters': {}}, None)
            assert result['body'] == 'Success sync, GOOD dst!'
        else:
            prepared = strategies.prepare_strategy('INDEX', 'GOOD')
            client.assert_not_called()
            runner_module.Runner('synthetic-token', prepared, 'dst').run_sync()
        prepare.assert_called_once_with('GOOD', ANY)
        create.assert_called_once()
        assert sorted(str(item.args[0]) for item in read.call_args_list) == sorted(
            str(path) for path in tmp_path.glob('*.json'))
        assert len(caplog.records) == 1
        assert 'bad.json' in caplog.messages[0]
        client.assert_called_once()
        if entrypoint == 'cli':
            engine.return_value.mainflow.assert_called_once_with('dst')
        else:
            engine.return_value.sync_accounts.assert_called_once_with('dst')


@pytest.mark.parametrize('arguments', [[], ['-s', '123'], ['--algoritm', 'ACCOUNT']])
def test_cli_missing_required_arguments_before_credentials(monkeypatch, arguments):
    """Argparse requires both fields without consulting credentials or Runner."""
    monkeypatch.setattr(sys, 'argv', ['main.py', *arguments])
    with patch.object(cli, 'Runner') as runner, patch.object(cli, 'os') as environment:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
        runner.assert_not_called()
        assert environment.mock_calls == []


@pytest.mark.parametrize('algoritm', ['', ' ', 'account', 'UNKNOWN'])
def test_cli_invalid_algorithm_before_credentials(monkeypatch, algoritm):
    """Registry validation, rather than argparse choices, rejects algorithm names."""
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', algoritm, '-s', '123'])
    with patch.object(cli, 'Runner') as runner, patch.object(cli, 'os') as environment:
        with pytest.raises(ValueError, match='algoritm'):
            cli.main()
        runner.assert_not_called()
        assert environment.mock_calls == []


@pytest.mark.parametrize('location, algoritm', [
    *[('query', value) for value in [None, '', ' ', 'account', 'UNKNOWN']],
    *[('environment', value) for value in ['', ' ', 'account', 'UNKNOWN']],
])
def test_cloud_invalid_algorithm_before_credentials(monkeypatch, location, algoritm):
    """A present invalid query never falls back to a valid environment algorithm."""
    query = {'src': '123'}
    if location == 'query':
        query['algoritm'] = algoritm
        monkeypatch.setenv('ALGORITM', 'ACCOUNT')
    elif location == 'environment' and algoritm is not None:
        monkeypatch.setenv('ALGORITM', algoritm)
    with patch.object(serverless, 'Runner') as runner, \
            patch.object(serverless, 'configure_yc_logging'), \
            patch.object(serverless, 'get_param', side_effect=AssertionError('credentials read')):
        with pytest.raises(ValueError, match='algoritm'):
            serverless.handler({'queryStringParameters': query}, None)
        runner.assert_not_called()


def test_cloud_query_algorithm_priority(monkeypatch):
    """Query ACCOUNT overrides invalid ALGORITM and leaves token/dst fallbacks intact."""
    monkeypatch.setenv('ALGORITM', 'UNKNOWN')
    monkeypatch.setenv('SRC_ACCOUNT', 'INVALID')
    monkeypatch.setenv('t_token', 'legacy-token')
    with patch.object(serverless, 'Runner') as runner, \
            patch.object(serverless, 'configure_yc_logging'):
        result = serverless.handler({'queryStringParameters': {
            'algoritm': 'ACCOUNT', 'src': '00123'}}, None)
    prepared = runner.call_args.kwargs['prepared_strategy']
    assert prepared.prepared_source.src == '00123'
    assert runner.call_args.kwargs['token'] == 'legacy-token'
    assert result['body'] == f'Success sync, 00123 {serverless.DEFAULT_DST_ACCOUNT}!'


@pytest.mark.parametrize('algoritm', ['ACCOUNT', 'INDEX'])
@pytest.mark.parametrize('query', [{}, {'src': None}, {'src': ''}, {'src': ' '}])
def test_cloud_requires_explicit_source(monkeypatch, query, algoritm):
    """Nondefault algorithms require a source; empty values never fall back."""
    monkeypatch.setenv('ALGORITM', algoritm)
    if query:
        monkeypatch.setenv('SRC_ACCOUNT', '123')
    with patch.object(serverless, 'Runner') as runner, \
            patch.object(serverless, 'configure_yc_logging'), \
            patch.object(serverless, 'get_param', side_effect=AssertionError('credentials read')):
        with pytest.raises(ValueError, match='src is required'):
            serverless.handler({'queryStringParameters': query}, None)
        runner.assert_not_called()


@pytest.mark.parametrize('event', [
    None, {}, {'queryStringParameters': None}, {'queryStringParameters': {}},
])
def test_cloud_defaults_to_balanced(monkeypatch, event):
    """A timer invocation prepares the exact ordered composition once."""
    monkeypatch.setenv('INVEST_TOKEN', 'synthetic-token')
    with patch.object(serverless, 'Runner', autospec=True) as runner, \
            patch.object(serverless, 'configure_yc_logging'), \
            patch.object(index_config, 'read_index_document',
                         wraps=index_config.read_index_document) as read, \
            patch.object(composite_config, 'read_composite_document',
                         wraps=composite_config.read_composite_document) as composite_read:
        result = serverless.handler(event, None)
    prepared = runner.call_args.kwargs['prepared_strategy']
    strategy = strategies.create_strategy(prepared)
    assert strategy.source.name == 'BALANCED'
    assert [(item.algoritm, item.src, item.weight) for item in strategy.source.components] == [
        ('INDEX', 'IMOEX', Decimal('0.684210526')),
        ('INDEX', 'BOND', Decimal('0.210526316')),
        ('INDEX', 'GOLD', Decimal('0.105263158'))]
    assert sum(item.weight for item in strategy.source.components) == Decimal('1')
    assert [child.config.name for child in strategy.children] == ['IMOEX', 'BOND', 'GOLD']
    paths = list(Path(index_config.__file__).with_name('configs').glob('*.json'))
    assert sorted(str(item.args[0]) for item in read.call_args_list) == sorted(map(str, paths * 3))
    composite_read.assert_called_once_with(
        Path(composite_config.__file__).parent / 'configs/composite/balanced.json')
    runner.assert_called_once_with(token='synthetic-token', prepared_strategy=prepared,
                                   dst=serverless.DEFAULT_DST_ACCOUNT)
    assert runner.return_value.method_calls == [call.run_sync()]
    assert result['body'] == f'Success sync, BALANCED {serverless.DEFAULT_DST_ACCOUNT}!'


@pytest.mark.parametrize('location', ['query', 'environment'])
@pytest.mark.parametrize('source', [None, 'GOLD', 'BALANCED'])
def test_cloud_partial_index_selection(monkeypatch, location, source):
    """Explicit INDEX needs src; src alone names a COMPOSITE config."""
    monkeypatch.setenv('INVEST_TOKEN', 'synthetic-token')
    query = {}
    name, value = ('algoritm', 'INDEX') if source is None else ('src', source)
    if location == 'query':
        query[name] = value
    else:
        monkeypatch.setenv('ALGORITM' if name == 'algoritm' else 'SRC_ACCOUNT', value)
    with patch.object(serverless, 'Runner', autospec=True) as runner, \
            patch.object(serverless, 'configure_yc_logging'):
        if source != 'BALANCED':
            with pytest.raises(ValueError, match=(
                    'src is required' if source is None else 'unsupported src: GOLD')):
                serverless.handler({'queryStringParameters': query}, None)
            runner.assert_not_called()
            return
        serverless.handler({'queryStringParameters': query}, None)
    assert runner.call_args.kwargs['prepared_strategy'].prepared_source.name == 'BALANCED'
    runner.return_value.run_sync.assert_called_once_with()


@pytest.mark.parametrize('location', ['query', 'environment'])
def test_cloud_explicit_previous_default(monkeypatch, location):
    """INDEX/TMON remains selectable explicitly with the same reserve."""
    monkeypatch.setenv('INVEST_TOKEN', 'synthetic-token')
    query = {'algoritm': 'INDEX', 'src': 'TMON'} if location == 'query' else {}
    if location == 'environment':
        monkeypatch.setenv('ALGORITM', 'INDEX')
        monkeypatch.setenv('SRC_ACCOUNT', 'TMON')
    with patch.object(serverless, 'Runner') as runner, \
            patch.object(serverless, 'configure_yc_logging'):
        serverless.handler({'queryStringParameters': query}, None)
    strategy = strategies.create_strategy(runner.call_args.kwargs['prepared_strategy'])
    assert strategy.config.name == 'TMON'
    assert strategy.config.reserve == Decimal('0.0005')


@pytest.mark.parametrize('payload', ['{}', '[]', '{"name":"BAD","reserve":"NaN"}'])
def test_foreign_validation_errors_include_path(documents, tmp_path, caplog, payload):
    """Missing names, invalid roots and invalid bodies remain independently diagnosed."""
    documents('good.json')
    path = tmp_path / 'bad.json'
    path.write_text(payload, encoding='utf-8')
    assert select_index_config('GOOD').name == 'GOOD'
    assert len(caplog.records) == 1 and str(path) in caplog.messages[0]
    with pytest.raises(ValueError):
        load_index_configs()


@pytest.mark.parametrize('mode', ['single', 'several', 'syntax', 'schema', 'duplicate'])
def test_calibration_auto_selection_requires_clean_single_candidate(
        documents, tmp_path, mode):
    """Automatic selection never guesses a unique good candidate among errors."""
    documents('renamed.json')
    if mode == 'syntax':
        (tmp_path / 'bad.json').write_text('{', encoding='utf-8')
    elif mode != 'single':
        documents('other.json', 'GOOD' if mode == 'duplicate' else 'OTHER',
                  reserve='NaN' if mode == 'schema' else '0.01')
    with patch.object(index_config, 'read_index_document',
                      wraps=index_config.read_index_document) as read:
        if mode == 'single':
            assert select_index_config().name == 'GOOD'
        else:
            with pytest.raises(ValueError, match='--src'):
                select_index_config()
        assert read.call_count == len(list(tmp_path.glob('*.json')))


@pytest.mark.parametrize('explicit', [True, False])
def test_calibrator_uses_same_single_pass_selection(documents, monkeypatch, caplog, explicit):
    """The INDEX calibrator reuses prepared data and warns only during selection."""
    documents('renamed.json')
    if explicit:
        documents('bad.json', 'BAD', reserve='NaN')
    monkeypatch.setenv('READ_ONLY_INVEST_TOKEN', 'synthetic-read-only')
    snapshot = {'ONE': IndexQuote('one', Decimal('10'), 1)}
    with patch.object(index_config, 'read_index_document',
                      wraps=index_config.read_index_document) as read, \
            patch.object(calibration, 'Client') as client, \
            patch.object(calibration.IndexStrategy, 'load_snapshot', return_value=snapshot), \
            patch.object(calibration.reporting, 'print_index_calibration'):
        calibration.main(['--src', 'GOOD'] if explicit else [])
        assert read.call_count == (2 if explicit else 1)
        assert len(caplog.records) == (1 if explicit else 0)
        client.assert_called_once()


def test_registry_import_does_not_read_files_or_sdk():
    """A fresh process imports built-in registrations without filesystem or network I/O."""
    script = '''
import builtins
import pathlib
import socket

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.startswith(('t_tech', 'grpc')):
        raise AssertionError('SDK import forbidden')
    return original_import(name, *args, **kwargs)
def blocked_io(*args, **kwargs):
    raise AssertionError('I/O forbidden')
builtins.__import__ = guarded_import
builtins.open = blocked_io
pathlib.Path.open = blocked_io
socket.socket = blocked_io
from autorepeater.strategies import ALGORITHMS, prepare_strategy
assert set(ALGORITHMS) == {'ACCOUNT', 'INDEX', 'COMPOSITE'}
try:
    prepare_strategy('ACCOUNT', '00123')
except AssertionError as error:
    assert str(error) == 'I/O forbidden'
else:
    raise AssertionError('ACCOUNT preparation must read its own settings')
'''
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
