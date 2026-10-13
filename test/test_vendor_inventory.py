"""Offline policy checks before any third-party binary is published."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts import vendor_inventory as inventory


def entry():
    """One candidate whose licence obligations have been reviewed."""
    return {
        'name': 'example', 'version': '1.2.3', 'kind': 'python',
        'scopes': ['runtime'], 'requires': [],
        'source': {'url': 'https://example.org/source/1.2.3', 'tag': 'v1.2.3',
                   'commit': None, 'github': 'absent', 'evidence': ['reviewed releases']},
        'license': {'expression': 'MIT', 'status': 'allowed',
                    'evidence': ['https://example.org/LICENSE'],
                    'obligations': ['retain copyright and licence'], 'fulfilled': True},
        'artifacts': [{'filename': 'example-1.2.3-py3-none-any.whl',
                       'path': 'vendor/example/example-1.2.3-py3-none-any.whl',
                       'sha256': sha256(b'wheel').hexdigest(), 'size': 5,
                       'tag': 'py3-none-any', 'role': 'wheel',
                       'url': 'https://example.org/example-1.2.3-py3-none-any.whl'},
                      {'filename': 'LICENSE', 'path': 'vendor/example/LICENSE',
                       'sha256': sha256(b'licence').hexdigest(), 'size': 7,
                       'tag': None, 'role': 'license', 'url': 'https://example.org/LICENSE'}],
        'storage': 'eligible', 'reason': 'Exact source absent; obligations fulfilled',
    }


def document(item=None):
    """A minimal seed graph with explicit provided system tools."""
    return {'schema_version': 1, 'target': 'cpython312-linux-x86_64',
            'provided_tools': ['Python', 'Git', 'Make', 'GitHub Actions'],
            'entries': [entry() if item is None else item]}


def test_valid_candidate():
    """A reviewed candidate need not have been copied in Task 1."""
    assert inventory.validate_inventory(document()) == document()['entries']


def test_sdk_policy_documents_require_exact_override(tmp_path):
    """SDK policy documents are allowed only by the narrow recovered-byte exception."""
    directory = tmp_path / 'vendor/t-tech-investments'
    directory.mkdir(parents=True)
    (directory / 'README.md').write_text('Policy and provenance caveat')
    (directory / 'manifest.json').write_text('{}')
    inventory.validate_vendor_files(tmp_path, document(sdk_entry()))
    item = sdk_entry()
    del item['override']
    item['storage'] = 'excluded'
    with pytest.raises(inventory.InventoryError, match='undeclared'):
        inventory.validate_vendor_files(tmp_path, document(item))


def test_other_packages_cannot_add_policy_documents(tmp_path):
    """The SDK exception must not allow arbitrary per-package metadata or binaries."""
    directory = tmp_path / 'vendor/example'
    directory.mkdir(parents=True)
    (directory / 'manifest.json').write_text('{}')
    with pytest.raises(inventory.InventoryError, match='undeclared'):
        inventory.validate_vendor_files(tmp_path, document())


@pytest.mark.parametrize('github,license_status,fulfilled,expected', [
    ('exact', 'allowed', True, 'excluded'),
    ('exact', 'conditional', False, 'excluded'),
    ('other-version', 'allowed', True, 'eligible'),
    ('unverified', 'allowed', True, 'blocked'),
    ('absent', 'conditional', True, 'eligible'),
    ('absent', 'conditional', False, 'blocked'),
    ('absent', 'forbidden', True, 'blocked'),
    ('absent', 'unknown', False, 'blocked'),
])
def test_storage_policy(github, license_status, fulfilled, expected):
    """Exact source excludes binaries; copyleft conditions require fulfilment."""
    item = entry()
    item['source']['github'] = github
    if github == 'exact':
        item['source'].update(url='https://github.com/example/source', commit='a' * 40)
    item['license'].update(status=license_status, fulfilled=fulfilled)
    item['storage'] = expected
    assert inventory.validate_inventory(document(item))[0]['storage'] == expected
    item['storage'] = 'eligible' if expected != 'eligible' else 'excluded'
    with pytest.raises(inventory.InventoryError, match='storage'):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('expression', ['GPL-2.0-or-later', 'LGPL-2.1-or-later', 'MPL-2.0'])
def test_copyleft_is_conditional(expression):
    """Copyleft has obligations rather than a blanket publication ban."""
    item = entry()
    item['license'].update(expression=expression, status='conditional')
    assert inventory.validate_inventory(document(item))
    item['license']['status'] = 'allowed'
    with pytest.raises(inventory.InventoryError, match='license'):
        inventory.validate_inventory(document(item))


def sdk_entry():
    """Only the explicitly accepted recovered SDK bypasses GitHub exclusion."""
    item = entry()
    item.update(name='t-tech-investments', version='1.51.0',
                override='explicit-user-sdk-1.51.0-recovered-wheel')
    item['source'].update(github='exact', url='https://github.com/Invest-SDK/invest-python',
                          commit='0110e451cb0316e975ac5fc5ee027b95246f6396')
    item['artifacts'][0].update(
        filename='t_tech_investments-1.51.0-py3-none-any.whl',
        path='vendor/t-tech-investments/t_tech_investments-1.51.0-py3-none-any.whl',
        sha256='f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856', size=316190)
    item['artifacts'][1]['path'] = 'vendor/t-tech-investments/LICENSE'
    item['artifacts'][1].update(
        sha256='5484c945bc04994e2080cd022e6df8ef3977230fcd65168846be50e0ecdfde89', size=11540)
    item['license']['expression'] = 'Apache-2.0'
    return item


def test_explicit_sdk_override():
    """Accept precisely the recovered identity without a general default allow."""
    item = sdk_entry()
    assert inventory.validate_inventory(document(item))[0]['storage'] == 'eligible'
    del item['override']
    with pytest.raises(inventory.InventoryError, match='storage'):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('change', [
    'name', 'version', 'hash', 'size', 'source', 'license', 'license-expression',
    'license-hash', 'override',
])
def test_override_cannot_spread(change):
    """Another package/version/bytes or unfulfilled licence cannot reuse consent."""
    item = sdk_entry()
    if change in ('name', 'version'):
        item[change] = 'other' if change == 'name' else '1.52.0'
        filename = f"{item['name'].replace('-', '_')}-{item['version']}-py3-none-any.whl"
        item['artifacts'][0].update(filename=filename, path=f"vendor/{item['name']}/{filename}")
    elif change in ('hash', 'size'):
        field = 'sha256' if change == 'hash' else 'size'
        item['artifacts'][0][field] = 'a' * 64 if change == 'hash' else 1
    elif change == 'source':
        item['source']['github'] = 'unverified'
    elif change == 'license':
        item['license']['fulfilled'] = False
    elif change == 'license-expression':
        item['license']['expression'] = 'MIT'
    elif change == 'license-hash':
        item['artifacts'][1]['sha256'] = 'a' * 64
    else:
        item['override'] = 'allow-everything'
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('field,value', [
    ('name', ''), ('name', 'Example_Name'), ('version', None), ('version', 'latest'),
    ('kind', 'tar'), ('scopes', []), ('scopes', ['runtime', 'runtime']),
    ('scopes', ['other']), ('requires', 'package'), ('requires', ['']),
    ('source', None), ('license', {}), ('artifacts', {}), ('reason', ''),
    ('extra', 'unsupported'),
])
def test_malformed_entry(field, value):
    """Wrong fields/types are rejected with a bounded fixed diagnostic."""
    item = entry()
    item[field] = value
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('section,field,value', [
    ('source', 'url', 'https://user:secret@example.org/source'),
    ('source', 'url', 'http://example.org/source'),
    ('source', 'commit', 'not-a-commit'), ('source', 'tag', ''),
    ('source', 'github', 'network-error-is-absent'), ('source', 'evidence', []),
    ('license', 'expression', ''), ('license', 'evidence', []),
    ('license', 'obligations', []), ('license', 'fulfilled', 1),
])
def test_malformed_evidence(section, field, value):
    """Claims require version identity and licence/source evidence."""
    item = entry()
    item[section][field] = value
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('field,value', [
    ('sha256', 'a' * 63), ('sha256', 'A' * 64), ('size', True), ('size', 0),
    ('size', 100_000_001), ('tag', 'cp312-cp312-win_amd64'),
    ('filename', 'different-1.2.3-py3-none-any.whl'),
    ('filename', 'example-9.9.9-py3-none-any.whl'),
    ('path', '../example.whl'), ('path', '/tmp/example.whl'),
    ('path', 'vendor/other/example-1.2.3-py3-none-any.whl'),
    ('path', 'vendor/example/../example-1.2.3-py3-none-any.whl'),
    ('path', 'vendor/example/.env/example-1.2.3-py3-none-any.whl'),
    ('role', 'binary'), ('url', 'https://example.org/wheel#secret'),
])
def test_malformed_artifact(field, value):
    """Candidate identity, supported target, digest and relative path are strict."""
    item = entry()
    item['artifacts'][0][field] = value
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('change', [
    'schema', 'target', 'provided', 'entries', 'duplicate', 'missing',
])
def test_malformed_document(change):
    """Malformed top-level schema and duplicated package identities fail."""
    data = document()
    if change == 'schema':
        data['schema_version'] = True
    elif change == 'target':
        data['target'] = 'unknown'
    elif change == 'provided':
        data['provided_tools'] = []
    elif change == 'entries':
        data['entries'] = None
    elif change == 'duplicate':
        data['entries'].append(deepcopy(data['entries'][0]))
    else:
        del data['entries'][0]['source']
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(data)


def test_vendor_contents(tmp_path):
    """Filesystem traversal catches untracked or ignored undeclared binaries."""
    (tmp_path / 'vendor').mkdir()
    inventory.validate_vendor_files(tmp_path, document())
    artifact = tmp_path / entry()['artifacts'][0]['path']
    artifact.parent.mkdir()
    (artifact.parent / 'LICENSE').write_bytes(b'licence')
    artifact.write_bytes(b'wheel')
    inventory.validate_vendor_files(tmp_path, document())
    artifact.write_bytes(b'wrong')
    with pytest.raises(inventory.InventoryError, match='artifact'):
        inventory.validate_vendor_files(tmp_path, document())
    artifact.write_bytes(b'wheel')
    (tmp_path / 'vendor/ignored.bin').write_bytes(b'undeclared')
    with pytest.raises(inventory.InventoryError, match='undeclared'):
        inventory.validate_vendor_files(tmp_path, document())


@pytest.mark.parametrize('relative', ['vendor', 'vendor/example', 'vendor/README.md',
                                      'vendor/inventory.json', 'vendor/example/LICENSE'])
def test_symlink_rejected(tmp_path, relative):
    """Reject links, including manifests/licences/parents, before following them."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / 'outside')
    with pytest.raises(inventory.InventoryError, match='symlink'):
        inventory.validate_vendor_files(tmp_path, document())


def test_excluded_binary_rejected(tmp_path):
    """A real exact GitHub version forbids the candidate even with correct bytes."""
    item = entry()
    item['source'].update(github='exact', url='https://github.com/example/source', commit='a' * 40)
    item['storage'] = 'excluded'
    path = tmp_path / item['artifacts'][0]['path']
    path.parent.mkdir(parents=True)
    (path.parent / 'LICENSE').write_bytes(b'licence')
    path.write_bytes(b'wheel')
    with pytest.raises(inventory.InventoryError, match='storage'):
        inventory.validate_vendor_files(tmp_path, document(item))


def test_checked_inventory_and_cli(capsys):
    """Checked-in facts and the offline CLI agree without external requests."""
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'vendor/inventory.json').read_text(encoding='utf-8'))
    entries = inventory.validate_inventory(data)
    assert any(item['name'] == 't-tech-investments' for item in entries)
    inventory.validate_vendor_files(root, data)
    assert inventory.main(['--project-root', str(root)]) == 0
    assert 'inventory' in capsys.readouterr().out


def test_binary_needs_declared_licence_materials(tmp_path):
    """A binary is never accepted before its reviewed accompanying notices."""
    item = entry()
    path = tmp_path / item['artifacts'][0]['path']
    path.parent.mkdir(parents=True)
    path.write_bytes(b'wheel')
    with pytest.raises(inventory.InventoryError, match='licence materials'):
        inventory.validate_vendor_files(tmp_path, document(item))
    item['artifacts'].pop()
    with pytest.raises(inventory.InventoryError, match='license artifact'):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('value', [None, [], 1, {'schema_version': 1}])
def test_wrong_document_type(value):
    """Top-level objects cannot exploit truthiness or duck typing."""
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(value)


@pytest.mark.parametrize('value', [None, [], 'not-a-record'])
def test_wrong_entry_type(value):
    """Non-record entries fail with the declared error type."""
    data = document()
    data['entries'] = [value]
    with pytest.raises(inventory.InventoryError):
        inventory.validate_inventory(data)


@pytest.mark.parametrize('value', [
    None, 1, 'bad-tag', 'cp312-cp312-manylinux_2_28_x86_64',
    'cp312-cp312-manylinux2014_x86_64.win_amd64',
    'cp312-cp312-manylinux2014_x86_64.manylinux2014_x86_64',
])
def test_unsupported_wheel_tags(value):
    """Only target-compatible tags qualify, even in compressed tag lists."""
    item = entry()
    item['artifacts'][0]['tag'] = value
    with pytest.raises(inventory.InventoryError, match='tag'):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('tag', [
    'cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64',
    'cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64',
    'cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64',
])
def test_compressed_compatible_wheel_tags(tag):
    """One manylinux2014-compatible alternative accepts the wheel on this target."""
    item = entry()
    filename = f'example-1.2.3-{tag}.whl'
    item['artifacts'][0].update(tag=tag, filename=filename, path=f'vendor/example/{filename}')
    assert inventory.validate_inventory(document(item))


def test_deployment_artifact_and_source_materials():
    """Native reserves also require identity, hashes and accompanying material."""
    item = entry()
    item.update(kind='deployment', scopes=['deployment'])
    item['artifacts'][0].update(filename='example-1.2.3-linux-x86_64',
                                path='vendor/example/example-1.2.3-linux-x86_64',
                                tag='linux-x86_64', role='native')
    assert inventory.validate_inventory(document(item))
    item['artifacts'][0]['tag'] = 'win32'
    with pytest.raises(inventory.InventoryError, match='native identity'):
        inventory.validate_inventory(document(item))


def test_duplicates_and_missing_dependencies():
    """Artifact paths are unique and all declared dependency nodes are present."""
    item = entry()
    item['artifacts'].append(deepcopy(item['artifacts'][0]))
    with pytest.raises(inventory.InventoryError, match='duplicate artifact'):
        inventory.validate_inventory(document(item))
    item = entry()
    item['requires'] = ['missing']
    with pytest.raises(inventory.InventoryError, match='closure'):
        inventory.validate_inventory(document(item))


@pytest.mark.parametrize('url', [
    1, 'https://example.org:bad/path', 'https://example.org:444/path', 'https://example.org/a b',
])
def test_unsafe_source_urls(url):
    """Malformed authorities and unexpected ports fail without echoing input."""
    item = entry()
    item['source']['url'] = url
    with pytest.raises(inventory.InventoryError, match='source'):
        inventory.validate_inventory(document(item))


def test_false_exact_github_claim():
    """Exact GitHub evidence requires a GitHub repository and immutable commit."""
    item = entry()
    item['source']['github'] = 'exact'
    item['storage'] = 'excluded'
    with pytest.raises(inventory.InventoryError, match='exact identity'):
        inventory.validate_inventory(document(item))


def test_cli_failure_is_safe(tmp_path, capsys):
    """Malformed local JSON yields a fixed diagnostic and failure status."""
    vendor = tmp_path / 'vendor'
    vendor.mkdir()
    (vendor / 'inventory.json').write_text('not-json secret', encoding='utf-8')
    assert inventory.main(['--project-root', str(tmp_path)]) == 1
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'invalid vendor inventory or files\n'
