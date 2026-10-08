"""Step-one schema migration, without enabling policy or SDK execution."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from autorepeater import account_config, composite_config, index_config, strategies
from autorepeater.composite_strategy import PreparedCompositeSource


def account_document(name='NAMED', **changes):
    """An exact catalog name is independent of the source account."""
    return {'name': name, 'source_account_id': '00123', 'reserve': '0.01',
            'allocation_drift_limit': '0.0092', **changes}


def drift_rows():
    """Adjacent half-open intervals with no finite upper endpoint."""
    return [
        {'budget_from': '0', 'budget_to': '100', 'upper_inclusive': False, 'limit': '0.05'},
        {'budget_from': '100', 'budget_to': None, 'upper_inclusive': False, 'limit': '0'},
    ]


def index_document():
    """Use real bundled metadata, replacing only the policy table."""
    path = Path(index_config.__file__).parent / 'configs/gold.json'
    return {**index_config.read_index_document(path),
            'allocation_drift_limits': drift_rows()}


@pytest.mark.parametrize('field', [
    'name', 'source_account_id', 'reserve', 'allocation_drift_limit',
])
def test_account_required_fields(field):
    """Every configuration field is mandatory before Client construction."""
    payload = account_document()
    del payload[field]
    with pytest.raises(ValueError, match=field):
        account_config.validate_account_config(payload)


@pytest.mark.parametrize('field', ['reserve', 'allocation_drift_limit'])
@pytest.mark.parametrize('value', [None, True, 0, 0.2, '', 'bad', 'NaN', 'sNaN',
                                   'Infinity', '-Infinity', '-0.1', '1'])
def test_account_policy_decimal_validation(field, value):
    """No coercion, nonfinite limits or fractions outside [0, 1)."""
    with pytest.raises(ValueError, match=field):
        account_config.validate_account_config(account_document(**{field: value}))


@pytest.mark.parametrize('field, value', [
    ('name', ''), ('name', ' NAMED'), ('name', 'NA\tMED'), ('name', True),
    ('source_account_id', ''), ('source_account_id', ' '),
    ('source_account_id', '123 '), ('source_account_id', ' 123'),
    ('source_account_id', '12 3'), ('source_account_id', '12\t3'),
    ('source_account_id', '123\n'), ('source_account_id', '12\u00a03'),
    ('source_account_id', 123), ('source_account_id', None),
    ('source_account_id', True), ('source_account_id', []), ('source_account_id', {}),
])
def test_account_names_and_source_strings(field, value):
    """Names and API identifiers are nonempty strings without whitespace or coercion."""
    with pytest.raises(ValueError, match=field):
        account_config.validate_account_config(account_document(**{field: value}))


@pytest.mark.parametrize('source', [
    '00123', 'a123', '１２３', '2a78f51a-42c0-4a3e-8f95-b9ef692d2da1',
    '2A78F51A-42C0-4A3E-8F95-B9EF692D2DA1',
])
def test_account_source_is_preserved_as_opaque_string(source):
    """The API owns identifier syntax; no integer or UUID normalization is applied."""
    config = account_config.validate_account_config(account_document(source_account_id=source))
    assert config.source_account_id == source


@pytest.mark.parametrize('name', ['NAMED', '00123', 'ACCOUNT'])
@pytest.mark.parametrize('source', ['00123', '2a78f51a-42c0-4a3e-8f95-b9ef692d2da1'])
def test_account_catalog_freezes_named_source(tmp_path, monkeypatch, name, source):
    """One document read; factories never reread or reinterpret the name as an ID."""
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(tmp_path))
    path = tmp_path / 'unrelated.json'
    path.write_text(json.dumps(account_document(name, source_account_id=source)), encoding='utf-8')
    with patch.object(account_config, 'read_account_document',
                      wraps=account_config.read_account_document) as read:
        prepared = strategies.prepare_strategy('ACCOUNT', name)
    read.assert_called_once_with(path)
    path.write_text(json.dumps(account_document(name, source_account_id='0004',
                                                allocation_drift_limit='0.02')), encoding='utf-8')
    with patch('builtins.open', side_effect=AssertionError('factory I/O')):
        strategy = strategies.create_strategy(prepared)
    assert strategy.src == source
    assert strategy.config.name == name
    assert strategy.config.allocation_drift_limit == Decimal('0.0092')
    assert strategies.create_strategy(strategies.prepare_strategy('ACCOUNT', name)).src == '0004'
    for missing in ('named', ' NAMED', 'unrelated', '0004'):
        with pytest.raises(ValueError, match='unsupported src'):
            strategies.prepare_strategy('ACCOUNT', missing)


@pytest.mark.parametrize('invalid_duplicate', [False, True])
def test_account_selected_duplicates_count_invalid_names(
        tmp_path, monkeypatch, invalid_duplicate):
    """Schema errors do not hide duplicate names."""
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(tmp_path))
    for position in range(2):
        payload = account_document()
        if position and invalid_duplicate:
            del payload['reserve']
        (tmp_path / f'{position}.json').write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate.*NAMED'):
        strategies.prepare_strategy('ACCOUNT', 'NAMED')


def test_account_foreign_errors_duplicates_and_discovery(tmp_path, monkeypatch, caplog):
    """Foreign errors warn; hidden/nested files and text do not enter discovery."""
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(tmp_path))
    good = tmp_path / 'good.json'
    good.write_text(json.dumps(account_document()), encoding='utf-8')
    for filename in ('a.json', 'b.json'):
        (tmp_path / filename).write_text(json.dumps(account_document('OTHER')), encoding='utf-8')
    (tmp_path / 'bad.json').write_text('{', encoding='utf-8')
    for filename in ('.hidden.json', 'file.txt'):
        (tmp_path / filename).write_text('{', encoding='utf-8')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/ignored.json').write_text('{', encoding='utf-8')
    with patch.object(account_config, 'read_account_document',
                      wraps=account_config.read_account_document) as read:
        prepared = strategies.prepare_strategy('ACCOUNT', 'NAMED')
        assert strategies.create_strategy(prepared).src == '00123'
    assert read.call_count == 4
    assert len(caplog.records) == 2
    assert all(record.levelname == 'WARNING' for record in caplog.records)
    assert 'duplicate' in caplog.text and 'bad.json' in caplog.text


@pytest.mark.parametrize('directory', ['', ' ', 'missing', 'empty', 'file.json'])
def test_account_invalid_catalog_without_fallback(tmp_path, monkeypatch, directory):
    """Bad configured catalogs never select the bundled source."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'empty').mkdir()
    (tmp_path / 'file.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', directory)
    with pytest.raises(ValueError, match='account config'):
        strategies.prepare_strategy('ACCOUNT', 'NAMED')


def test_account_catalog_path_conflict(tmp_path, monkeypatch):
    """Directory and compatible single-file override are mutually exclusive."""
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', 'unused.json')
    with patch('builtins.open', side_effect=AssertionError('conflict read')):
        with pytest.raises(ValueError, match='ACCOUNT_CONFIG_DIR.*ACCOUNT_CONFIG_PATH'):
            strategies.prepare_strategy('ACCOUNT', 'NAMED')


@pytest.mark.parametrize('operation', ['is_dir', 'glob'])
def test_account_directory_error_cause(tmp_path, monkeypatch, operation):
    """Directory I/O errors remain ValueError with the actual cause."""
    monkeypatch.setenv('ACCOUNT_CONFIG_DIR', str(tmp_path))
    with patch.object(Path, operation, side_effect=PermissionError('cannot list')):
        with pytest.raises(ValueError, match='account config directory') as caught:
            strategies.prepare_strategy('ACCOUNT', 'NAMED')
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize('budget, expected', [('0', '0.05'), ('99.99', '0.05'),
                                              ('100', '0'), ('1E20', '0')])
def test_index_ranges_preserve_half_open_boundaries(budget, expected):
    """The stored Decimal ranges uniquely cover zero, shared edges and large budgets."""
    config = index_config.validate_index_config(index_document())
    assert isinstance(config.allocation_drift_limits, tuple)
    matches = [row for row in config.allocation_drift_limits
               if row.budget_from <= Decimal(budget)
               and (row.budget_to is None or Decimal(budget) < row.budget_to)]
    assert len(matches) == 1 and matches[0].limit == Decimal(expected)
    assert matches[0].upper_inclusive is False


@pytest.mark.parametrize('table', [None, [], {}, True, 'table', [None], [[]]])
def test_index_required_nonempty_table(table):
    """All index compositions need a validated policy table."""
    with pytest.raises(ValueError, match='allocation_drift_limits'):
        index_config.validate_index_config({**index_document(), 'allocation_drift_limits': table})


def test_index_missing_table():
    """No implicit limit repairs an omitted table."""
    payload = index_document()
    del payload['allocation_drift_limits']
    with pytest.raises(ValueError, match='allocation_drift_limits'):
        index_config.validate_index_config(payload)


@pytest.mark.parametrize('field', ['budget_from', 'budget_to', 'limit', 'upper_inclusive'])
def test_index_missing_range_field(field):
    """Every row is strict and complete, including an explicit null final endpoint."""
    payload = index_document()
    del payload['allocation_drift_limits'][0][field]
    with pytest.raises(ValueError, match=field):
        index_config.validate_index_config(payload)


@pytest.mark.parametrize('field', ['budget_from', 'budget_to', 'limit'])
@pytest.mark.parametrize('value', [
    True, 0, 0.1, '', 'bad', 'NaN', 'sNaN', 'Infinity', '-Infinity', '-1',
])
def test_index_range_decimal_errors(field, value):
    """Finite Decimal strings are mandatory for numeric endpoints and limits."""
    payload = index_document()
    payload['allocation_drift_limits'][0][field] = value
    with pytest.raises(ValueError, match=field):
        index_config.validate_index_config(payload)


@pytest.mark.parametrize('field, value', [('budget_from', '1'), ('budget_to', '0'),
                                          ('budget_to', None), ('limit', '1'),
                                          ('upper_inclusive', True), ('upper_inclusive', 0),
                                          ('upper_inclusive', None), ('extra', '0')])
def test_index_range_structural_errors(field, value):
    """No finite inverted intervals, first-row nulls, inclusive edges or extensions."""
    payload = index_document()
    payload['allocation_drift_limits'][0][field] = value
    with pytest.raises(ValueError, match='allocation_drift_limits'):
        index_config.validate_index_config(payload)


@pytest.mark.parametrize('lower', ['99', '101'])
def test_index_gap_or_overlap(lower):
    """Ordered intervals meet exactly, without epsilon."""
    payload = index_document()
    payload['allocation_drift_limits'][1]['budget_from'] = lower
    with pytest.raises(ValueError, match='allocation_drift_limits'):
        index_config.validate_index_config(payload)


def test_index_finite_last_endpoint():
    """The last row must cover unbounded budgets."""
    payload = deepcopy(index_document())
    payload['allocation_drift_limits'][-1]['budget_to'] = '200'
    with pytest.raises(ValueError, match='budget_to'):
        index_config.validate_index_config(payload)


@pytest.mark.parametrize('value', [None, True, 0, '', 'bad', 'NaN', 'sNaN',
                                   'Infinity', '-Infinity', '-0.1', '1'])
def test_composite_required_decimal_limit(value):
    """Inter-component tolerance is required and unrelated to reserve or turnover."""
    payload = {'name': 'ROOT', 'component_drift_limit': value,
               'components': [{'algoritm': 'INDEX', 'src': 'GOLD', 'weight': '1'}]}
    with pytest.raises(ValueError, match='component_drift_limit'):
        composite_config.validate_composite_config(payload)


def test_bundled_policy_settings_and_preparation():
    """Shipped configurations and frozen composite data migrate atomically."""
    prepared = strategies.prepare_strategy('COMPOSITE', 'BALANCED')
    source = prepared.prepared_source
    assert isinstance(source, PreparedCompositeSource)
    assert getattr(source, 'component_drift_limit') == Decimal('0.20')
    for name in ('GOLD', 'OBLG', 'TMON', 'IMOEX'):
        config = index_config.select_index_config(name)
        assert config.allocation_drift_limits[0].budget_from == 0
        assert config.allocation_drift_limits[-1].budget_to is None
        if name != 'IMOEX':
            assert len(config.allocation_drift_limits) == 1
            assert config.allocation_drift_limits[0].limit == 0
    config = strategies.create_strategy(strategies.prepare_strategy('ACCOUNT', '2193248994')).config
    assert config.name == config.source_account_id == '2193248994'
    assert config.reserve == Decimal('0.01')
    assert config.allocation_drift_limit == Decimal('0.0092')


@pytest.mark.parametrize('limit', ['0', '0.20', '0.9999999999999999999999999999'])
def test_composite_limit_is_preserved_without_rounding(limit):
    """Zero and the upper edge remain fractions, without a second safety multiplier."""
    payload = {'name': 'ROOT', 'component_drift_limit': limit,
               'components': [{'algoritm': 'INDEX', 'src': 'GOLD', 'weight': '1'}]}
    config = composite_config.validate_composite_config(payload)
    assert config.component_drift_limit == Decimal(limit)


def test_composite_policy_limit_is_frozen(tmp_path, monkeypatch):
    """New preparation sees changed limits; constructing a saved tree performs no I/O."""
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(tmp_path))
    path = tmp_path / 'root.json'
    payload = {'name': 'ROOT', 'component_drift_limit': '0.20',
               'components': [{'algoritm': 'INDEX', 'src': 'GOLD', 'weight': '1'}]}
    path.write_text(json.dumps(payload), encoding='utf-8')
    prepared = strategies.prepare_strategy('COMPOSITE', 'ROOT')
    payload['component_drift_limit'] = '0.30'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with patch('builtins.open', side_effect=AssertionError('construction I/O')):
        assert strategies.create_strategy(prepared).source.component_drift_limit == Decimal('0.20')
    changed = strategies.create_strategy(strategies.prepare_strategy('COMPOSITE', 'ROOT'))
    assert changed.source.component_drift_limit == Decimal('0.30')
