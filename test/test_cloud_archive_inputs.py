"""Synthetic filesystem probes for the common cloud application input boundary."""
import os
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from scripts import build_yandex_archive as builder
from scripts import check_cloud_bundle as checker
from scripts import cloud_dependencies
from scripts import cloud_archive_inputs as inputs


@pytest.fixture(name='tree')
def fixture_tree(tmp_path):
    """Use harmless bytes and no machine-specific configuration."""
    root = tmp_path / 'project'
    for name in ('main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt',
                 'autorepeater/module.py', 'autorepeater/configs/deep/config.json'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'harmless bytes')
    return root


UNSAFE_INPUTS = [
    'project', 'ancestor', 'autorepeater', 'autorepeater/configs',
    'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt',
    'autorepeater/linked.py', 'autorepeater/configs/linked.json',
    'autorepeater/configs/linked', 'cycle', 'dangling', 'missing',
    'wrong-file', 'wrong-root', 'fifo', 'config-fifo', 'module-fifo',
    'unsafe-name', 'backslash-name',
]


def replace_required_input(root, damage):
    """Remove a required ordinary file, optionally replacing its type."""
    path = root / 'handler.py'
    path.unlink()
    if damage == 'wrong-file':
        path.mkdir()
    elif damage == 'fifo':
        os.mkfifo(path)


def unsafe_tree(root, damage, tmp_path):
    """Construct one invalid input without reading a symlink's destination."""
    if damage == 'ancestor':
        alias = tmp_path / 'alias'
        alias.symlink_to(root.parent, target_is_directory=True)
        return alias / root.name
    if damage in ('missing', 'wrong-file', 'fifo'):
        replace_required_input(root, damage)
        return root
    if damage == 'wrong-root':
        path = root / 'autorepeater/configs'
        path.rename(root / 'saved-configs')
        path.write_bytes(b'ordinary file')
        return root
    if damage in ('config-fifo', 'module-fifo'):
        os.mkfifo(root / ('autorepeater/configs/pipe.json' if damage == 'config-fifo'
                          else 'autorepeater/pipe.py'))
        return root
    if damage in ('unsafe-name', 'backslash-name'):
        (root / 'autorepeater/configs' / (
            'control\n.json' if damage == 'unsafe-name' else 'back\\slash.json')).write_bytes(b'')
        return root
    if damage in ('cycle', 'dangling'):
        path = root / 'autorepeater/configs/link'
        target = path.parent if damage == 'cycle' else tmp_path / 'absent'
    else:
        path = root if damage == 'project' else root / damage
        target = tmp_path / ('project-sibling' if damage == 'project' else 'outside-input')
        if path.exists():
            path.rename(target)
        elif damage.endswith(('.py', '.json')):
            target.write_bytes(b'outside sentinel')
        else:
            target.mkdir()
            (target / 'outside.json').write_bytes(b'outside sentinel')
    path.symlink_to(target, target_is_directory=target.is_dir())
    return root


@pytest.mark.parametrize('flags', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('damage', UNSAFE_INPUTS)
def test_unsafe_inputs_fail_before_reads_or_preparation(
        tree, tmp_path, monkeypatch, damage, flags):
    """Every mode shares the boundary; external bytes and effect ports stay untouched."""
    root = unsafe_tree(tree, damage, tmp_path)
    reads = create_autospec(Path.read_bytes, spec_set=True,
                            side_effect=AssertionError('application read before validation'))
    monkeypatch.setattr(Path, 'read_bytes', reads)
    ports = []
    for owner, name in ((builder, '_default_entries'), (builder, '_vendored_entries'),
                        (cloud_dependencies, 'prepare_payload')):
        port = create_autospec(getattr(owner, name), spec_set=True,
                               side_effect=AssertionError('preparation before validation'))
        monkeypatch.setattr(owner, name, port)
        ports.append(port)
    with pytest.raises(ValueError, match='^unsafe application'):
        builder.build_archive(root, tmp_path / 'output.zip',
                              use_vendored_sdk=flags[0], offline_archive=flags[1])
    reads.assert_not_called()
    for port in ports:
        port.assert_not_called()


@pytest.mark.parametrize('damage', UNSAFE_INPUTS)
def test_checker_uses_same_boundary(tree, tmp_path, monkeypatch, damage):
    """Checkout comparison cannot silently skip dangerous paths either."""
    root = unsafe_tree(tree, damage, tmp_path)
    reads = create_autospec(Path.read_bytes, spec_set=True,
                            side_effect=AssertionError('unsafe checkout read'))
    monkeypatch.setattr(Path, 'read_bytes', reads)
    with pytest.raises(ValueError, match='^unsafe application'):
        checker.application_files(root)
    reads.assert_not_called()


def test_relative_root_and_allowlist(tree, tmp_path, monkeypatch):
    """Relative roots, flat Python and deep JSON work independently of cwd/env."""
    for name in ('autorepeater/.hidden.py', 'autorepeater/nested/ignored.py',
                 'autorepeater/configs/.hidden/ignored.json',
                 'autorepeater/configs/upper.JSON', 'autorepeater/configs/readme.txt',
                 'autorepeater/configs/readme:notes.txt',
                 'autorepeater/configs/back\\slash.txt'):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'excluded')
    (tree / 'autorepeater/configs/.hidden-link').symlink_to(tmp_path / 'absent')
    (tree / 'autorepeater/ignored.txt').symlink_to(tmp_path / 'absent')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(tmp_path / 'unrelated'))
    assert {p.relative_to(tree).as_posix() for p in inputs.archive_files(Path('project'))} == {
        'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt',
        'autorepeater/module.py', 'autorepeater/configs/deep/config.json'}
    assert checker.application_files(Path('project')) == {
        'main.py': b'harmless bytes', 'handler.py': b'harmless bytes',
        'autorepeater/module.py': b'harmless bytes',
        'autorepeater/configs/deep/config.json': b'harmless bytes'}


@pytest.mark.parametrize('kind', ['symlink', 'fifo'])
def test_non_json_config_still_rejects_unsafe_types(tree, tmp_path, kind):
    """Allowlist exclusions do not bypass lstat rejection of links and special files."""
    path = tree / 'autorepeater/configs/readme:notes.txt'
    if kind == 'symlink':
        path.symlink_to(tmp_path / 'absent')
    else:
        os.mkfifo(path)
    with pytest.raises(inputs.InputError, match='unsafe application path type'):
        inputs.archive_files(tree)


def test_lexical_parent_is_rejected(tree, monkeypatch):
    """Normalization must not erase a forbidden parent component."""
    monkeypatch.chdir(tree.parent)
    with pytest.raises(ValueError, match='^unsafe application'):
        inputs.archive_files(Path('project') / '..' / 'project')


@pytest.mark.parametrize('name', ['main.py', 'autorepeater/configs/deep'])
def test_internal_symlink_is_rejected(tree, name):
    """Even links pointing back into the same allowed tree are unsafe inputs."""
    path = tree / name
    saved = path.with_name('saved-input')
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=saved.is_dir())
    with pytest.raises(inputs.InputError, match='^unsafe application'):
        inputs.archive_files(tree)


def test_sibling_prefix_is_not_containment(tree):
    """Containment compares path components instead of matching text prefixes."""
    sibling = tree.with_name(tree.name + '-sibling')
    sibling.mkdir()
    path = sibling / 'module.py'
    path.write_bytes(b'outside')
    with pytest.raises(inputs.InputError, match='^unsafe application'):
        inputs.validate_path(path, tree)


def test_read_revalidates_path_and_masks_errors(tree, tmp_path, monkeypatch):
    """Selection cannot authorize later symlink replacement or raw I/O error text."""
    path = inputs.archive_files(tree)[0]
    target = tmp_path / 'external-bytes'
    path.rename(target)
    path.symlink_to(target)
    reads = create_autospec(Path.read_bytes, spec_set=True,
                            side_effect=OSError('private sentinel'))
    monkeypatch.setattr(Path, 'read_bytes', reads)
    with pytest.raises(inputs.InputError, match='^unsafe application path type$'):
        inputs.read_file(path, tree)
    reads.assert_not_called()
    with pytest.raises(inputs.InputError, match='^unsafe application read failed$'):
        inputs.read_file(tree / 'handler.py', tree)


def test_builder_revalidates_after_preparation(tree, tmp_path, monkeypatch):
    """A path replaced during preparation never reaches the archive reader."""
    def prepare(root):
        path = root / 'handler.py'
        target = tmp_path / 'external-bytes'
        path.rename(target)
        path.symlink_to(target)
        return {}

    monkeypatch.setattr(builder, '_default_entries', create_autospec(
        getattr(builder, '_default_entries'), spec_set=True, side_effect=prepare))
    with pytest.raises(inputs.InputError, match='^unsafe application path type$'):
        builder.build_archive(tree)
    assert not (tree / 'build').exists() or not list((tree / 'build').iterdir())


def test_hidden_config_targets_are_never_inspected(tree, monkeypatch):
    """Hidden links are excluded by name before lstat or target classification."""
    hidden = tree / 'autorepeater/configs/.hidden-link'
    hidden.symlink_to(tree)
    original = Path.lstat

    def inspect(path):
        assert path != hidden, 'hidden target was inspected'
        return original(path)

    monkeypatch.setattr(Path, 'lstat', inspect)
    assert len(inputs.archive_files(tree)) == 6


def test_config_inspection_errors_are_safe(tree, monkeypatch):
    """Metadata failures do not echo filesystem exception details."""
    original = Path.lstat

    def inspect(path):
        if path.name == 'config.json':
            raise OSError('private sentinel')
        return original(path)

    monkeypatch.setattr(Path, 'lstat', inspect)
    with pytest.raises(inputs.InputError, match='^unsafe application path unavailable$'):
        inputs.archive_files(tree)
