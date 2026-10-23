"""Direct, coverage-visible checks of archive selection, writing and entrypoint."""
import json
from copy import deepcopy
from dataclasses import replace
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec, patch
from zipfile import ZipFile

import pytest

from scripts import build_yandex_archive as builder
from scripts import runtime_lock, sdk_wheel


@pytest.fixture(name='project')
def fixture_project(tmp_path):
    """An isolated project contains allowed files and representative distractions."""
    root = tmp_path / 'project'
    contents = {
        'main.py': b'main', 'handler.py': b'handler', 'requirements.txt': b'requirements',
        'requirements-runtime.txt': b'locked runtime requirements',
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
    'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt',
    'autorepeater/__init__.py',
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


@pytest.fixture(name='vendored_project')
def fixture_vendored_project(project):
    """Copy only the reviewed local policy and wheels; never install packages."""
    repository = Path(__file__).resolve().parents[1]
    for name in ('runtime-lock.json', 'requirements.txt', 'requirements-runtime.txt',
                 'vendor/inventory.json', 'vendor/t-tech-investments/manifest.json',
                 'vendor/t-tech-investments/LICENSE',
                 'vendor/t-tech-investments/' + sdk_wheel.FILENAME):
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository / name, destination)
    return project


@pytest.fixture(name='acquisition')
def fixture_acquisition(vendored_project, monkeypatch):
    """The strict acquisition port supplies already validated immutable bytes."""
    acquire = create_autospec(sdk_wheel.acquire_sdk, spec_set=True)
    acquire.return_value = sdk_wheel.Selection(
        sdk_wheel.Source.VENDORED, sdk_wheel.Reason.EXPLICIT_VENDORED,
        sdk_wheel.load_vendored_wheel(vendored_project), sdk_wheel.SDK_HASH)
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', acquire)
    return acquire


def test_vendored_archive_locked_requirements_and_owned_bytes(vendored_project, acquisition):
    """Only the ZIP receives local SDK requirements, hashes and upstream license."""
    original = {name: (vendored_project / name).read_bytes()
                for name in ('requirements.txt', 'requirements-runtime.txt',
                             'vendor/t-tech-investments/manifest.json')}
    owned = acquisition.return_value.wheel

    def selected(root, mode):
        assert root == vendored_project and mode == 'vendored'
        # A consumer must use the validated bytes, not reread the source wheel/license.
        (root / 'vendor/t-tech-investments' / sdk_wheel.FILENAME).write_bytes(b'changed')
        (root / 'vendor/t-tech-investments/LICENSE').write_bytes(b'changed')
        return acquisition.return_value

    acquisition.side_effect = selected
    output = builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_called_once_with(vendored_project, 'vendored')
    prefix = 'vendor/t-tech-investments/'
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == EXPECTED | {
            prefix + sdk_wheel.FILENAME, prefix + 'LICENSE', prefix + 'manifest.json'}
        assert archive.read(prefix + sdk_wheel.FILENAME) == owned.data
        assert archive.read(prefix + 'LICENSE') == owned.license_bytes
        assert json.loads(archive.read(prefix + 'manifest.json')) == json.loads(
            original[prefix + 'manifest.json'])
        requirements = archive.read('requirements.txt').decode()
        assert requirements.splitlines() == [
            '--index-url https://pypi.org/simple',
            './' + prefix + sdk_wheel.FILENAME + ' --hash=sha256:' + sdk_wheel.SDK_HASH,
            '-r requirements-runtime.txt']
        assert archive.read('requirements-runtime.txt') == original['requirements-runtime.txt']
        for name in ('requirements.txt', 'requirements-runtime.txt'):
            assert b'tbank' not in archive.read(name)
            assert b'extra-index' not in archive.read(name)
            assert b'trusted-host' not in archive.read(name)
    for name, content in original.items():
        assert (vendored_project / name).read_bytes() == content


@pytest.mark.parametrize('failure', ['approval_required', 'TLS certificate', 'SDK hash'])
def test_vendored_selection_failure_preserves_zip(vendored_project, acquisition, failure):
    """Policy/transport integrity failures never publish a partial replacement."""
    output = builder.build_archive(vendored_project)
    original = output.read_bytes()
    acquisition.side_effect = sdk_wheel.WheelError(failure)
    with pytest.raises(sdk_wheel.WheelError, match=failure):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize('relative', [
    'vendor/t-tech-investments/manifest.json', 'vendor/t-tech-investments/LICENSE',
    'vendor/t-tech-investments/' + sdk_wheel.FILENAME])
def test_invalid_reserve_precedes_output(vendored_project, acquisition, relative):
    """All local reserve validation precedes acquisition and ZIP creation."""
    (vendored_project / relative).write_bytes(b'invalid')
    with pytest.raises(ValueError):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_not_called()
    assert not (vendored_project / 'build').exists()


def test_vendored_requirements_drift_is_not_packaged(vendored_project, acquisition):
    """An edited nested input cannot smuggle another index or unpinned dependency."""
    with (vendored_project / 'requirements-runtime.txt').open('ab') as stream:
        stream.write(b'--extra-index-url https://untrusted.invalid\n')
    with pytest.raises(ValueError, match='requirements drift'):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_not_called()
    assert not (vendored_project / 'build').exists()


def test_selected_sdk_outside_lock_is_rejected(vendored_project, acquisition):
    """A successful selection still needs the canonical lock's artifact identity."""
    selected = acquisition.return_value
    acquisition.return_value = replace(selected, wheel=replace(
        selected.wheel, manifest=replace(selected.wheel.manifest, sha256='1' * 64)))
    with pytest.raises(ValueError, match='SDK artifact outside runtime lock'):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    assert not (vendored_project / 'build').exists()


def test_vendored_sdk_dependency_metadata_matches_lock(vendored_project, acquisition, monkeypatch):
    """Artifact identity alone cannot replace verification of the locked dependency graph."""
    document = deepcopy(runtime_lock.load_lock(vendored_project))
    next(p for p in document['packages']
         if p['name'] == sdk_wheel.NAME)['requires_dist'] = []
    monkeypatch.setattr(runtime_lock, 'load_lock', create_autospec(
        runtime_lock.load_lock, spec_set=True, return_value=document))
    with pytest.raises(ValueError, match='SDK dependency metadata drift'):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_called_once()
    assert not (vendored_project / 'build').exists()


@pytest.mark.parametrize('field', ['source', 'reason', 'manifest'])
def test_vendored_selection_policy_drift(vendored_project, acquisition, field):
    """Only a forced vendored selection under the snapshotted policy is accepted."""
    selected = acquisition.return_value
    if field == 'manifest':
        selected = replace(selected, wheel=replace(selected.wheel, manifest=replace(
            selected.wheel.manifest, official_hash_verified=True)))
    else:
        value = sdk_wheel.Source.OFFICIAL if field == 'source' else sdk_wheel.Reason.NETWORK
        selected = replace(selected, **{field: value})
    acquisition.return_value = selected
    with pytest.raises(ValueError, match='SDK selection policy drift'):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    assert not (vendored_project / 'build').exists()


def test_vendored_reserve_symlink_rejected(vendored_project, acquisition, tmp_path):
    """A symlinked reserve directory cannot escape the project before packing."""
    source = vendored_project / 'vendor/t-tech-investments'
    destination = tmp_path / 'outside-vendor'
    source.rename(destination)
    source.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_not_called()
    assert not (vendored_project / 'build').exists()


def test_vendored_content_validation(vendored_project, acquisition, monkeypatch):
    """Even a structurally valid ZIP must contain exactly the prepared bytes."""
    original_read = ZipFile.read

    def changed_read(archive, name, *args, **kwargs):
        data = original_read(archive, name, *args, **kwargs)
        return b'changed' if name == 'requirements.txt' else data

    monkeypatch.setattr(ZipFile, 'read', changed_read)
    with pytest.raises(ValueError, match='cloud archive content drift'):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    acquisition.assert_called_once()
    assert not list((vendored_project / 'build').iterdir())


@pytest.mark.parametrize('stage', ['write', 'crc', 'members', 'replace', 'interrupt'])
def test_atomic_vendored_publication(vendored_project, acquisition, monkeypatch, stage):
    """Writing, validation, replacement and cancellation preserve the old artifact."""
    output = builder.build_archive(vendored_project)
    original = output.read_bytes()
    if stage == 'write':
        monkeypatch.setattr(ZipFile, 'write', create_autospec(
            ZipFile.write, spec_set=True, side_effect=PermissionError('write failed')))
    elif stage == 'interrupt':
        monkeypatch.setattr(ZipFile, 'write', create_autospec(
            ZipFile.write, spec_set=True, side_effect=KeyboardInterrupt))
    elif stage == 'crc':
        monkeypatch.setattr(ZipFile, 'testzip', create_autospec(
            ZipFile.testzip, spec_set=True, return_value='handler.py'))
    elif stage == 'members':
        monkeypatch.setattr(ZipFile, 'namelist', create_autospec(
            ZipFile.namelist, spec_set=True, return_value=['handler.py']))
    else:
        monkeypatch.setattr(Path, 'replace', create_autospec(
            Path.replace, spec_set=True, side_effect=PermissionError('replace failed')))
    with pytest.raises((OSError, ValueError, KeyboardInterrupt)):
        builder.build_archive(vendored_project, use_vendored_sdk=True)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]
    acquisition.assert_called_once()


def test_default_builder_never_acquires_sdk(project, monkeypatch):
    """The ordinary bundle remains independent of vendor and network helpers."""
    acquire = create_autospec(sdk_wheel.acquire_sdk, spec_set=True,
                              side_effect=AssertionError('network forbidden'))
    monkeypatch.setattr(sdk_wheel, 'acquire_sdk', acquire)
    builder.build_archive(project)
    acquire.assert_not_called()


@pytest.mark.parametrize('arguments', [['--use-vendored-sdk', '0'],
                                       ['--use-vendored-sdk=1'],
                                       ['--offline-archive=1'], ['--offline-archive', '0']])
def test_archive_cli_rejects_invalid_or_unimplemented_flags(arguments):
    """Boolean flags accept no values; malformed input is never a silent default."""
    with pytest.raises(SystemExit) as error:
        builder.main(arguments)
    assert error.value.code == 2


def test_vendored_cli(vendored_project, acquisition, tmp_path):
    """The explicit CLI flag selects the same local SDK acquisition policy."""
    output = tmp_path / 'chosen.zip'
    builder.main(['--project-root', str(vendored_project), '--output', str(output),
                  '--use-vendored-sdk'])
    acquisition.assert_called_once_with(vendored_project, 'vendored')
    assert output.is_file()


@pytest.mark.parametrize('vendored', [False, True])
def test_offline_root_archive_and_cli(project, monkeypatch, tmp_path, vendored):
    """Offline root contains installed dependencies and never cloud installer inputs."""
    from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
    expected = {'grpc/__init__.py': b'native import',
                'grpcio-1.84.0.dist-info/LICENSE': b'original licence',
                cloud_dependencies.MANIFEST: b'{}'}
    port = create_autospec(
        cloud_dependencies.prepare_payload, spec_set=True, return_value=expected)
    monkeypatch.setattr(cloud_dependencies, 'prepare_payload', port)
    output = tmp_path / 'offline.zip'
    arguments = ['--project-root', str(project), '--output', str(output), '--offline-archive']
    if vendored:
        arguments.append('--use-vendored-sdk')
    builder.main(arguments)
    application = port.call_args.args[1]
    assert application['main.py'] == (project / 'main.py').read_bytes()
    assert application['handler.py'] == (project / 'handler.py').read_bytes()
    assert not any(n.startswith('requirements') for n in application)
    port.assert_called_once_with(project, application, use_vendored_sdk=vendored)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(application) | set(expected)
        assert archive.testzip() is None
        assert all(archive.read(n) == data for n, data in expected.items())


@pytest.mark.parametrize('stage', ['payload', 'write', 'check', 'replace'])
def test_atomic_offline_failure_preserves_previous_zip(project, monkeypatch, stage):
    """Only a fully verified staged ZIP replaces the previous result."""
    from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
    output = builder.build_archive(project)
    original = output.read_bytes()
    port = create_autospec(cloud_dependencies.prepare_payload, spec_set=True, return_value={})
    monkeypatch.setattr(cloud_dependencies, 'prepare_payload', port)
    if stage == 'payload':
        port.side_effect = cloud_dependencies.PayloadError('failed imports')
    elif stage in ('write', 'check'):
        name = '_write_archive' if stage == 'write' else '_check_archive'
        monkeypatch.setattr(builder, name, create_autospec(
            getattr(builder, name), spec_set=True, side_effect=ValueError('invalid ZIP')))
    else:
        monkeypatch.setattr(Path, 'replace', create_autospec(
            Path.replace, spec_set=True, side_effect=OSError('replace failed')))
    with pytest.raises((ValueError, OSError)):
        builder.build_archive(project, offline_archive=True)
    assert output.read_bytes() == original
    assert not list(output.parent.glob('.yandex-archive-*'))


def test_offline_application_symlink_precedes_payload(project, monkeypatch, tmp_path):
    """External application files cannot enter the isolated import root."""
    from scripts import cloud_dependencies  # pylint: disable=import-outside-toplevel
    original = tmp_path / 'real-main.py'
    (project / 'main.py').replace(original)
    (project / 'main.py').symlink_to(original)
    port = create_autospec(cloud_dependencies.prepare_payload, spec_set=True)
    monkeypatch.setattr(cloud_dependencies, 'prepare_payload', port)
    with pytest.raises(ValueError, match='unsafe application'):
        builder.build_archive(project, offline_archive=True)
    port.assert_not_called()


@pytest.mark.parametrize('entrypoint', ['script', 'runpy'])
def test_offline_stdlib_entrypoint_and_other_cwd(project, tmp_path, entrypoint):
    """Mode helpers are lazy and resolve from the builder tree, without PYTHONPATH."""
    repository = Path(__file__).resolve().parents[1]
    shutil.copyfile(repository / 'scripts/build_yandex_archive.py',
                    project / 'scripts/build_yandex_archive.py')
    (project / 'scripts/cloud_dependencies.py').write_text(
        'def prepare_payload(root, application, *, use_vendored_sdk=False):\n'
        '    assert "main.py" in application\n'
        '    assert use_vendored_sdk\n'
        '    return {"dependency-manifest.json": b"{}"}\n')
    output = tmp_path / 'isolated-offline.zip'
    script = str(project / 'scripts/build_yandex_archive.py')
    args = ['--project-root', str(project), '--output', str(output),
            '--offline-archive', '--use-vendored-sdk']
    if entrypoint == 'script':
        command = [sys.executable, '-I', '-S', script, *args]
    else:
        command = [sys.executable, '-I', '-S', '-c',
                   'import runpy,sys; script=sys.argv.pop(1); '
                   'runpy.run_path(script,run_name="__main__")', script, *args]
    result = subprocess.run(command, cwd=tmp_path, env={'PATH': '/usr/bin:/bin'},
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    with ZipFile(output) as archive:
        assert 'dependency-manifest.json' in archive.namelist()
        assert 'requirements.txt' not in archive.namelist()


@pytest.mark.parametrize('probe', ['available', 'unavailable', 'mismatch', 'tls'])
def test_vendored_builder_probe_policy(vendored_project, monkeypatch, caplog, probe):
    """A real forced selection probes only the index; fatal checks prevent publication."""
    port = create_autospec(sdk_wheel.Transport, instance=True, spec_set=True)
    digest = '1' * 64 if probe == 'mismatch' else sdk_wheel.SDK_HASH
    index = (f'<!DOCTYPE html><html><head><title>Links for t-tech-investments</title>'
             f'</head><body><h1>Links for t-tech-investments</h1><a href="'
             f'{sdk_wheel.SIMPLE_URL}{sdk_wheel.FILENAME}#sha256={digest}">'
             f'{sdk_wheel.FILENAME}</a></body></html>').encode()
    port.fetch.return_value = sdk_wheel.Response(sdk_wheel.SIMPLE_URL, 200, 'text/html', index)
    if probe == 'unavailable':
        port.fetch.side_effect = sdk_wheel.AvailabilityError(sdk_wheel.Reason.DNS)
    elif probe == 'tls':
        port.fetch.side_effect = sdk_wheel.WheelError('TLS certificate')

    def worker(stage, root, mode, *, timeout):
        assert stage == 'select' and mode == 'vendored' and timeout == 40
        return sdk_wheel.encode_selection(sdk_wheel.select_source(root, mode, port))

    monkeypatch.setattr(sdk_wheel.sdk_worker, 'run_worker', create_autospec(
        sdk_wheel.sdk_worker.run_worker, spec_set=True, side_effect=worker))
    output = builder.build_archive(vendored_project)
    original = output.read_bytes()
    if probe in ('tls', 'mismatch'):
        with pytest.raises(sdk_wheel.WheelError, match='TLS|approval_required'):
            builder.build_archive(vendored_project, use_vendored_sdk=True)
        assert output.read_bytes() == original
    else:
        builder.build_archive(vendored_project, use_vendored_sdk=True)
        assert 'WARNING' in caplog.text and 'explicit_vendored_mode' in caplog.text
    port.fetch.assert_called_once_with(sdk_wheel.SIMPLE_URL)


@pytest.mark.parametrize('entrypoint', ['script', 'runpy', 'make'])
def test_vendored_stdlib_entrypoints(vendored_project, tmp_path, entrypoint):
    """All helpers resolve from the isolated script tree without site-packages/cwd."""
    repository = Path(__file__).resolve().parents[1]
    for relative in ('Makefile', 'scripts/build_yandex_archive.py', 'scripts/sdk_wheel.py',
                     'scripts/runtime_lock.py', 'scripts/vendor_inventory.py'):
        shutil.copyfile(repository / relative, vendored_project / relative)
    # This offline worker double exercises the actual acquisition decode/validation.
    (vendored_project / 'scripts/sdk_worker.py').write_text(
        'class WorkerTimeout(Exception):\n    pass\n'
        'def run_worker(stage, root, mode, *, timeout):\n'
        '    from scripts import sdk_wheel as sdk\n'
        '    assert stage == "select" and mode == "vendored" and timeout == 40\n'
        '    return sdk.encode_selection(sdk.Selection(sdk.Source.VENDORED,\n'
        '        sdk.Reason.EXPLICIT_VENDORED, sdk.load_vendored_wheel(root), sdk.SDK_HASH))\n',
        encoding='utf-8')
    output = tmp_path / 'isolated.zip'
    script = str(vendored_project / 'scripts/build_yandex_archive.py')
    args = ['--project-root', str(vendored_project), '--output', str(output),
            '--use-vendored-sdk']
    guard = ('import builtins, socket\n'
             'original_import = builtins.__import__\n'
             'def guarded(name, *args, **kwargs):\n'
             '    assert not name.startswith(("t_tech", "packaging", "pytest")), name\n'
             '    return original_import(name, *args, **kwargs)\n'
             'builtins.__import__ = guarded\n'
             'def blocked(*args, **kwargs):\n    raise AssertionError("network forbidden")\n'
             'socket.create_connection = blocked\n')
    if entrypoint == 'script':
        command = [sys.executable, '-I', '-S', script, *args]
    elif entrypoint == 'runpy':
        command = [sys.executable, '-I', '-S', '-c', guard +
                   'import runpy, sys\nscript = sys.argv.pop(1)\n'
                   'runpy.run_path(script, run_name="__main__")\n', script, *args]
    else:
        command = ['make', '-C', str(vendored_project), 'claude-yandex-archive',
                   f'PYTHON={sys.executable}', 'USE_VENDORED_SDK=1']
        output = vendored_project / 'build/yandex-function.zip'
    result = subprocess.run(command, cwd=tmp_path, env={'PATH': '/usr/bin:/bin'},
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'WARNING' in result.stderr and 'explicit_vendored_mode' in result.stderr
    with ZipFile(output) as archive:
        assert archive.read('vendor/t-tech-investments/' + sdk_wheel.FILENAME) == (
            repository / 'vendor/t-tech-investments' / sdk_wheel.FILENAME).read_bytes()


@pytest.fixture(name='application_bundle')
def fixture_application_bundle(tmp_path):
    """Package the real entrypoints and modules, with an independent source tree."""
    repository = Path(__file__).resolve().parents[1]
    project_root = tmp_path / 'application'
    sources = [repository / name for name in (
        'main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt')]
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
from unittest.mock import create_autospec, patch
sys.path.insert(0, sys.argv[1])
with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
        patch('grpc.secure_channel', side_effect=AssertionError('gRPC forbidden')):
    import main
    import handler
    from autorepeater import runner
    from autorepeater.strategies import prepare_strategy, create_strategy
    from autorepeater.execution import ExecutionReceipt, OrderExecutor
    from autorepeater.execution_data import (
        AccountReadiness, ExecutionData, ExecutionSnapshot, TradeRules,
    )
    from autorepeater.strategy_data import PortfolioSnapshot, StrategyData
    prepared = prepare_strategy('ARCHIVE_INDEPENDENT', 'independent-source')
    strategy = create_strategy(prepared)
    assert strategy.prepared == 'independent-source'
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot(())
    data.position_events.side_effect = RuntimeError('finite offline stream')
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    execution.get_destination.return_value = ExecutionSnapshot(
        PortfolioSnapshot(()), Decimal(100), {}, {}, {'rub': Decimal(100)}, {}, (), True)
    execution.get_trade_rules.return_value = {
        'stock': TradeRules(1, 'rub', True, True, Decimal(100), 100, 100)}
    execution.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
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
    assert execution.get_readiness.call_count == 2
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
