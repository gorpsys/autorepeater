"""Catalog mechanics preserve schema-specific parsing, warnings and strict loads."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from autorepeater import composite_config, index_config, reporting
from autorepeater.config_catalog import Candidate, discover_candidates
from autorepeater.strategy_contract import UnsupportedSourceError


def test_index_warning_delegates_to_current_common_reporter():
    """Patching the common reporter also intercepts the index compatibility entry."""
    with patch.object(reporting, 'print_config_warning', autospec=True) as warn:
        reporting.print_index_config_warning('invalid index document')
    warn.assert_called_once_with('invalid index document')


def test_discovery_reads_once_preserves_names_and_error_text():
    """Readable names survive invalid bodies; failed reads never infer a name."""
    paths = [Path('good.json'), Path('bad.json'), Path('broken.json')]
    document = {'name': 'GOOD'}
    invalid = {'name': 'BAD'}
    config = object()
    read = Mock(side_effect=[document, invalid, OSError('cannot read')])
    validate = Mock(side_effect=[config, ValueError('invalid body')])
    candidates = discover_candidates(paths, read, validate)
    assert read.call_args_list == [call(path) for path in paths]
    assert validate.call_args_list == [call(document), call(invalid)]
    assert [(item.path, item.name, item.config) for item in candidates] == [
        (paths[0], 'GOOD', config), (paths[1], 'BAD', None), (paths[2], None, None)]
    assert candidates[0].error is None
    assert str(candidates[1].error) == 'bad.json (BAD): invalid body'
    assert str(candidates[2].error) == 'broken.json: cannot read'


@pytest.mark.parametrize('schema', [index_config, composite_config])
def test_public_selection_keeps_exact_warnings_and_fatal_errors(schema):
    """Selected duplicates precede body errors; foreign diagnostics retain order."""
    good = object()
    bad_error = ValueError('bad.json (OTHER): invalid body')
    broken_error = ValueError('broken.json: invalid JSON')
    candidates = [
        Candidate(Path('good.json'), 'GOOD', good, None),
        Candidate(Path('bad.json'), 'OTHER', None, bad_error),
        Candidate(Path('other.json'), 'OTHER', object(), None),
        Candidate(Path('broken.json'), None, None, broken_error),
    ]
    warning = ('print_index_config_warning' if schema is index_config else 'print_config_warning')
    select = (schema.select_index_config if schema is index_config
              else schema.select_composite_config)
    duplicate = 'duplicate strategy name: OTHER (bad.json, other.json)'
    with patch.object(schema, '_discover_candidates', return_value=candidates), \
            patch.object(schema.reporting, warning) as warn:
        assert select('GOOD') is good
        expected_warnings = [call(str(bad_error)), call(str(broken_error)), call(duplicate)]
        assert warn.call_args_list == expected_warnings
        warn.reset_mock()
        with pytest.raises(UnsupportedSourceError) as error:
            select('MISSING')
        assert str(error.value) == (
            'unsupported src: MISSING; problematic files: bad.json, broken.json')
        assert warn.call_args_list == expected_warnings
        warn.reset_mock()
        with pytest.raises(ValueError) as error:
            select('OTHER')
        assert str(error.value) == duplicate
        assert warn.call_args_list == [call(str(broken_error))]
        candidates.pop(2)
        with pytest.raises(ValueError) as error:
            select('OTHER')
        assert error.value is bad_error


def test_index_implicit_selection_and_strict_load_are_unchanged():
    """Implicit calibration rejects ambiguity; load-all stays fatal without warnings."""
    config = index_config.IndexConfig('GOOD', None, [], None)
    paths = [Path('first.json'), Path('second.json')]
    with patch.object(index_config, '_discover_candidates', return_value=[
            Candidate(paths[0], 'GOOD', config, None)]):
        assert index_config.select_index_config() is config
    with patch.object(index_config, '_discover_candidates', return_value=[]):
        with pytest.raises(UnsupportedSourceError) as error:
            index_config.select_index_config()
        assert str(error.value) == '--src must name a configured index strategy'
    with patch.object(index_config, '_index_paths', return_value=paths), \
            patch.object(index_config, 'load_index_config', return_value=config) as load, \
            patch.object(index_config.reporting, 'print_index_config_warning') as warn:
        with pytest.raises(ValueError) as error:
            index_config.load_index_configs()
        assert str(error.value) == 'duplicate strategy name: GOOD'
        assert load.call_args_list == [call(path) for path in paths]
        warn.assert_not_called()
        load.reset_mock()
        load.side_effect = ValueError('invalid body')
        with pytest.raises(ValueError, match='^invalid body$'):
            index_config.load_index_configs()
        load.assert_called_once_with(paths[0])
        warn.assert_not_called()


def test_catalog_import_and_execution_without_sdk_schemas_or_io(guarded_import_script):
    """The helper imports no schema, registry or SDK and has no import-time I/O."""
    script = guarded_import_script(
        ('t_tech', 'grpc', 'autorepeater.strategies', 'autorepeater.runner',
         'autorepeater.repeater', 'autorepeater.tinvest_strategy_data',
         'autorepeater.index_config', 'autorepeater.composite_config',
         'autorepeater.account_config', 'autorepeater.account_strategy',
         'autorepeater.index_strategy',
         'autorepeater.composite_strategy'), '''
from autorepeater.config_catalog import discover_candidates, select_candidate
value = object()
candidates = discover_candidates([pathlib.Path('opaque.json')],
                                 lambda path: {'name': 'GOOD'}, lambda document: value)
assert select_candidate(candidates, 'GOOD', blocked_io) is value
''')
    result = subprocess.run([sys.executable, '-c', script],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
