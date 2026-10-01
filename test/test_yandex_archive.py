"""Direct, coverage-visible checks of archive selection, writing and entrypoint."""
import runpy
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
        'autorepeater/configs/composite/nested/demo.json': b'{"name":"demo"}',
        'autorepeater/configs/.hidden.json': b'hidden',
        'autorepeater/configs/.hidden/nested.json': b'hidden directory',
        'autorepeater/configs/account/readme.txt': b'not JSON',
        'autorepeater/.hidden.py': b'hidden module',
        'autorepeater/nested/module.py': b'not flat',
        'scripts/build_yandex_archive.py': b'not packaged',
        'test/test_example.py': b'not packaged',
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
    return root


EXPECTED = {
    'main.py', 'handler.py', 'requirements.txt', 'autorepeater/__init__.py',
    'autorepeater/new_strategy.py', 'autorepeater/configs/index.json',
    'autorepeater/configs/account/account.json',
    'autorepeater/configs/composite/nested/demo.json',
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
