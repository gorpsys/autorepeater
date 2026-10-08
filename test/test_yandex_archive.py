"""Direct, coverage-visible checks of archive selection, writing and entrypoint."""
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from scripts import build_yandex_archive as builder


@pytest.fixture(name='project')
def fixture_project(tmp_path):
    """An isolated project contains allowed files and representative distractions."""
    root = tmp_path / 'project'
    contents = {
        'main.py': b'main', 'handler.py': b'handler', 'requirements.txt': b'requirements',
        'autorepeater/__init__.py': b'', 'autorepeater/new_strategy.py': b'new module',
        'autorepeater/configs/index.json': b'{"reserve":"0.01"}',
        'autorepeater/configs/account/account.json': b'{"reserve":"0.01"}',
        'autorepeater/configs/composite/balanced.json': b'{"name":"BALANCED"}',
        'autorepeater/configs/composite/nested/demo.json': b'{"name":"demo"}',
        'autorepeater/configs/new/catalog/deep/another.json': b'{"name":"another"}\n',
        'autorepeater/configs/.hidden.json': b'hidden',
        'autorepeater/configs/.hidden/nested.json': b'hidden directory',
        'autorepeater/configs/account/readme.txt': b'not JSON',
        'autorepeater/configs/new/catalog/.hidden.json': b'hidden nested file',
        'autorepeater/configs/new/.hidden/deep.json': b'hidden nested directory',
        'autorepeater/configs/new/catalog/upper.JSON': b'not lowercase JSON',
        'autorepeater/configs/new/catalog/config.json.bak': b'not JSON',
        'autorepeater/.hidden.py': b'hidden module',
        'autorepeater/nested/module.py': b'not flat',
        'scripts/build_yandex_archive.py': b'not packaged',
        'test/test_example.py': b'not packaged',
        'venv/lib/example.py': b'not packaged',
    }
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'external.json').write_bytes(b'external')
    (root / 'autorepeater/configs/linked').symlink_to(external, target_is_directory=True)
    (root / 'autorepeater/configs/linked.json').symlink_to(external / 'external.json')
    (root / 'autorepeater/configs/new/catalog/linked').symlink_to(
        external, target_is_directory=True)
    (root / 'autorepeater/configs/new/catalog/linked.json').symlink_to(external / 'external.json')
    (root / 'autorepeater/linked.py').symlink_to(external / 'external.json')
    return root


EXPECTED = {
    'main.py', 'handler.py', 'requirements.txt', 'autorepeater/__init__.py',
    'autorepeater/new_strategy.py', 'autorepeater/configs/index.json',
    'autorepeater/configs/account/account.json',
    'autorepeater/configs/composite/balanced.json',
    'autorepeater/configs/composite/nested/demo.json',
    'autorepeater/configs/new/catalog/deep/another.json',
}


def test_archive_files_and_bytes(project, monkeypatch, tmp_path):
    """Keep paths and bytes, excluding hidden, external and unrelated files."""
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path / 'external'))
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(tmp_path / 'external'))
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', str(tmp_path / 'external/external.json'))
    assert {path.relative_to(project).as_posix()
            for path in builder.archive_files(project)} == EXPECTED
    archive_path = builder.build_archive(project)
    assert archive_path == project / 'build/yandex-function.zip'
    with ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == EXPECTED
        for name in EXPECTED:
            assert archive.read(name) == (project / name).read_bytes()
    assert not (project / 'build/yandex-function').exists()


def test_custom_output_and_rebuild(project, tmp_path):
    """Each build replaces a previous ZIP without retaining stale members."""
    output = tmp_path / 'output/function.zip'
    assert builder.build_archive(project, output) == output
    (project / 'autorepeater/new_strategy.py').unlink()
    builder.build_archive(project, output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == EXPECTED - {'autorepeater/new_strategy.py'}


def test_import_does_not_build():
    """Importing the script has no filesystem side effects."""
    with patch.object(Path, 'mkdir', side_effect=AssertionError('import wrote files')), \
            patch('zipfile.ZipFile', side_effect=AssertionError('import built archive')):
        runpy.run_path(str(Path(builder.__file__)))


def test_entrypoint_in_process(project, tmp_path, monkeypatch):
    """The executable entrypoint uses the same builder, within coverage tracing."""
    output = tmp_path / 'entrypoint.zip'
    monkeypatch.setattr('sys.argv', ['build_yandex_archive.py', '--project-root', str(project),
                                     '--output', str(output)])
    runpy.run_path(str(Path(builder.__file__)), run_name='__main__')
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == EXPECTED


def test_default_entrypoint(project):
    """The default source root is inferred from the script, not cwd."""
    with patch.object(builder, '__file__', str(project / 'scripts/build_yandex_archive.py')):
        builder.main([])
    assert (project / 'build/yandex-function.zip').is_file()


@pytest.mark.parametrize('override', [False, True])
def test_make_archive_without_python_alias(project, tmp_path, override):
    """Make uses python3 by default and accepts an explicit interpreter."""
    repository = Path(__file__).resolve().parents[1]
    for relative in ('Makefile', 'scripts/build_yandex_archive.py'):
        shutil.copyfile(repository / relative, project / relative)
    commands = tmp_path / 'commands'
    commands.mkdir()
    arguments = [f'PYTHON={sys.executable}'] if override else []
    if not override:
        (commands / 'python3').symlink_to(sys.executable)
    make = shutil.which('make')
    assert make is not None
    result = subprocess.run([make, 'claude-yandex-archive', *arguments], cwd=project,
                            env={'PATH': str(commands)}, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    with ZipFile(project / 'build/yandex-function.zip') as archive:
        assert set(archive.namelist()) == EXPECTED


def test_missing_required_file(project):
    """A missing root entry fails rather than producing a silently incomplete bundle."""
    (project / 'handler.py').unlink()
    with pytest.raises(FileNotFoundError):
        builder.build_archive(project)


def test_config_read_error(project):
    """Errors while selecting configs propagate."""
    with patch.object(Path, 'iterdir', side_effect=PermissionError('cannot list configs')):
        with pytest.raises(PermissionError, match='cannot list'):
            builder.build_archive(project)


def test_archive_read_error(project):
    """Errors while reading a selected file propagate to the caller."""
    with patch.object(builder.ZipFile, 'write', side_effect=PermissionError('cannot read file')):
        with pytest.raises(PermissionError, match='cannot read file'):
            builder.build_archive(project)


def test_archive_write_error(project, tmp_path):
    """An unwritable output is not reported as a successful build."""
    with pytest.raises(IsADirectoryError):
        builder.build_archive(project, tmp_path)


@pytest.fixture(name='application_bundle')
def fixture_application_bundle(tmp_path):
    """Package the real entrypoints and modules, with an independent source tree."""
    repository = Path(__file__).resolve().parents[1]
    project_root = tmp_path / 'application'
    sources = [repository / name for name in ('main.py', 'handler.py', 'requirements.txt')]
    sources.extend((repository / 'autorepeater').glob('*.py'))
    sources.extend((repository / 'autorepeater/configs').rglob('*.json'))
    for source in sources:
        relative = source.relative_to(repository)
        destination = project_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (project_root / 'scripts').mkdir()
    for relative in ('Makefile', 'scripts/build_yandex_archive.py'):
        shutil.copyfile(repository / relative, project_root / relative)
    expected = {source.relative_to(repository).as_posix() for source in sources}
    config_root = project_root / 'autorepeater/configs/new/deep'
    documents = {
        'composites/first-file.json': {
            'component_drift_limit': '0.20', 'name': 'ARCHIVE_ROOT',
            'components': [{'algoritm': 'COMPOSITE', 'src': 'ARCHIVE_MIDDLE', 'weight': '1'}]},
        'composites/second-file.json': {
            'component_drift_limit': '0.20', 'name': 'ARCHIVE_MIDDLE',
            'components': [{'algoritm': 'COMPOSITE', 'src': 'ARCHIVE_LEAF', 'weight': '1'}]},
        'composites/third-file.json': {
            'component_drift_limit': '0.20', 'name': 'ARCHIVE_LEAF', 'components': [
                {'algoritm': 'INDEX', 'src': 'ARCHIVE_INDEX', 'weight': '0.5'},
                {'algoritm': 'ACCOUNT', 'src': '00123', 'weight': '0.5'}]},
        'composites/invalid-foreign.json': {
            'component_drift_limit': '0.20', 'name': 'FOREIGN', 'components': []},
        'composites/duplicate-one.json': {
            'component_drift_limit': '0.20', 'name': 'DUPLICATE',
            'components': [{'algoritm': 'ACCOUNT', 'src': '00123', 'weight': '1'}]},
        'composites/duplicate-two.json': {
            'component_drift_limit': '0.20', 'name': 'DUPLICATE',
            'components': [{'algoritm': 'ACCOUNT', 'src': '00123', 'weight': '1'}]},
        'settings/different-name.json': {'name': '00123', 'source_account_id': '00123',
                                         'reserve': '0.07', 'allocation_drift_limit': '0.0092'},
    }
    index = json.loads((project_root / 'autorepeater/configs/gold.json').read_text(
        encoding='utf-8'))
    documents['indexes/unrelated-filename.json'] = dict(index, name='ARCHIVE_INDEX')
    documents['indexes/invalid-foreign.json'] = dict(index, name='FOREIGN_INDEX', reserve=1)
    for relative, document in documents.items():
        destination = config_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document) + '\n', encoding='utf-8')
        expected.add(destination.relative_to(project_root).as_posix())
    for relative in ('composites/malformed.json', 'indexes/malformed.json'):
        destination = config_root / relative
        destination.write_bytes(b'{')
        expected.add(destination.relative_to(project_root).as_posix())
    return project_root, expected


NESTED_ARCHIVE_PROBE = """
import os
import sys
import sysconfig
from decimal import Decimal
from pathlib import Path
from unittest.mock import call, patch

extracted, repository, source_tree = map(Path, sys.argv[1:4])
scenario, path_style = sys.argv[4:]
dependency_paths = {Path(sysconfig.get_path(key)).resolve() for key in ('purelib', 'platlib')}
for root in (repository, source_tree, extracted):
    assert not Path.cwd().is_relative_to(root)
    source_paths = [path for path in sys.path if Path(path).resolve().is_relative_to(root)
                    and Path(path).resolve() not in dependency_paths]
    assert not source_paths, ('application source leaked onto sys.path', source_paths)
sys.path.insert(0, str(extracted))
with patch('t_tech.invest.Client', side_effect=AssertionError('SDK client forbidden')) as client, \
        patch('grpc.secure_channel', side_effect=AssertionError('gRPC forbidden')) as secure, \
        patch('grpc.insecure_channel', side_effect=AssertionError('gRPC forbidden')) as insecure, \
        patch('socket.socket.connect', side_effect=AssertionError('network forbidden')) as connect, \
        patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as network:
    import handler
    import main
    from autorepeater import reporting, runner, serverless
    from autorepeater.composite_strategy import CompositeSnapshot, CompositeStrategy
    from autorepeater.index_strategy import IndexQuote
    from autorepeater.strategy_data import InstrumentType, PortfolioEntry
    from autorepeater.strategy_plan import StrategyContext
    from autorepeater.strategies import create_strategy, prepare_strategy
    assert Path(handler.__file__).resolve() == extracted / 'handler.py'
    assert Path(main.__file__).resolve() == extracted / 'main.py'

    with patch.object(runner.TInvestOrderExecutor, 'submit_order',
                      side_effect=AssertionError('trading forbidden')) as trade:
        # Exercise the actual handler and its saved prepared default, with no SDK lifecycle.
        with patch.object(serverless, 'Runner', autospec=True) as launch:
            result = handler.handler({'queryStringParameters': {
                'dst': 'destination', 'token': 'test-token'}}, None)
            assert result['body'] == 'Success sync, BALANCED destination!'
            launch.assert_called_once()
            assert launch.call_args.kwargs['dst'] == 'destination'
            assert launch.call_args.kwargs['token'] == 'test-token'
            balanced = create_strategy(launch.call_args.kwargs['prepared_strategy'])
            assert balanced.source.name == 'BALANCED'
            assert [(item.algoritm, item.src, item.weight)
                    for item in balanced.source.components] == [
                ('INDEX', 'IMOEX', Decimal('0.684210526')),
                ('INDEX', 'OBLG', Decimal('0.210526316')),
                ('INDEX', 'GOLD', Decimal('0.105263158'))]
            assert launch.return_value.method_calls == [call.run_sync()]

        account = create_strategy(prepare_strategy('ACCOUNT', '2193248994'))
        assert account.src == '2193248994' and account.config.reserve == Decimal('0.01')

        # Discover by JSON name through configured paths, independently of cwd.
        configs = extracted / 'autorepeater/configs/new/deep'
        for variable, path in [
            ('COMPOSITE_CONFIG_DIR', configs / 'composites'),
            ('INDEX_CONFIG_DIR', configs / 'indexes'),
            ('ACCOUNT_CONFIG_PATH', configs / 'settings/different-name.json')]:
            os.environ[variable] = os.path.relpath(path) if path_style == 'relative' else str(path)
        with patch.object(reporting, 'print_config_warning') as warnings, \
                patch.object(reporting, 'print_index_config_warning') as index_warnings, \
                patch.object(serverless, 'Runner', autospec=True) as launch:
            if scenario == 'valid':
                prepared = prepare_strategy('COMPOSITE', 'ARCHIVE_ROOT')
                nested = create_strategy(prepared)
                assert isinstance(nested, CompositeStrategy)
                middle, = nested.children
                leaf, = middle.children
                assert middle.source.name == 'ARCHIVE_MIDDLE'
                assert leaf.source.name == 'ARCHIVE_LEAF'
                index, account = leaf.children
                assert index.config.name == 'ARCHIVE_INDEX'
                assert account.src == '00123' and account.config.reserve == Decimal('0.07')
                assert nested.event_accounts('destination') == ('destination', '00123')
                account_snapshot = ({'share': PortfolioEntry(
                    'share', InstrumentType.SHARE, 'RUB', Decimal('10'), Decimal('10'), 'share')},
                    Decimal('100'))
                leaf_snapshot = CompositeSnapshot((
                    {'GOLD': IndexQuote('gold', Decimal('10'), 1)}, account_snapshot))
                snapshot = CompositeSnapshot((CompositeSnapshot((leaf_snapshot,)),))
                context = StrategyContext((), Decimal('1000'), Decimal(0), False, (),
                                          nested.allocation_profile(snapshot).prices)
                target = nested.build_plan(snapshot, context).target
                assert target.quantities == {'gold': Decimal('49'), 'share': Decimal('46.50')}
                assert target.prices == {'gold': Decimal('10'), 'share': Decimal('10')}
                messages = [item.args[0] for item in warnings.call_args_list]
                assert len(messages) == 9, messages
                for name in ('invalid-foreign.json', 'malformed.json', 'duplicate strategy name'):
                    assert sum(name in message for message in messages) == 3, messages
                assert len(index_warnings.call_args_list) == 2
                assert 'invalid-foreign.json' in index_warnings.call_args_list[0].args[0]
                assert 'malformed.json' in index_warnings.call_args_list[1].args[0]
                result = handler.handler({'queryStringParameters': {
                    'algoritm': 'COMPOSITE', 'src': 'ARCHIVE_ROOT',
                    'dst': 'destination', 'token': 'test-token'}}, None)
                assert result['body'] == 'Success sync, ARCHIVE_ROOT destination!'
                launch.assert_called_once_with(
                    token='test-token', prepared_strategy=prepared, dst='destination')
                assert launch.return_value.method_calls == [call.run_sync()]
                # The saved tree remains usable if files vanish after preparation.
                for path in configs.rglob('*.json'):
                    path.unlink()
                assert create_strategy(prepared).build_plan(snapshot, context).target == target
            else:
                expected = {
                    'invalid-composite': ('third-file.json', 'components'),
                    'missing-child': ('ABSENT_CHILD', 'unsupported src'),
                    'invalid-index': ('unrelated-filename.json', 'reserve'),
                    'invalid-account': ('different-name.json', 'reserve'),
                }[scenario]
                try:
                    # No token supplied: config failure must precede even credential lookup.
                    handler.handler({'queryStringParameters': {
                        'algoritm': 'COMPOSITE', 'src': 'ARCHIVE_ROOT'}}, None)
                except ValueError as error:
                    assert all(part in str(error) for part in expected), str(error)
                else:
                    raise AssertionError('invalid selected child accepted')
                launch.assert_not_called()
        trade.assert_not_called()

    for name, module in list(sys.modules.items()):
        if name == 'autorepeater' or name.startswith('autorepeater.'):
            assert Path(module.__file__).resolve().is_relative_to(extracted), name
    for blocked in (client, secure, insecure, connect, network):
        blocked.assert_not_called()
print('nested archive validation completed')
"""


@pytest.mark.parametrize('location', ['repository', 'package', 'source_tree', 'extracted', 'venv'])
def test_archive_probe_rejects_application_source_paths(tmp_path, location):
    """Allow dependency directories, not the repository, its package or entire venv."""
    repository = Path(__file__).resolve().parents[1]
    extracted = tmp_path / 'extracted'
    source_tree = tmp_path / 'source'
    working = tmp_path / 'working'
    for directory in (extracted, source_tree, working):
        directory.mkdir()
    injected = {
        'repository': repository, 'package': repository / 'autorepeater',
        'source_tree': source_tree, 'extracted': extracted, 'venv': repository / '.venv',
    }[location]
    script = f'import sys\nsys.path.insert(0, {str(injected)!r})\n' + NESTED_ARCHIVE_PROBE
    probe = subprocess.run([
        sys.executable, '-I', '-c', script, str(extracted), str(repository), str(source_tree),
        'valid', 'absolute'], cwd=working, env={}, check=False,
        capture_output=True, text=True, timeout=30)
    assert probe.returncode != 0
    assert 'application source leaked onto sys.path' in probe.stderr


def assert_staging_parity(project_root, extracted, expected):
    """Any intermediate tree must have exactly the ZIP's members and bytes."""
    staging = project_root / 'build/yandex-function'
    if staging.exists():
        staged = {path.relative_to(staging).as_posix()
                  for path in staging.rglob('*') if path.is_file()}
        assert staged == expected
        for name in expected:
            assert (staging / name).read_bytes() == (extracted / name).read_bytes()


def damage_archive_child(project_root, scenario):
    """Corrupt only a selected field, preserving the rest of each valid document."""
    configs = project_root / 'autorepeater/configs/new/deep'
    changes = {
        'invalid-composite': ('composites/third-file.json',
                              {'component_drift_limit': '0.20',
                                  'name': 'ARCHIVE_LEAF', 'components': []}),
        'missing-child': ('composites/third-file.json',
                          {'component_drift_limit': '0.20', 'name': 'ARCHIVE_LEAF', 'components': [
                              {'algoritm': 'INDEX', 'src': 'ABSENT_CHILD', 'weight': '1'}]}),
        'invalid-index': ('indexes/unrelated-filename.json',
                          {'name': 'ARCHIVE_INDEX', 'reserve': 1}),
        'invalid-account': ('settings/different-name.json', {'reserve': 1}),
    }
    if scenario in changes:
        relative, document = changes[scenario]
        path = configs / relative
        original = json.loads(path.read_text(encoding='utf-8'))
        path.write_text(json.dumps(dict(original, **document)), encoding='utf-8')


@pytest.mark.parametrize('path_style', ['relative', 'absolute'])
@pytest.mark.parametrize('scenario', [
    'valid', 'invalid-composite', 'missing-child', 'invalid-index', 'invalid-account'])
def test_nested_application_archive(application_bundle, tmp_path, scenario, path_style):
    """Make preserves the bundle path and extracted JSON trees work without the repo."""
    project_root, expected = application_bundle
    damage_archive_child(project_root, scenario)
    build = subprocess.run(['make', 'claude-yandex-archive'], cwd=project_root,
                           check=False, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stdout + build.stderr
    extracted = tmp_path / 'extracted'
    with ZipFile(project_root / 'build/yandex-function.zip') as archive:
        assert len(archive.namelist()) == len(expected)
        assert set(archive.namelist()) == expected
        for name in expected:
            assert archive.read(name) == (project_root / name).read_bytes()
        archive.extractall(extracted)
    assert_staging_parity(project_root, extracted, expected)
    working = tmp_path / 'elsewhere'
    working.mkdir()
    probe = subprocess.run(
        [sys.executable, '-I', '-c', NESTED_ARCHIVE_PROBE, str(extracted),
         str(Path(__file__).resolve().parents[1]), str(project_root), scenario, path_style],
        cwd=working, env={}, check=False, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.rstrip().endswith('nested archive validation completed')


INDEPENDENT_MODULE = '''
from decimal import Decimal
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_plan import AllocationProfile, StrategyDecision, StrategyPlan, TradeMode

def prepare_source(src, context):
    if src != 'independent-source':
        raise ValueError('unexpected independent source')
    return src

class IndependentStrategy:
    def __init__(self, prepared):
        self.prepared = prepared
    def load_snapshot(self, data):
        return data.get_portfolio('source')
    def allocation_profile(self, snapshot):
        return AllocationProfile({'stock': Decimal(1)}, {'stock': Decimal(10)}, Decimal(0))
    def build_plan(self, snapshot, context):
        return StrategyPlan(context.path, context.budget,
            TargetPortfolio({'stock': context.budget / 10}, {'stock': Decimal(10)}),
            Decimal(0), StrategyDecision(TradeMode.BUY_ONLY, False, 'independent', None, Decimal(0)),
            (), {}, context.positions)
    def event_accounts(self, dst_account_id):
        return (dst_account_id,)
    def should_rebalance(self, event, dst_account_id):
        return True
'''


INDEPENDENT_PROBE = '''
import sys
from decimal import Decimal
from unittest.mock import Mock, patch
sys.path.insert(0, sys.argv[1])
with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
        patch('grpc.secure_channel', side_effect=AssertionError('gRPC forbidden')):
    import main
    import handler
    from autorepeater import runner
    from autorepeater.strategies import prepare_strategy, create_strategy
    from autorepeater.execution import ExecutionReceipt
    from autorepeater.execution_data import ExecutionSnapshot, TradeRules
    from autorepeater.strategy_data import PortfolioSnapshot
    prepared = prepare_strategy('ARCHIVE_INDEPENDENT', 'independent-source')
    strategy = create_strategy(prepared)
    assert strategy.prepared == 'independent-source'
    data = Mock(spec_set=['begin_snapshot', 'get_portfolio', 'position_events'])
    data.get_portfolio.return_value = PortfolioSnapshot(())
    data.position_events.side_effect = RuntimeError('finite offline stream')
    execution = Mock(spec_set=['get_destination', 'get_trade_rules'])
    execution.get_destination.return_value = ExecutionSnapshot(
        PortfolioSnapshot(()), Decimal(100), {}, {}, {'rub': Decimal(100)}, {}, (), True)
    execution.get_trade_rules.return_value = {
        'stock': TradeRules(1, 'rub', True, True, Decimal(100), 100, 100)}
    executor = Mock(spec_set=['submit_order'])
    executor.submit_order.side_effect = lambda account, intent: ExecutionReceipt(
        intent.uid, intent.side, 'offline', 'FILL', intent.lots, intent.lots, {})
    with patch.object(runner, 'Client', autospec=True), \
            patch.object(runner, 'TInvestStrategyData', return_value=data), \
            patch.object(runner, 'TInvestExecutionData', return_value=execution), \
            patch.object(runner, 'TInvestOrderExecutor', return_value=executor), \
            patch.object(runner, 'print_all_portfolio'), \
            patch.object(runner, 'configure_local_logging'), \
            patch('autorepeater.serverless.configure_yc_logging'), \
            patch.dict('os.environ', {'INVEST_TOKEN': 'offline'}):
        sys.argv = ['main.py', '--algoritm', 'ARCHIVE_INDEPENDENT', '-s', 'independent-source',
                    '-d', 'dst', '--debug']
        try:
            main.main()
        except RuntimeError as error:
            assert str(error) == 'finite offline stream'
        else:
            raise AssertionError('CLI did not open its event stream')
        executor.submit_order.assert_not_called()
        result = handler.handler({'queryStringParameters': {
            'algoritm': 'ARCHIVE_INDEPENDENT', 'src': 'independent-source', 'dst': 'dst'}}, None)
        assert result['body'] == 'Success sync, independent-source dst!'
        executor.submit_order.assert_called_once()
        assert executor.submit_order.call_args.args[1].lots == 10
    assert data.get_portfolio.call_count == 2
print('independent archive launch completed')
'''


def test_independent_algorithm_needs_only_module_and_registration_in_archive(
        application_bundle, tmp_path):
    """A new algorithm runs from an extracted bundle without changes to orchestration."""
    root, expected = application_bundle
    module = root / 'autorepeater/archive_independent.py'
    module.write_text(INDEPENDENT_MODULE, encoding='utf-8')
    registry = root / 'autorepeater/strategies.py'
    registry.write_text(registry.read_text(encoding='utf-8') + '''
from autorepeater.archive_independent import prepare_source, IndependentStrategy
register_algorithm('ARCHIVE_INDEPENDENT', AlgorithmDefinition(prepare_source, IndependentStrategy))
''', encoding='utf-8')
    expected.add('autorepeater/archive_independent.py')
    extracted = tmp_path / 'independent-extracted'
    with ZipFile(builder.build_archive(root)) as archive:
        assert set(archive.namelist()) == expected
        archive.extractall(extracted)
    working = tmp_path / 'independent-elsewhere'
    working.mkdir()
    probe = subprocess.run([sys.executable, '-I', '-c', INDEPENDENT_PROBE, str(extracted)],
                           cwd=working, env={}, check=False, capture_output=True,
                           text=True, timeout=30)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.rstrip().endswith('independent archive launch completed')
