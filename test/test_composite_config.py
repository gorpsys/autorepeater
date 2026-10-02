"""Pure COMPOSITE schema and isolated, exact-name catalog selection."""
import json
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import call, patch

import pytest

from autorepeater import composite_config
from autorepeater.strategy_contract import UnsupportedSourceError


def document(name='GOOD', weights=('0.6', '0.3')):
    """Small compositions keep their declared order and unallocated remainder."""
    return {'name': name, 'components': [
        {'algoritm': 'INDEX', 'src': f'SOURCE{position}', 'weight': weight}
        for position, weight in enumerate(weights)
    ]}


@pytest.fixture(name='documents')
def fixture_documents(tmp_path, monkeypatch):
    """Write catalogs only into a controlled temporary directory."""
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(tmp_path))

    def write(filename, name='GOOD', **changes):
        path = tmp_path / filename
        path.write_text(json.dumps({**document(name), **changes}), encoding='utf-8')
        return path

    return write


@pytest.mark.parametrize('name', ['GOOD', '00123', 'INDEX', 'COMPOSITE', 'good'])
@pytest.mark.parametrize('weights', [('0.6', '0.3'), ('0.6', '0.4'), ('1',), ('2E-28',)])
def test_valid_composite_schema(name, weights):
    """Exact names and finite positive shares survive without normalization."""
    config = composite_config.validate_composite_config(document(name, weights))
    assert isinstance(config, composite_config.CompositeConfig)
    assert config.name == name
    assert config.components == tuple(
        composite_config.CompositeComponent('INDEX', f'SOURCE{position}', Decimal(weight))
        for position, weight in enumerate(weights))


def test_component_references_are_opaque_ordered_and_repeatable():
    """Registry membership and source semantics belong to later preparation."""
    payload = document()
    payload['components'] = [
        {'algoritm': 'CUSTOM', 'src': ' source with spaces ', 'weight': '0.2'},
        {'algoritm': 'ACCOUNT', 'src': '00123', 'weight': '0.1'},
        {'algoritm': 'CUSTOM', 'src': ' source with spaces ', 'weight': '0.2'},
    ]
    with patch('builtins.open', side_effect=AssertionError('validator I/O')), \
            patch.object(Path, 'glob', side_effect=AssertionError('validator I/O')):
        config = composite_config.validate_composite_config(payload)
    assert [(item.algoritm, item.src, item.weight) for item in config.components] == [
        ('CUSTOM', ' source with spaces ', Decimal('0.2')),
        ('ACCOUNT', '00123', Decimal('0.1')),
        ('CUSTOM', ' source with spaces ', Decimal('0.2')),
    ]
    assert payload['components'][0]['weight'] == '0.2'


@pytest.mark.parametrize('precision', [28, 50])
def test_rounded_weight_sum_equal_to_one_is_accepted(precision):
    """Use ordinary ordered addition at precision 28, even for longer operands."""
    weights = ('0.9999999999999999999999999999', '2E-28')
    with localcontext() as context:
        context.prec = precision
        config = composite_config.validate_composite_config(document(weights=weights))
        assert context.prec == precision
    assert tuple(item.weight for item in config.components) == tuple(map(Decimal, weights))


def test_weight_sum_uses_component_order():
    """Rounding each addition preserves the specified order rather than sorting."""
    weights = ('1', '4E-28', '4E-28')
    assert composite_config.validate_composite_config(document(weights=weights))
    with pytest.raises(ValueError, match='sum.*weight|weight.*sum'):
        composite_config.validate_composite_config(document(weights=tuple(reversed(weights))))


@pytest.mark.parametrize('payload', [None, [], 'GOOD', 1, True])
def test_invalid_root(payload):
    """The schema requires a JSON object."""
    with pytest.raises(ValueError, match='composite config'):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('name', [
    None, '', ' ', ' GOOD', 'GOOD ', 'GO OD', 'GO\tOD', 123, True, [],
])
def test_invalid_name(name):
    """No coercion, trimming, or whitespace is allowed in catalog names."""
    with pytest.raises(ValueError, match='name'):
        composite_config.validate_composite_config(document(name))


@pytest.mark.parametrize('components', [None, [], {}, 'INDEX', 1, True, [None], [[]], ['INDEX']])
def test_invalid_components(components):
    """Components must be a nonempty list of objects."""
    with pytest.raises(ValueError, match='components'):
        composite_config.validate_composite_config({'name': 'GOOD', 'components': components})


@pytest.mark.parametrize('field', ['algoritm', 'src', 'weight'])
def test_missing_component_field(field):
    """Each reference needs all three fields; no defaults fill omissions."""
    payload = document(weights=('1',))
    del payload['components'][0][field]
    with pytest.raises(ValueError, match=rf'components\[0\].*{field}'):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('field', ['name', 'components'])
def test_missing_root_field(field):
    """Both root fields are required."""
    payload = document()
    del payload[field]
    with pytest.raises(ValueError, match=field):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('algoritm', [None, '', ' ', ' INDEX', 'INDEX ', 'IN DEX', 1, True, []])
def test_invalid_component_algorithm(algoritm):
    """Algorithm spelling is an exact name without whitespace."""
    payload = document(weights=('1',))
    payload['components'][0]['algoritm'] = algoritm
    with pytest.raises(ValueError, match=r'components\[0\].algoritm'):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('src', [None, '', ' ', 123, True, [], {}])
def test_invalid_component_source(src):
    """Source must be a nonempty string; algorithms later validate its meaning."""
    payload = document(weights=('1',))
    payload['components'][0]['src'] = src
    with pytest.raises(ValueError, match=r'components\[0\].src'):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('weight', [
    None, True, False, 0, 0.3, [], {}, '', 'invalid', 'NaN', 'sNaN',
    'Infinity', '-Infinity', '0', '-0', '-0.1', '1.0000000000000000000000000001',
])
def test_invalid_component_weight(weight):
    """Only finite Decimal strings in (0, 1] are accepted, before rounding sums."""
    with pytest.raises(ValueError, match=r'components\[0\].weight'):
        composite_config.validate_composite_config(document(weights=(weight,)))


@pytest.mark.parametrize('weights', [
    ('0.6', '0.5'), ('1', '1E-27'), ('0.9999999999999999999999999999', '7E-28'),
])
def test_weight_sum_over_one(weights):
    """The precision-28 result above one fails without normalization or epsilon."""
    with pytest.raises(ValueError, match='sum.*weight|weight.*sum'):
        composite_config.validate_composite_config(document(weights=weights))


@pytest.mark.parametrize('field', ['reserve', 'threshold', 'debug', 'dst', 'token', 'extra'])
@pytest.mark.parametrize('level', ['root', 'component'])
def test_unknown_keys_rejected(field, level):
    """Execution parameters and arbitrary extensions are not part of this schema."""
    payload = document()
    target = payload if level == 'root' else payload['components'][0]
    target[field] = '0.01'
    with pytest.raises(ValueError, match=f'unknown.*{field}'):
        composite_config.validate_composite_config(payload)


@pytest.mark.parametrize('name', ['GOOD', '00123', 'INDEX', 'COMPOSITE'])
def test_exact_name_independent_of_filename(documents, name, caplog):
    """Selection uses the JSON name, not the file's stem or another namespace."""
    path = documents('unrelated.json', name)
    config = composite_config.select_composite_config(name)
    assert config.name == name
    assert composite_config.load_composite_config(path) == config
    assert not caplog.records


@pytest.mark.parametrize('name', [None, '', ' ', 'good', ' GOOD', 'GOOD ', 'unrelated', 123, []])
def test_missing_or_inexact_selection_never_falls_back(documents, name):
    """A single valid neighbor cannot substitute for an absent exact name."""
    documents('unrelated.json')
    with pytest.raises(UnsupportedSourceError, match='src'):
        composite_config.select_composite_config(name)


def test_foreign_errors_are_isolated_with_one_read_and_validation(documents, tmp_path, caplog):
    """Invalid foreign bodies and unidentified broken JSON cannot block GOOD."""
    good = documents('renamed.json')
    bad = documents('bad.json', 'BAD', components=[])
    broken = tmp_path / 'broken.json'
    broken.write_text('{"name": "GOOD",', encoding='utf-8')
    with patch.object(composite_config, 'read_composite_document',
                      wraps=composite_config.read_composite_document) as read, \
            patch.object(composite_config, 'validate_composite_config',
                         wraps=composite_config.validate_composite_config) as validate:
        config = composite_config.select_composite_config('GOOD')
    assert config.name == 'GOOD'
    assert read.call_args_list == [call(path) for path in sorted([good, bad, broken])]
    assert validate.call_count == 2
    assert len(caplog.records) == 2
    assert all(record.levelname == 'WARNING' for record in caplog.records)
    assert any(str(bad) in record.message and 'components' in record.message
               and 'BAD' in record.message
               for record in caplog.records)
    assert any(str(broken) in record.message for record in caplog.records)


def test_selected_schema_error_fails_with_path_and_name(documents, caplog):
    """A selected invalid body is fatal, even with a valid neighbor."""
    documents('good.json')
    bad = documents('bad.json', 'BAD', components=[])
    with pytest.raises(ValueError, match='components') as error:
        composite_config.select_composite_config('BAD')
    assert str(bad) in str(error.value)
    assert 'BAD' in str(error.value)
    assert not caplog.records


@pytest.mark.parametrize('invalid_second', [False, True])
def test_selected_duplicate_counts_invalid_body(documents, invalid_second):
    """A readable name participates in duplicates even if its body is invalid."""
    first = documents('first.json')
    second = documents('second.json', **({'components': []} if invalid_second else {}))
    with pytest.raises(ValueError, match='duplicate.*GOOD') as error:
        composite_config.select_composite_config('GOOD')
    assert str(first) in str(error.value) and str(second) in str(error.value)


def test_foreign_duplicates_warn_without_blocking_selection(documents, caplog):
    """Duplicate foreign names warn once with both paths."""
    documents('good.json')
    first = documents('first.json', 'OTHER')
    second = documents('second.json', 'OTHER')
    assert composite_config.select_composite_config('GOOD').name == 'GOOD'
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert 'duplicate' in message and 'OTHER' in message
    assert str(first) in message and str(second) in message


def test_previously_warned_foreign_error_fails_when_selected(documents, caplog):
    """A previous warning cannot turn later selection of that file into success."""
    documents('good.json')
    documents('bad.json', 'BAD', components=[])
    assert composite_config.select_composite_config('GOOD').name == 'GOOD'
    assert len(caplog.records) == 1
    with pytest.raises(ValueError, match='components'):
        composite_config.select_composite_config('BAD')


@pytest.mark.parametrize('payload', ['{"name": "MISSING",', '', '[]', '{"name": 123}'])
def test_unidentified_invalid_json_is_not_recovered(documents, tmp_path, caplog, payload):
    """Neither raw text nor filenames recover a name from broken JSON."""
    documents('good.json')
    broken = tmp_path / 'MISSING.json'
    broken.write_text(payload, encoding='utf-8')
    with pytest.raises(UnsupportedSourceError, match='unsupported src: MISSING') as error:
        composite_config.select_composite_config('MISSING')
    assert str(broken) in str(error.value)
    assert len(caplog.records) == 1
    assert str(broken) in caplog.records[0].message


def test_only_immediate_nonhidden_json_files_are_discovered(documents, tmp_path, caplog):
    """Hidden files, nested documents and other suffixes do not enter the catalog."""
    good = documents('good.json')
    for filename in ('.hidden.json', 'other.txt', 'upper.JSON'):
        (tmp_path / filename).write_text('{', encoding='utf-8')
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'good.json').write_text('{', encoding='utf-8')
    with patch.object(composite_config, 'read_composite_document',
                      wraps=composite_config.read_composite_document) as read:
        assert composite_config.select_composite_config('GOOD').name == 'GOOD'
    read.assert_called_once_with(good)
    assert not caplog.records


@pytest.mark.parametrize('configured', ['', ' ', 'missing', 'empty', 'file.json', 'hidden-only'])
def test_invalid_directory_is_explicit_without_fallback(tmp_path, monkeypatch, configured):
    """Empty paths and missing, empty, file or hidden-only catalogs fail."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'file.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'empty').mkdir()
    hidden = tmp_path / 'hidden-only'
    hidden.mkdir()
    (hidden / '.ignored.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', configured)
    with pytest.raises(ValueError, match='composite config'):
        composite_config.select_composite_config('GOOD')


def test_relative_directory_uses_cwd_and_selection_is_not_cached(tmp_path, monkeypatch):
    """New calls see file changes; a previously returned DTO remains unchanged."""
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / 'catalog'
    catalog.mkdir()
    path = catalog / 'one.json'
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', 'catalog')
    path.write_text(json.dumps(document(weights=('0.5',))), encoding='utf-8')
    first = composite_config.select_composite_config('GOOD')
    path.write_text(json.dumps(document(weights=('0.8',))), encoding='utf-8')
    assert composite_config.select_composite_config('GOOD').components[0].weight == Decimal('0.8')
    assert first.components[0].weight == Decimal('0.5')


def test_default_directory_is_relative_to_module(tmp_path, monkeypatch):
    """Use the bundled location from any cwd, without requiring stage-7 assets."""
    module = tmp_path / 'package' / 'composite_config.py'
    catalog = module.parent / 'configs' / 'composite'
    catalog.mkdir(parents=True)
    (catalog / 'one.json').write_text(json.dumps(document()), encoding='utf-8')
    monkeypatch.setattr(composite_config, '__file__', str(module))
    monkeypatch.delenv('COMPOSITE_CONFIG_DIR', raising=False)
    monkeypatch.chdir(tmp_path)
    assert composite_config.select_composite_config('GOOD').name == 'GOOD'


def test_missing_default_directory_fails(tmp_path, monkeypatch):
    """No fallback invents a built-in composition before its assets exist."""
    monkeypatch.setattr(composite_config, '__file__', str(tmp_path / 'composite_config.py'))
    monkeypatch.delenv('COMPOSITE_CONFIG_DIR', raising=False)
    with pytest.raises(ValueError, match='directory.*does not exist') as error:
        composite_config.select_composite_config('GOOD')
    assert str(tmp_path / 'configs/composite') in str(error.value)


def test_composite_catalog_is_independent_of_other_paths(documents, monkeypatch):
    """INDEX and ACCOUNT paths cannot affect COMPOSITE schema selection."""
    documents('one.json')
    for variable in ('INDEX_CONFIG_DIR', 'IMOEX_CONFIG_PATH', 'ACCOUNT_CONFIG_PATH'):
        monkeypatch.setenv(variable, '/nonexistent-foreign-config')
    with patch('autorepeater.index_config.read_index_document',
               side_effect=AssertionError('INDEX read')), \
            patch('autorepeater.account_config.load_account_config',
                  side_effect=AssertionError('ACCOUNT read')):
        assert composite_config.select_composite_config('GOOD').name == 'GOOD'


def test_unreadable_document_does_not_infer_name(documents, caplog):
    """A read failure reports its reason and path without guessing a name."""
    path = documents('one.json')
    with patch('builtins.open', side_effect=PermissionError('cannot read')):
        with pytest.raises(UnsupportedSourceError, match='unsupported src: GOOD') as error:
            composite_config.select_composite_config('GOOD')
    assert str(path) in str(error.value)
    assert len(caplog.records) == 1
    assert str(path) in caplog.records[0].message and 'cannot read' in caplog.records[0].message


@pytest.mark.parametrize('operation', ['is_dir', 'glob'])
def test_unreadable_directory_has_explicit_error(documents, tmp_path, operation):
    """Directory access errors stay ValueError with the path and original cause."""
    documents('one.json')
    with patch.object(Path, operation, side_effect=PermissionError('cannot list')):
        with pytest.raises(ValueError, match='composite config directory') as error:
            composite_config.select_composite_config('GOOD')
    assert str(tmp_path) in str(error.value) and 'cannot list' in str(error.value)
    assert isinstance(error.value.__cause__, PermissionError)


@pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_json_numbers_fail_schema(literal):
    """Python's permissive JSON parser does not make non-string weights valid."""
    payload = json.loads('{"name":"GOOD","components":[{"algoritm":"INDEX",'
                         '"src":"IMOEX","weight":' + literal + '}]}')
    with pytest.raises(ValueError, match=r'components\[0\].weight'):
        composite_config.validate_composite_config(payload)


def test_import_has_no_io_sdk_strategy_registry_or_foreign_config_dependencies(
        guarded_import_script):
    """A fresh interpreter imports the schema while forbidden edges are blocked."""
    script = guarded_import_script(
        ('t_tech', 'grpc', 'autorepeater.strategies', 'autorepeater.account_strategy',
         'autorepeater.index_strategy', 'autorepeater.index_config',
         'autorepeater.account_config'), '''
from autorepeater.composite_config import validate_composite_config
assert validate_composite_config({'name': '00123', 'components': [
    {'algoritm': 'CUSTOM', 'src': 'opaque', 'weight': '1'}]}).name == '00123'
''')
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
