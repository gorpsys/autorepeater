"""Independent full collection, never invoking a broker or a live fixture."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from scripts import sandbox_collection as collector
from scripts import sandbox_retry_evidence as evidence

SHA = 'a' * 40
NODE = 'e2e/test_other.py::test_one'
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault', ['none', 'version', 'sha', 'empty', 'duplicate', 'node', 'field'])
def test_collection_manifest_boundary(tmp_path, fault):
    """Only a complete, nonempty, exact-SHA manifest crosses the verifier boundary."""
    fields = {'version': 1, 'sha': SHA, 'nodeids': [NODE]}
    if fault == 'version':
        fields['version'] = True
    if fault == 'sha':
        fields['sha'] = 'b' * 40
    if fault == 'empty':
        fields['nodeids'] = []
    if fault == 'duplicate':
        fields['nodeids'] = [NODE, NODE]
    if fault == 'node':
        fields['nodeids'] = ['../test_other.py::test_one']
    if fault == 'field':
        fields['complete'] = True
    path = tmp_path / 'collection.json'
    path.write_text(json.dumps(fields))
    if fault == 'none':
        assert evidence.read_collection(path, SHA) == evidence.SuiteCollection(SHA, (NODE,))
    else:
        with pytest.raises(evidence.RetryFailure):
            evidence.read_collection(path, SHA)


@pytest.mark.parametrize('fault', ['none', 'exit', 'errors', 'deselected', 'not-collect', 'empty'])
def test_recorder_refuses_partial_collection(tmp_path, fault):
    """Pytest selection and collection errors cannot produce a full manifest."""
    path = tmp_path / 'collection.json'
    recorder = collector.CollectionRecorder(path, SHA)
    session = SimpleNamespace(testsfailed=int(fault == 'errors'),
                              config=SimpleNamespace(option=SimpleNamespace(
                                  collectonly=fault != 'not-collect')),
                              items=[] if fault == 'empty' else [SimpleNamespace(nodeid=NODE)])
    recorder.pytest_deselected([NODE] if fault == 'deselected' else [])
    recorder.pytest_sessionfinish(session, int(fault == 'exit'))
    assert path.exists() is (fault == 'none')


@pytest.mark.parametrize('fault', ['sha', 'existing', 'symlink', 'dirty', 'untracked', 'pytest'])
def test_collector_guards_precede_acceptance(tmp_path, monkeypatch, fault):
    """Typed process doubles prove fixed command/environment and failure handling."""
    run = create_autospec(subprocess.run, spec_set=True)
    monkeypatch.setattr(collector.subprocess, 'run', run)
    path = tmp_path / 'collection.json'
    if fault == 'existing':
        path.write_text('{}')
    if fault == 'symlink':
        path.symlink_to(tmp_path / 'absent.json')
    run.side_effect = [
        subprocess.CompletedProcess([], 0, 'b' * 40 if fault == 'sha' else SHA),
        subprocess.CompletedProcess([], int(fault == 'dirty')),
        subprocess.CompletedProcess([], 0, 'extra.py' if fault == 'untracked' else ''),
        subprocess.CompletedProcess([], 1),
    ]
    with pytest.raises(evidence.RetryFailure):
        collector.collect_suite(tmp_path, SHA, path)
    if fault == 'pytest':
        command = run.call_args.args[0]
        environment = run.call_args.kwargs['env']
        assert command[-1] == 'e2e/' and '--collect-only' in command
        assert environment['PYTEST_ADDOPTS'] == ''
        assert environment['PYTEST_PLUGINS'] == ''
        assert environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
        assert 'E2E_ATTEMPT_REPORT' not in environment


@pytest.mark.parametrize('fault', [
    'none', 'dirty-config', 'untracked-config', 'deselected', 'collection-error', 'empty',
])
def test_actual_offline_collection_cannot_run_fixtures(tmp_path, monkeypatch, fault):
    """Run a committed tiny repo with hostile inherited filters and fatal fixtures."""
    suite = tmp_path / 'e2e'
    suite.mkdir()
    (suite / 'test_other.py').write_text(
        'import pytest\n@pytest.fixture(autouse=True)\n'
        'def no_live():\n    raise AssertionError("fixture executed")\n'
        'def test_one():\n    pass\ndef test_two():\n    pass\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    if fault == 'deselected':
        (suite / 'conftest.py').write_text(
            'def pytest_collection_modifyitems(config, items):\n'
            '    removed = items.pop()\n'
            '    config.hook.pytest_deselected(items=[removed])\n')
    if fault == 'collection-error':
        (suite / 'test_other.py').write_text('def syntax_error(\n')
    if fault == 'empty':
        (suite / 'test_other.py').write_text('"""No tests."""\n')
    for arguments in (['init', '-q'], ['add', 'e2e', 'pytest.ini'],
                      ['-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid',
                       'commit', '-qm', 'offline fixture']):
        subprocess.run(['git', *arguments], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=tmp_path, check=True,
                         capture_output=True, text=True).stdout.strip()
    if fault == 'dirty-config':
        (tmp_path / 'pytest.ini').write_text('[pytest]\nxfail_strict=true\n')
    if fault == 'untracked-config':
        (tmp_path / 'extra.cfg').write_text('[pytest]\n')
    monkeypatch.setenv('PYTHONPATH', str(ROOT))
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k absent')
    monkeypatch.setenv('PYTEST_PLUGINS', 'nonexistent_plugin')
    path = tmp_path / 'collection.json'
    monkeypatch.chdir(tmp_path)
    assert collector.main(['--sha', sha, '--output', str(path)]) == (0 if fault == 'none' else 1)
    if fault == 'none':
        assert evidence.read_collection(path, sha).nodeids == (
            NODE, 'e2e/test_other.py::test_two')
    else:
        assert not path.exists()


def test_collection_cli_failure_is_safe(monkeypatch, capsys):
    """A bad SHA and missing collection cannot emit an acceptance artifact."""
    monkeypatch.setattr(sys, 'argv', ['sandbox_collection', '--sha', 'invalid',
                                      '--output', 'unused.json'])
    assert collector.main() == 1
    assert capsys.readouterr().err == 'Full exact-SHA sandbox collection failed\n'


@pytest.mark.parametrize('enabled', [False, True])
def test_plugin_registration_is_explicit(tmp_path, monkeypatch, enabled):
    """Only the collector's own subprocess opts into the full-suite recorder."""
    manager = create_autospec(pytest.PytestPluginManager, instance=True, spec_set=True)
    monkeypatch.delenv('E2E_COLLECTION_REPORT', raising=False)
    if enabled:
        monkeypatch.setenv('E2E_COLLECTION_REPORT', str(tmp_path / 'collection.json'))
        monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    collector.pytest_configure(SimpleNamespace(pluginmanager=manager))
    if enabled:
        manager.register.assert_called_once()
        recorder = manager.register.call_args.args[0]
        assert isinstance(recorder, collector.CollectionRecorder)
        assert recorder.sha == SHA and recorder.path == tmp_path / 'collection.json'
    else:
        manager.register.assert_not_called()


def test_module_entrypoint_is_offline(monkeypatch):
    """The real CLI entrypoint rejects an invalid SHA before any process call."""
    monkeypatch.setattr(sys, 'argv', ['sandbox_collection', '--sha', 'invalid',
                                      '--output', 'unused.json'])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(collector.__file__)), run_name='__main__')
    assert error.value.code == 1
