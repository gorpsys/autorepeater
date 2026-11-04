"""Catalog mechanics preserve schema-specific parsing, warnings and strict loads."""
import subprocess
import sys
import json
from pathlib import Path
from decimal import Decimal, InvalidOperation
from unittest.mock import call, create_autospec, patch

import pytest

from autorepeater import account_config, composite_config, config_json, index_config, reporting
from autorepeater.index_config import AllocationDriftRange
from autorepeater.config_catalog import Candidate, discover_candidates, select_candidate
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
    read_error = OSError('cannot read')
    validation_error = ValueError('invalid body')
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           side_effect=[document, invalid, read_error])
    validate = create_autospec(account_config.validate_account_config, spec_set=True,
                               side_effect=[config, validation_error])
    candidates = discover_candidates(paths, read, validate)
    assert read.call_args_list == [call(path) for path in paths]
    assert validate.call_args_list == [call(document), call(invalid)]
    assert [(item.path, item.name, item.config) for item in candidates] == [
        (paths[0], 'GOOD', config), (paths[1], 'BAD', None), (paths[2], None, None)]
    assert candidates[0].error is None
    assert candidates[0].cause is None
    assert candidates[1].cause is validation_error
    assert candidates[2].cause is read_error
    assert str(candidates[1].error) == 'bad.json (BAD): invalid body'
    assert str(candidates[2].error) == 'broken.json: cannot read'


def test_selected_error_keeps_original_cause_and_nested_chain():
    """Deferred selection raises the same diagnostic with the exact caught cause."""
    path = Path('selected.json')
    document = {'name': 'SELECTED'}
    original = ValueError('invalid body')
    nested = InvalidOperation('invalid decimal')
    original.__cause__ = nested
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           return_value=document)
    validate = create_autospec(account_config.validate_account_config, spec_set=True,
                               side_effect=original)
    warn = create_autospec(reporting.print_config_warning, spec_set=True)
    candidates = discover_candidates([path], read, validate)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.cause is original
    assert candidate.cause.__cause__ is nested
    assert candidate.error.__cause__ is None
    with pytest.raises(ValueError) as caught:
        select_candidate([candidate], 'SELECTED', warn)
    assert caught.value is candidate.error
    assert str(caught.value) == 'selected.json (SELECTED): invalid body'
    assert caught.value.__cause__ is original
    assert caught.value.__cause__.__cause__ is nested
    read.assert_called_once_with(path)
    validate.assert_called_once_with(document)
    warn.assert_not_called()


@pytest.mark.parametrize('original', [OSError('cannot read'), PermissionError('denied')])
def test_discovery_preserves_io_cause_without_guessing_name(original):
    """A reader's failure is retained without validation or filename-based names."""
    path = Path('SELECTED.json')
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           side_effect=original)
    validate = create_autospec(account_config.validate_account_config, spec_set=True)
    candidates = discover_candidates([path], read, validate)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name is None
    assert candidate.config is None
    assert candidate.cause is original
    assert str(candidate.error) == f'{path}: {original}'
    read.assert_called_once_with(path)
    validate.assert_not_called()


@pytest.mark.parametrize('content, exception_type', [
    (b'{"name":"SELECTED",', json.JSONDecodeError),
    (b'{"name":"SELECTED","name":"SELECTED"}', ValueError),
    (b'\xff', UnicodeDecodeError),
])
def test_discovery_preserves_real_parse_cause(tmp_path, content, exception_type):
    """Syntax, duplicate keys and UTF-8 errors retain the reader's actual exception."""
    path = tmp_path / 'SELECTED.json'
    path.write_bytes(content)
    caught_errors = []

    def read_document(candidate_path):
        try:
            return config_json.read_config_document(candidate_path)
        except ValueError as error:
            caught_errors.append(error)
            raise

    read = create_autospec(config_json.read_config_document, spec_set=True,
                           side_effect=read_document)
    validate = create_autospec(account_config.validate_account_config, spec_set=True)
    candidates = discover_candidates([path], read, validate)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert len(caught_errors) == 1
    assert candidate.cause is caught_errors[0]
    assert candidate.cause.__class__ is exception_type
    assert candidate.name is None
    assert candidate.config is None
    read.assert_called_once_with(path)
    validate.assert_not_called()


def test_discovery_does_not_leak_cause_into_next_success():
    """Failure followed by success resets diagnostic state for each candidate."""
    paths = [Path('bad.json'), Path('good.json')]
    original = ValueError('invalid body')
    config = object()
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           side_effect=[{'name': 'BAD'}, {'name': 'GOOD'}])
    validate = create_autospec(account_config.validate_account_config, spec_set=True,
                               side_effect=[original, config])
    candidates = discover_candidates(paths, read, validate)
    assert len(candidates) == 2
    bad, good = candidates[0], candidates[1]
    warn = create_autospec(reporting.print_config_warning, spec_set=True)
    assert bad.cause is original
    assert good.cause is None
    assert good.error is None
    assert select_candidate([bad, good], 'GOOD', warn) is config
    warn.assert_called_once_with('bad.json (BAD): invalid body')
    assert read.call_args_list == [call(path) for path in paths]
    assert validate.call_args_list == [call({'name': 'BAD'}), call({'name': 'GOOD'})]


@pytest.mark.parametrize('selection', ['MISSING', 'SELECTED'])
def test_unidentified_or_duplicate_selection_has_no_arbitrary_cause(selection):
    """Missing and ambiguous selections never borrow one candidate's exception."""
    original = ValueError('invalid body')
    paths = [Path('bad.json'), Path('valid.json'), Path('broken.json')]
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           side_effect=[{'name': 'SELECTED'}, {'name': 'SELECTED'},
                                        OSError('cannot read')])
    validate = create_autospec(account_config.validate_account_config, spec_set=True,
                               side_effect=[original, object()])
    candidates = discover_candidates(paths, read, validate)
    warn = create_autospec(reporting.print_config_warning, spec_set=True)
    expected_type = UnsupportedSourceError if selection == 'MISSING' else ValueError
    with pytest.raises(expected_type) as caught:
        select_candidate(candidates, selection, warn)
    assert caught.type is expected_type
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if selection == 'MISSING':
        assert str(caught.value) == (
            'unsupported src: MISSING; problematic files: bad.json, broken.json')
        assert warn.call_args_list == [
            call('bad.json (SELECTED): invalid body'), call('broken.json: cannot read'),
            call('duplicate strategy name: SELECTED (bad.json, valid.json)')]
    else:
        assert str(caught.value) == 'duplicate strategy name: SELECTED (bad.json, valid.json)'
        warn.assert_called_once_with('broken.json: cannot read')


def test_legacy_candidate_keeps_error_without_manufactured_cause():
    """Four positional fields keep working and selection raises the same error."""
    original = ValueError('legacy diagnostic')
    candidate = Candidate(Path('legacy.json'), 'SELECTED', None, original)
    warn = create_autospec(reporting.print_config_warning, spec_set=True)
    assert candidate.cause is None
    with pytest.raises(ValueError) as caught:
        select_candidate([candidate], 'SELECTED', warn)
    assert caught.value is original
    assert caught.value.__cause__ is None
    warn.assert_not_called()


@pytest.mark.parametrize('stage', ['read', 'validate'])
@pytest.mark.parametrize('exception_type', [TypeError, RuntimeError, KeyboardInterrupt, SystemExit])
def test_discovery_does_not_catch_programming_errors_or_stop_signals(stage, exception_type):
    """Only the existing OSError/ValueError boundary is deferred by the catalog."""
    original = exception_type('stop')
    document = {'name': 'SELECTED'}
    read = create_autospec(account_config.read_account_document, spec_set=True,
                           return_value=document)
    validate = create_autospec(account_config.validate_account_config, spec_set=True)
    (read if stage == 'read' else validate).side_effect = original
    with pytest.raises(exception_type) as caught:
        discover_candidates([Path('selected.json')], read, validate)
    assert caught.value is original
    if stage == 'read':
        validate.assert_not_called()
    else:
        validate.assert_called_once_with(document)


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


def test_index_implicit_schema_failure_keeps_original_nested_cause():
    """A single invalid body fails with the legacy wrapper and exact schema cause."""
    path = Path('selected.json')
    document = {'name': 'SELECTED'}
    original = ValueError('invalid decimal string')
    nested = InvalidOperation('invalid decimal')
    original.__cause__ = nested
    with patch.object(index_config, '_index_paths', autospec=True, spec_set=True,
                      return_value=[path]), \
            patch.object(index_config, 'read_index_document', autospec=True, spec_set=True,
                         return_value=document) as read, \
            patch.object(index_config, 'validate_index_config', autospec=True, spec_set=True,
                         side_effect=original) as validate, \
            patch.object(reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
        with pytest.raises(UnsupportedSourceError) as caught:
            index_config.select_index_config()
    assert caught.type is UnsupportedSourceError
    assert str(caught.value) == '--src must name a configured index strategy'
    assert caught.value.__cause__ is original
    assert caught.value.__cause__.__cause__ is nested
    read.assert_called_once_with(path)
    validate.assert_called_once_with(document)
    warn.assert_not_called()


@pytest.mark.parametrize('original', [OSError('cannot read'), PermissionError('denied')])
def test_index_implicit_io_failure_keeps_original_cause(original):
    """A single unreadable candidate needs no inferred name to expose its cause."""
    path = Path('SELECTED.json')
    with patch.object(index_config, '_index_paths', autospec=True, spec_set=True,
                      return_value=[path]), \
            patch.object(index_config, 'read_index_document', autospec=True, spec_set=True,
                         side_effect=original) as read, \
            patch.object(index_config, 'validate_index_config',
                         autospec=True, spec_set=True) as validate, \
            patch.object(reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
        with pytest.raises(UnsupportedSourceError) as caught:
            index_config.select_index_config()
    assert caught.type is UnsupportedSourceError
    assert str(caught.value) == '--src must name a configured index strategy'
    assert caught.value.__cause__ is original
    read.assert_called_once_with(path)
    validate.assert_not_called()
    warn.assert_not_called()


@pytest.mark.parametrize('content, exception_type', [
    ('{"name":"SELECTED",', json.JSONDecodeError),
    ('{"name":"SELECTED","name":"SELECTED"}', ValueError),
])
def test_index_implicit_parse_failure_keeps_real_cause(tmp_path, content, exception_type):
    """Malformed and duplicate-key JSON fail through the actual shared reader."""
    path = tmp_path / 'SELECTED.json'
    path.write_text(content, encoding='utf-8')
    caught_errors = []

    def read_document(candidate_path):
        try:
            return config_json.read_config_document(candidate_path)
        except ValueError as error:
            caught_errors.append(error)
            raise

    with patch.object(index_config, '_index_paths', autospec=True, spec_set=True,
                      return_value=[path]), \
            patch.object(index_config, 'read_index_document', autospec=True, spec_set=True,
                         side_effect=read_document) as read, \
            patch.object(index_config, 'validate_index_config',
                         autospec=True, spec_set=True) as validate, \
            patch.object(reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
        with pytest.raises(UnsupportedSourceError) as caught:
            index_config.select_index_config()
    assert len(caught_errors) == 1
    assert caught_errors[0].__class__ is exception_type
    assert caught.type is UnsupportedSourceError
    assert str(caught.value) == '--src must name a configured index strategy'
    assert caught.value.__cause__ is caught_errors[0]
    read.assert_called_once_with(path)
    validate.assert_not_called()
    warn.assert_not_called()


def test_index_implicit_legacy_failure_has_no_manufactured_cause():
    """A legacy candidate's diagnostic is never substituted for its absent cause."""
    diagnostic = ValueError('legacy diagnostic')
    candidate = Candidate(Path('legacy.json'), None, None, diagnostic)
    with patch.object(index_config, '_discover_candidates', autospec=True, spec_set=True,
                      return_value=[candidate]) as discover, \
            patch.object(reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
        with pytest.raises(UnsupportedSourceError) as caught:
            index_config.select_index_config()
    assert caught.type is UnsupportedSourceError
    assert str(caught.value) == '--src must name a configured index strategy'
    assert caught.value.__cause__ is None
    discover.assert_called_once_with()
    warn.assert_not_called()


@pytest.mark.parametrize('mode', ['empty', 'good_bad', 'bad_bad', 'duplicate'])
def test_index_implicit_ambiguity_has_no_arbitrary_cause(mode):
    """An ambiguous catalog never auto-selects GOOD or borrows a bad file's cause."""
    good = Candidate(Path('good.json'), 'GOOD', object(), None)
    bad = Candidate(Path('bad.json'), 'GOOD' if mode == 'duplicate' else None,
                    None, ValueError('bad diagnostic'), ValueError('original failure'))
    other = Candidate(Path('other.json'), 'OTHER', None,
                      ValueError('other diagnostic'), OSError('cannot read'))
    candidates = {'empty': [], 'good_bad': [good, bad], 'bad_bad': [bad, other],
                  'duplicate': [good, bad]}[mode]
    with patch.object(index_config, '_discover_candidates', autospec=True, spec_set=True,
                      return_value=candidates) as discover, \
            patch.object(reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
        with pytest.raises(UnsupportedSourceError) as caught:
            index_config.select_index_config()
    assert caught.type is UnsupportedSourceError
    assert str(caught.value) == '--src must name a configured index strategy'
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    discover.assert_called_once_with()
    warn.assert_not_called()


def test_index_strict_load_is_unchanged():
    """Load-all stays fatal on duplicates and body errors without warnings."""
    config = index_config.IndexConfig('GOOD', None, [], None,
                                      allocation_drift_limits=(
            AllocationDriftRange(Decimal('0'), None, False, Decimal('0')),))
    paths = [Path('first.json'), Path('second.json')]
    with patch.object(index_config, '_index_paths', autospec=True, spec_set=True,
                      return_value=paths), \
            patch.object(index_config, 'load_index_config', autospec=True, spec_set=True,
                         return_value=config) as load, \
            patch.object(index_config.reporting, 'print_index_config_warning',
                         autospec=True, spec_set=True) as warn:
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


@pytest.mark.parametrize('instrument', [False, True])
def test_index_strict_catalog_load_rejects_unknown_fields(tmp_path, monkeypatch, instrument):
    """The load-all entry point uses the same closed root and instrument schema."""
    payload = index_config.read_index_document(
        Path(index_config.__file__).parent / 'configs/gold.json')
    record = payload['instruments'][0] if instrument else payload
    record['description'] = 'unsupported metadata'
    (tmp_path / 'index.json').write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path))
    with patch.object(index_config.reporting, 'print_index_config_warning',
                      autospec=True, spec_set=True) as warn:
        with pytest.raises(ValueError, match='unknown fields: description'):
            index_config.load_index_configs()
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
