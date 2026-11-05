"""Raw Git provenance probes use private, harmless repositories only."""
import os
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import create_autospec
from zipfile import ZipFile

import pytest

from scripts import cloud_archive_inputs as inputs
from scripts import build_yandex_archive as builder
from scripts import cloud_dependencies

# Atomicity tests replace the builder's existing internal preparation port.
# pylint: disable=protected-access


def git(root, *args):
    """Run fixture setup without machine-specific Git configuration or hooks."""
    environment = {key: os.environ[key] for key in ('PATH', 'SystemRoot') if key in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                       GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                          env=environment, check=True, capture_output=True).stdout.strip()


@pytest.fixture(name='repository')
def fixture_repository(tmp_path):
    """Commit an application plus a tracked file outside its payload."""
    root = tmp_path / 'repository'
    root.mkdir()
    for name in (*inputs.EXPLICIT_FILES, 'autorepeater/module.py',
                 'autorepeater/configs/config.json', 'notes.txt'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'harmless bytes\n')
    git(root, 'init', '-q')
    git(root, 'add', '.')
    commit(root)
    return root


def commit(root):
    """Every synthetic commit carries the required attribution exactly once."""
    git(root, 'commit', '-qm', 'Fixture\n\nCo-Authored-By: O3 Agent Codex <noreply@ozon.ru>')


def test_cloud_archive_inputs_git_clean(repository):
    """A clean private checkout reports its actual complete identity."""
    facts = inputs.snapshot(repository, release=True).source
    head = git(repository, 'rev-parse', 'HEAD').decode()
    assert facts == inputs.SourceFacts(head, False, 'known')


def test_cloud_archive_inputs_git_replacement_cannot_relabel_tree(repository):
    """Replacement objects must not certify another tree under the original HEAD."""
    head = git(repository, 'rev-parse', 'HEAD').decode()
    (repository / 'handler.py').write_bytes(b'replacement application bytes\n')
    git(repository, 'add', 'handler.py')
    commit(repository)
    replacement = git(repository, 'rev-parse', 'HEAD').decode()
    git(repository, 'update-ref', 'HEAD', head)
    git(repository, 'replace', head, replacement)
    assert inputs.snapshot(repository).source == inputs.SourceFacts(head, True, 'known')
    with pytest.raises(inputs.InputError, match='release source is dirty'):
        inputs.snapshot(repository, release=True, expected_sha=head)


@pytest.mark.parametrize('committed_mode,worktree_mode,dirty', [
    (0o755, 0o654, True), (0o644, 0o641, False),
])
def test_cloud_archive_inputs_git_owner_executable_mode(
        repository, committed_mode, worktree_mode, dirty):
    """Git records the owner executable bit independently of group and other bits."""
    path = repository / 'notes.txt'
    path.chmod(committed_mode)
    if committed_mode == 0o755:
        git(repository, 'add', 'notes.txt')
        commit(repository)
    path.chmod(worktree_mode)
    assert inputs.snapshot(repository).source.dirty is dirty
    if dirty:
        with pytest.raises(inputs.InputError, match='release source is dirty'):
            inputs.snapshot(repository, release=True)
    else:
        assert inputs.snapshot(repository, release=True).source.dirty is False


@pytest.mark.parametrize('change', ['unstaged', 'staged', 'deleted', 'untracked', 'ignored',
                                    'executable', 'same-stat'])
def test_cloud_archive_inputs_git_dirty(repository, change):
    """Raw changes anywhere and additional eligible inputs make local facts dirty."""
    path = repository / 'notes.txt'
    if change in ('unstaged', 'staged', 'same-stat'):
        previous = path.stat()
        path.write_bytes(b'changed! bytes\n')
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        if change == 'staged':
            git(repository, 'add', 'notes.txt')
    elif change == 'deleted':
        path.unlink()
    elif change == 'executable':
        path.chmod(0o755)
    else:
        if change == 'ignored':
            git(repository, 'config', 'core.excludesFile', '/dev/null')
            (repository / '.gitignore').write_text('autorepeater/extra.py\n')
            git(repository, 'add', '.gitignore')
            commit(repository)
        (repository / 'autorepeater/extra.py').write_bytes(b'extra')
    assert inputs.snapshot(repository).source.dirty is True
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_cloud_archive_inputs_git_index_flags(repository, flag):
    """Unsupported original index flags never produce partial facts."""
    git(repository, 'update-index', flag, 'notes.txt')
    assert inputs.snapshot(repository).source.git_state == 'unavailable'
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


def test_cloud_archive_inputs_git_expected_and_no_git(repository, tmp_path):
    """Expected identity requires a valid known HEAD, including local mode."""
    head = git(repository, 'rev-parse', 'HEAD').decode()
    assert inputs.snapshot(repository, expected_sha=head).source.head == head
    for expected in ('bad', '0' * 40):
        with pytest.raises(inputs.InputError):
            inputs.snapshot(repository, expected_sha=expected)
    git(repository, 'init', '-q', str(tmp_path / 'empty'))
    assert inputs.source_facts(tmp_path / 'empty', ()).git_state == 'unavailable'


def test_cloud_archive_inputs_git_filter_never_executes(repository, monkeypatch):
    """Neither clean nor process filters can hide raw drift or execute commands."""
    sentinel = repository / 'filter-ran'
    (repository / '.gitattributes').write_text('notes.txt filter=sentinel\n')
    git(repository, 'add', '.gitattributes')
    commit(repository)
    git(repository, 'config', 'filter.sentinel.clean', f'touch {sentinel}')
    git(repository, 'config', 'filter.sentinel.process', f'touch {sentinel}')
    port = create_autospec(builder._default_entries, spec_set=True, return_value={})
    monkeypatch.setattr(builder, '_default_entries', port)
    builder.build_archive(repository, release=True)
    assert not sentinel.exists()
    (repository / 'notes.txt').write_bytes(b'changed raw bytes\r\n')
    assert inputs.snapshot(repository).source.dirty is True
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)
    assert not sentinel.exists()


def test_cloud_archive_inputs_release_builder_drift(repository, monkeypatch):
    """Late drift keeps the old ZIP and prevents publication."""
    output = repository / 'build/output.zip'
    output.parent.mkdir()
    output.write_bytes(b'old zip')
    prepare = create_autospec(builder._default_entries, spec_set=True)

    def mutate(_root):
        (repository / 'notes.txt').write_bytes(b'drift')
        return {}
    prepare.side_effect = mutate
    monkeypatch.setattr(builder, '_default_entries', prepare)
    with pytest.raises(inputs.InputError):
        builder.build_archive(repository, output, release=True)
    assert output.read_bytes() == b'old zip'


def test_cloud_archive_inputs_release_offline_auto_before_prepare(repository, monkeypatch):
    """The release SDK policy rejects auto before any input or preparation call."""
    prepare = create_autospec(inputs.snapshot, spec_set=True)
    monkeypatch.setattr(inputs, 'snapshot', prepare)
    with pytest.raises(ValueError):
        builder.build_archive(repository, release=True, offline_archive=True)
    prepare.assert_not_called()


@pytest.mark.parametrize('policy', ['local', 'expected', 'release'])
@pytest.mark.parametrize('failure', ['missing', 'timeout', 'stdout', 'stderr', 'exit'])
def test_cloud_archive_inputs_git_failure_table(repository, monkeypatch, caplog,
                                                policy, failure):
    """Failed facts are wholly unavailable or fatal, without leaking output."""
    binary = repository.parent / 'bin'
    binary.mkdir()
    executable = binary / 'git'
    scripts = {
        'timeout': 'import time; time.sleep(30)',
        'stdout': 'import os; os.write(1, b"sensitive-sentinel" * 100000)',
        'stderr': 'import os; os.write(2, b"sensitive-sentinel" * 100000)',
        'exit': 'import sys; print("sensitive-sentinel", file=sys.stderr); sys.exit(2)',
    }
    if failure != 'missing':
        executable.write_text(f'#!{sys.executable}\n{scripts[failure]}\n')
        executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary))
    monkeypatch.setattr(inputs, 'GIT_TIMEOUT', 0.2)
    kwargs = {'release': policy == 'release', 'expected_sha': '0' * 40 if policy == 'expected'
              else ''}
    if policy == 'local':
        assert inputs.snapshot(repository, **kwargs).source == inputs.SourceFacts()
    else:
        with pytest.raises(inputs.InputError) as error:
            inputs.snapshot(repository, **kwargs)
        assert 'sensitive-sentinel' not in str(error.value)
    assert 'sensitive-sentinel' not in caplog.text


@pytest.mark.parametrize('state', ['intent', 'sparse', 'sparse-index', 'gitlink', 'unmerged'])
def test_cloud_archive_inputs_git_unsupported(repository, state):
    """Unsupported index and repository states refuse release without content effects."""
    if state == 'intent':
        (repository / 'new.txt').write_bytes(b'new')
        git(repository, 'add', '-N', 'new.txt')
    elif state in ('sparse', 'sparse-index'):
        git(repository, 'config', 'core.sparseCheckout' if state == 'sparse' else 'index.sparse',
            'true')
    elif state == 'gitlink':
        head = git(repository, 'rev-parse', 'HEAD').decode()
        git(repository, 'update-index', '--add', '--cacheinfo', f'160000,{head},submodule')
    else:
        identity = git(repository, 'hash-object', 'notes.txt').decode()
        git(repository, 'update-index', '--force-remove', 'notes.txt')
        command = ['git', '-C', str(repository), 'update-index', '--index-info']
        subprocess.run(command, input=f'100644 {identity} 1\tnotes.txt\n'.encode(),
                       env=inputs.git_environment(), check=True, capture_output=True)
    assert inputs.snapshot(repository).source == inputs.SourceFacts()
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


def test_cloud_archive_inputs_git_symlink_metadata(repository, tmp_path, monkeypatch):
    """A tracked nonpayload symlink is compared through readlink only."""
    link = repository / 'reference'
    target = tmp_path / 'absent-target'
    link.symlink_to(target)
    git(repository, 'add', 'reference')
    commit(repository)
    read = create_autospec(type(link).read_bytes, spec_set=True)
    original = type(link).read_bytes

    def reject_target(path):
        assert path not in (target, link)
        return original(path)
    read.side_effect = reject_target
    monkeypatch.setattr(type(link), 'read_bytes', read)
    assert inputs.snapshot(repository, release=True).source.dirty is False
    link.unlink()
    link.symlink_to(tmp_path / 'different-absent-target')
    assert inputs.snapshot(repository).source.dirty is True


@pytest.mark.parametrize('kind', ['shallow', 'worktree'])
def test_cloud_archive_inputs_git_checkout_kinds(repository, tmp_path, kind):
    """Shallow and linked worktree Git discovery need no environment overrides."""
    destination = tmp_path / kind
    if kind == 'shallow':
        git(repository, 'clone', '-q', '--depth=1', repository.as_uri(), str(destination))
    else:
        git(repository, 'worktree', 'add', '-q', '--detach', str(destination))
    assert inputs.snapshot(destination, release=True).source.dirty is False


def test_cloud_archive_inputs_git_environment(repository, monkeypatch):
    """External repository, index, config and object environment overrides are dropped."""
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                'GIT_CONFIG_COUNT', 'GIT_NAMESPACE'):
        monkeypatch.setenv(key, 'invalid-sentinel')
    assert inputs.snapshot(repository, release=True).source.dirty is False
    assert set(inputs.git_environment()) <= {
        'PATH', 'SystemRoot', 'LANG', 'LC_ALL', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL',
        'GIT_TERMINAL_PROMPT', 'GIT_OPTIONAL_LOCKS'}


@pytest.mark.parametrize('mutation', ['head', 'bytes', 'membership'])
def test_cloud_archive_inputs_git_recheck(repository, mutation):
    """Release source identity, raw bytes and eligible membership remain fixed."""
    original = inputs.snapshot(repository, release=True)
    if mutation == 'head':
        git(repository, 'commit', '--allow-empty', '-qm',
            'Next\n\nCo-Authored-By: O3 Agent Codex <noreply@ozon.ru>')
    elif mutation == 'bytes':
        (repository / 'handler.py').write_bytes(b'changed')
    else:
        (repository / 'autorepeater/extra.py').write_bytes(b'extra')
    with pytest.raises(inputs.InputError):
        inputs.recheck(repository, original)


def test_cloud_archive_inputs_git_batches_and_no_filters(repository, monkeypatch):
    """Raw ordinary hashes use argument batches of at most 64 paths and no writes."""
    for number in range(130):
        (repository / f'notes-{number}.txt').write_bytes(b'harmless')
    git(repository, 'add', '.')
    commit(repository)
    original = inputs.git_output
    port = create_autospec(original, spec_set=True, side_effect=original)
    monkeypatch.setattr(inputs, 'git_output', port)
    assert inputs.snapshot(repository, release=True).source.dirty is False
    batches = [call.args[1] for call in port.call_args_list
               if call.args[1][0] == 'hash-object']
    assert len(batches) == 3
    assert all(batch[:3] == ['hash-object', '--no-filters', '--'] and len(batch[3:]) <= 64
               for batch in batches)


@pytest.mark.parametrize('release', [False, True])
@pytest.mark.parametrize('flags', [(False, False), (True, False), (True, True)])
def test_cloud_archive_inputs_git_builder_profiles(repository, monkeypatch, release, flags):
    """Every allowed build profile records verified source facts in its final manifest."""
    for owner, name in ((builder, '_default_entries'), (builder, '_vendored_entries'),
                        (cloud_dependencies, 'prepare_payload')):
        port = create_autospec(getattr(owner, name), spec_set=True, return_value={})
        monkeypatch.setattr(owner, name, port)
    head = git(repository, 'rev-parse', 'HEAD').decode()
    output = builder.build_archive(repository, release=release, expected_sha=head,
                                   use_vendored_sdk=flags[0], offline_archive=flags[1])
    with ZipFile(output) as archive:
        document = json.loads(archive.read('archive-manifest.json'))
    assert document['source'] == {'head': head, 'dirty': False, 'git_state': 'known'}
    assert document['mode'] == ('release' if release else 'local')


@pytest.mark.parametrize('name', [
    '.env', '.env.production', '.idea/state', '../escape', '/absolute', 'back\\slash'])
def test_cloud_archive_inputs_git_unsafe_metadata_without_reads(repository, monkeypatch, name):
    """Forbidden names exist only in metadata doubles and stop before content commands."""
    original = inputs.git_output

    def output(root, arguments, **kwargs):
        if arguments == ['ls-files', '--stage', '-z']:
            return f'100644 {"0" * 40} 0\t{name}\0'.encode()
        assert arguments[0] not in ('hash-object', 'cat-file')
        return original(root, arguments, **kwargs)
    port = create_autospec(original, spec_set=True, side_effect=output)
    monkeypatch.setattr(inputs, 'git_output', port)
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


@pytest.mark.parametrize('kind', ['fifo', 'ancestor-symlink', 'directory'])
def test_cloud_archive_inputs_git_wrong_tracked_type(repository, tmp_path, kind):
    """Tracked nonordinary bytes are never opened or traversed by Git hashing."""
    if kind == 'ancestor-symlink':
        directory = repository / 'notes'
        directory.mkdir()
        (directory / 'record').write_bytes(b'harmless')
        git(repository, 'add', 'notes/record')
        commit(repository)
        directory.rename(tmp_path / 'saved-notes')
        directory.symlink_to(tmp_path / 'absent-notes', target_is_directory=True)
    else:
        path = repository / 'notes.txt'
        path.unlink()
        if kind == 'fifo':
            os.mkfifo(path)
        else:
            path.mkdir()
    assert inputs.snapshot(repository).source.dirty is True
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


@pytest.mark.parametrize('value', ['0', '1', 'invalid', '2'])
def test_cloud_archive_inputs_git_make_release_flag(value):
    """The additive Make switch accepts exactly zero or one."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['make', '-n', 'claude-yandex-archive', f'ARCHIVE_RELEASE={value}',
                             'CLOUD_PACKAGE_SHA=' + 'a' * 40], cwd=root,
                            check=False, capture_output=True, text=True)
    assert result.returncode == (0 if value in ('0', '1') else 2)
    if result.returncode == 0:
        assert ('--release' in result.stdout) == (value == '1')
        assert '--expected-sha "' + 'a' * 40 + '"' in result.stdout


def test_cloud_archive_inputs_git_cli_forwarding(repository, monkeypatch):
    """The CLI forwards the explicit source policy without hidden defaults."""
    port = create_autospec(builder.build_archive, spec_set=True)
    monkeypatch.setattr(builder, 'build_archive', port)
    builder.main(['--project-root', str(repository), '--release', '--expected-sha', 'a' * 40])
    port.assert_called_once_with(repository, None, use_vendored_sdk=False,
                                 offline_archive=False, release=True, expected_sha='a' * 40)


def test_cloud_archive_inputs_git_unrelated_untracked_and_hooks(repository):
    """Unrelated files do not dirty a checkout and provenance never triggers hooks."""
    (repository / 'draft.txt').write_bytes(b'untracked notes')
    sentinel = repository / 'hook-ran'
    hook = repository / '.git/hooks/post-index-change'
    hook.write_text(f'#!/bin/sh\ntouch {sentinel}\n')
    hook.chmod(0o755)
    assert inputs.snapshot(repository, release=True).source.dirty is False
    assert not sentinel.exists()


def test_cloud_archive_inputs_git_mismatched_root(repository):
    """Nested source roots cannot claim the identity of their parent repository."""
    root = repository / 'nested'
    for file in inputs.snapshot(repository).files:
        path = root / file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.data)
    assert inputs.snapshot(root).source == inputs.SourceFacts()
    with pytest.raises(inputs.InputError):
        inputs.snapshot(root, release=True)


def test_cloud_archive_inputs_git_crlf_normalization_cannot_hide_drift(repository):
    """Built-in EOL normalization cannot turn raw CRLF bytes into clean provenance."""
    git(repository, 'config', 'core.autocrlf', 'true')
    (repository / 'notes.txt').write_bytes(b'harmless bytes\r\n')
    git(repository, 'add', 'notes.txt')
    # The normalized blob is unchanged, but the raw worktree is dirty.
    assert inputs.snapshot(repository).source.dirty is True
    with pytest.raises(inputs.InputError):
        inputs.snapshot(repository, release=True)


def test_cloud_archive_inputs_git_invalid_symlink_hash(repository, monkeypatch):
    """Malformed Git hash output invalidates all facts rather than claiming dirty."""
    (repository / 'reference').symlink_to('absent-target')
    git(repository, 'add', 'reference')
    commit(repository)
    original = inputs.git_output

    def output(root, arguments, **kwargs):
        if '--stdin' in arguments:
            return b'invalid-sentinel\n'
        return original(root, arguments, **kwargs)

    port = create_autospec(original, spec_set=True, side_effect=output)
    monkeypatch.setattr(inputs, 'git_output', port)
    assert inputs.snapshot(repository).source == inputs.SourceFacts()
