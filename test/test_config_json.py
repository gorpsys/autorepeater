"""Strict JSON reading is shared while algorithm schemas remain independent."""
import json
from unittest.mock import create_autospec, patch

import pytest

from autorepeater import account_config, composite_config, index_config
from autorepeater.config_catalog import discover_candidates


READERS = [account_config.read_account_document, index_config.read_index_document,
           composite_config.read_composite_document]


@pytest.mark.parametrize('reader', READERS)
@pytest.mark.parametrize('payload, key', [
    ('{"reserve":"0.1","reserve":"0.2"}', 'reserve'),
    ('{"reserve":"0.1","reserve":"0.1"}', 'reserve'),
    ('{"extra":{"nested":[{"limit":"0","limit":"0"}]}}', 'limit'),
    (r'{"name":"FIRST","na\u006de":"SECOND"}', 'name'),
    ('{"extra":{"ключ":1,"ключ":2}}', 'ключ'),
])
def test_readers_reject_duplicate_keys(tmp_path, reader, payload, key):
    """Every object rejects repeated decoded keys, including equal values."""
    path = tmp_path / 'config.json'
    path.write_text(payload, encoding='utf-8')
    with pytest.raises(ValueError, match=f'duplicate.*{key}'):
        reader(path)


@pytest.mark.parametrize('reader', READERS)
def test_duplicate_error_does_not_disclose_values(tmp_path, reader):
    """Diagnostics identify a key without dumping document values."""
    path = tmp_path / 'config.json'
    path.write_text('{"name":"private-first","name":"private-second"}', encoding='utf-8')
    with pytest.raises(ValueError) as caught:
        reader(path)
    assert 'private-first' not in str(caught.value)
    assert 'private-second' not in str(caught.value)


@pytest.mark.parametrize('reader', READERS)
@pytest.mark.parametrize('payload', [
    '{"ключ":[{"same":1},{"same":2}],"types":[true,false,null,1.25,"0.01"]}',
    '[]', 'null', 'true', '42', '1.25', '"строка"',
])
def test_readers_preserve_json_types_order_and_unicode(tmp_path, reader, payload):
    """Parsing stays schema neutral and reads each UTF-8 file just once."""
    path = tmp_path / 'config.json'
    path.write_text(payload, encoding='utf-8')
    with patch('builtins.open', wraps=open) as opened:
        parsed = reader(path)
    opened.assert_called_once_with(path, encoding='utf-8')
    expected = json.loads(payload)
    assert parsed == expected
    assert type(parsed) is type(expected)
    if isinstance(parsed, dict):
        assert list(parsed) == list(expected)
        assert [type(item) for item in parsed['types']] == [bool, bool, type(None), float, str]


@pytest.mark.parametrize('reader', READERS)
def test_readers_keep_json_syntax_error(tmp_path, reader):
    """The normal decoder error retains its original location information."""
    path = tmp_path / 'config.json'
    path.write_text('{', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError) as caught:
        reader(path)
    assert caught.value.lineno == 1
    assert caught.value.pos == 1


@pytest.mark.parametrize('reader', READERS)
def test_readers_keep_io_error(tmp_path, reader):
    """A missing file remains an I/O failure for the catalog to handle."""
    path = tmp_path / 'missing.json'
    with pytest.raises(FileNotFoundError) as caught:
        reader(path)
    assert caught.value.filename == str(path)


@pytest.mark.parametrize('reader', READERS)
def test_duplicate_json_has_no_catalog_name(tmp_path, reader):
    """A failed parse is read once and never reaches schema validation."""
    path = tmp_path / 'config.json'
    path.write_text('{"name":"GOOD","reserve":"0.1","reserve":"0.2"}', encoding='utf-8')
    validate = create_autospec(account_config.validate_account_config, spec_set=True)
    with patch('builtins.open', wraps=open) as opened:
        candidates = discover_candidates([path], reader, validate)
    opened.assert_called_once_with(path, encoding='utf-8')
    validate.assert_not_called()
    assert candidates[0].name is None
    assert candidates[0].config is None
    assert 'duplicate' in str(candidates[0].error)
