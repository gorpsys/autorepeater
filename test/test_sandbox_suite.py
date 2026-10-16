"""Full collection is independent of attempts, their size and their final outcomes."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
from test.test_sandbox_retry import attempt, store

import pytest

from scripts import sandbox_retry_evidence as evidence
from scripts import sandbox_collection as collector

SHA = 'a' * 40


def collection(size):
    """Two arbitrary suite sizes exercise the same acceptance rule."""
    return evidence.SuiteCollection(SHA, tuple(
        f'e2e/test_sandbox{"_multicurrency" if index % 2 else ""}.py::test_case_{index}'
        for index in range(size)))


@pytest.mark.parametrize('size', [2, 5])
@pytest.mark.parametrize('retry', [False, True])
def test_exact_dynamic_suite_and_legal_failed_subset(tmp_path, size, retry):
    """A full first attempt may be followed by only its proven failed subset."""
    expected = collection(size)
    failed = expected.nodeids[:1] if retry else ()
    store(tmp_path, 1, replace(attempt(expected.nodeids, failed), sha=SHA))
    if retry:
        store(tmp_path, 2, replace(attempt(failed), sha=SHA))
    states = evidence.inspect_history(tmp_path, expected=expected, require_full=True)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, expected=expected) == size


@pytest.mark.parametrize('fault', [
    'missing', 'extra', 'duplicate', 'skip', 'xfail', 'setup', 'teardown', 'collection',
    'cleanup', 'canceled', 'incomplete', 'sha', 'empty-sha', 'illegal-retry',
    'duplicate-cleanup', 'duplicate-entered',
])
def test_dynamic_full_suite_rejects_every_incomplete_original(tmp_path, fault):
    """A fabricated green merged XML cannot repair invalid original evidence."""
    expected = collection(3)
    report = replace(attempt(expected.nodeids), sha=SHA)
    if fault in ('missing', 'extra', 'duplicate'):
        nodes = {'missing': expected.nodeids[:-1],
                 'extra': (*expected.nodeids, 'e2e/test_sandbox.py::test_extra'),
                 'duplicate': (*expected.nodeids, expected.nodeids[0])}[fault]
        report = replace(attempt(nodes), sha=SHA)
    if fault in ('skip', 'xfail', 'setup', 'teardown'):
        index = {'skip': 1, 'xfail': 1, 'setup': 0, 'teardown': 2}[fault]
        phases = list(report.phases)
        phases[index] = replace(phases[index], outcome=(
            'skipped' if fault in ('skip', 'xfail') else 'failed'))
        report = replace(report, phases=tuple(phases), exit_status=1)
    if fault == 'collection':
        report = replace(report, collection_errors=1)
    if fault == 'cleanup':
        report = replace(report, session_cleanup=False)
    if fault == 'canceled':
        report = replace(report, exit_status=2)
    if fault == 'incomplete':
        report = replace(report, phases=report.phases[:-1])
    if fault == 'duplicate-cleanup':
        report = replace(report, cleaned=(*report.cleaned, report.cleaned[0]))
    if fault == 'duplicate-entered':
        report = replace(report, entered=(*report.entered, report.entered[0]))
    report = replace(report, sha={'sha': 'b' * 40, 'empty-sha': None}.get(fault, report.sha))
    store(tmp_path, 1, report)
    if fault == 'illegal-retry':
        store(tmp_path, 2, replace(attempt(expected.nodeids[:1]), sha=SHA))
    evidence.write_final_junit(dict.fromkeys(expected.nodeids, 'passed'), tmp_path / 'junit.xml')
    with pytest.raises(evidence.RetryFailure):
        evidence.verify_history(tmp_path, expected=expected)


@pytest.mark.parametrize('fault', ['empty', 'duplicate', 'sha', 'zero', 'filtered', 'error'])
def test_collection_manifest_must_prove_full_exact_sha(tmp_path, fault):
    """The expected set comes from full collection, never a successful subset."""
    data = {'version': 1, 'sha': SHA, 'nodeids': list(collection(2).nodeids)}
    if fault == 'empty':
        data['nodeids'] = []
    if fault == 'duplicate':
        data['nodeids'] *= 2
    if fault in ('sha', 'zero'):
        data['sha'] = 'b' * 40 if fault == 'sha' else '0' * 40
    if fault in ('filtered', 'error'):
        data[fault] = True
    path = tmp_path / 'collection.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(evidence.RetryFailure):
        evidence.read_collection(path, SHA)


@pytest.mark.parametrize('fault', ['none', 'empty', 'deselected', 'error', 'exit', 'not-collect'])
def test_collector_records_only_unfiltered_success(tmp_path, fault):
    """Collection does not enter live fixtures and cannot publish partial acceptance."""
    path = tmp_path / 'collection.json'
    recorder = collector.CollectionRecorder(path, SHA)
    nodes = collection(2).nodeids
    session = SimpleNamespace(items=[SimpleNamespace(nodeid=n) for n in nodes], testsfailed=0,
                              config=SimpleNamespace(option=SimpleNamespace(collectonly=True)))
    if fault == 'deselected':
        recorder.pytest_deselected([object()])
    else:
        recorder.pytest_deselected([])
    if fault == 'empty':
        session.items = []
    if fault == 'error':
        session.testsfailed = 1
    if fault == 'not-collect':
        session.config.option.collectonly = False
    recorder.pytest_sessionfinish(session, 1 if fault == 'exit' else 0)
    assert path.exists() == (fault == 'none')
    if path.exists():
        assert evidence.read_collection(path, SHA).nodeids == nodes


@pytest.mark.parametrize('fault', [
    'none', 'sha', 'existing', 'failed', 'empty', 'missing', 'dirty', 'untracked'])
def test_full_collection_owns_command_and_rejects_invalid_checkout(tmp_path, monkeypatch, fault):
    """Caller filters and inherited pytest arguments cannot define expected IDs."""
    output = tmp_path / 'collection.json'
    if fault == 'existing':
        output.write_text('{}', encoding='utf-8')
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == 'git':
            if command[1] != 'rev-parse':
                return collector.subprocess.CompletedProcess(
                    command, int(fault == 'dirty'),
                    stdout='new.py' if fault == 'untracked' else '')
            return collector.subprocess.CompletedProcess(command, 0, stdout=(
                'b' * 40 if fault == 'sha' else SHA) + '\n')
        if fault != 'missing':
            output.write_text(json.dumps({'version': 1, 'sha': SHA, 'nodeids': (
                [] if fault == 'empty' else collection(2).nodeids)}), encoding='utf-8')
        return collector.subprocess.CompletedProcess(command, int(fault == 'failed'))

    monkeypatch.setattr(collector.subprocess, 'run', create_autospec(
        collector.subprocess.run, spec_set=True, side_effect=run))
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k hidden')
    monkeypatch.setenv('PYTEST_PLUGINS', 'untrusted')
    monkeypatch.setenv('E2E_ATTEMPT_REPORT', '/tmp/unrelated-report')
    if fault == 'none':
        collector.collect_suite(tmp_path, SHA, output)
        command, kwargs = calls[-1]
        assert command[2:] == ['pytest', '-p', 'scripts.sandbox_collection', '--collect-only',
                               '-q', '-o', 'addopts=', '--confcutdir=e2e', 'e2e/']
        assert kwargs['env']['PYTEST_ADDOPTS'] == kwargs['env']['PYTEST_PLUGINS'] == ''
        assert kwargs['env']['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
        assert 'E2E_ATTEMPT_REPORT' not in kwargs['env']
    else:
        with pytest.raises(evidence.RetryFailure):
            collector.collect_suite(tmp_path, SHA, output)


def test_collection_plugin_and_cli_boundaries(tmp_path, monkeypatch):
    """Only the collector's explicit path enables recording; CLI failures stay safe."""
    manager = SimpleNamespace(register=create_autospec(lambda plugin: None, spec_set=True))
    config = SimpleNamespace(pluginmanager=manager)
    monkeypatch.delenv('E2E_COLLECTION_REPORT', raising=False)
    collector.pytest_configure(config)
    manager.register.assert_not_called()
    monkeypatch.setenv('E2E_COLLECTION_REPORT', str(tmp_path / 'collection.json'))
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    collector.pytest_configure(config)
    assert manager.register.call_args.args[0].sha == SHA
    collect = create_autospec(collector.collect_suite, spec_set=True)
    monkeypatch.setattr(collector, 'collect_suite', collect)
    args = ['--sha', SHA, '--output', str(tmp_path / 'collection.json')]
    assert collector.main(args) == 0
    collect.assert_called_once_with(Path.cwd(), SHA, tmp_path / 'collection.json')
    collect.side_effect = evidence.RetryFailure('fixed')
    assert collector.main(args) == 1


def test_collection_module_propagates_failure(monkeypatch, tmp_path):
    """A bad SHA cannot produce a successful module exit or artifact."""
    import runpy  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(sys, 'argv', [
        'sandbox_collection', '--sha', 'bad', '--output', str(tmp_path / 'collection.json')])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(collector.__file__, run_name='__main__')
    assert caught.value.code == 1


@pytest.mark.parametrize('expected', [None, evidence.SuiteCollection(SHA, ())])
def test_full_verifier_requires_an_independent_nonempty_collection(tmp_path, expected):
    """A locally valid subset is never its own full-suite oracle."""
    with pytest.raises(evidence.RetryFailure, match='independent full'):
        evidence.inspect_history(tmp_path, require_full=True, expected=expected)


def test_duplicate_final_junit_cannot_hide_behind_full_originals(tmp_path):
    """The final merged XML has the same unique-ID contract as every original attempt."""
    import xml.etree.ElementTree as ET  # pylint: disable=import-outside-toplevel
    expected = collection(2)
    store(tmp_path, 1, replace(attempt(expected.nodeids), sha=SHA))
    path = tmp_path / 'junit.xml'
    evidence.write_final_junit(dict.fromkeys(expected.nodeids, 'passed'), path)
    tree = ET.parse(path)
    tree.getroot().append(tree.getroot()[0])
    tree.getroot().set('tests', '3')
    tree.write(path)
    with pytest.raises(evidence.RetryFailure, match='duplicate'):
        evidence.verify_history(tmp_path, expected=expected)


def test_non_strict_xpass_is_not_recorded_as_pass():
    """A pytest passed phase with wasxfail cannot become successful original evidence."""
    from e2e import conftest  # pylint: disable=import-outside-toplevel
    from scripts.sandbox_retry_report import AttemptRecorder  # pylint: disable=import-outside-toplevel
    recorder = AttemptRecorder(Path('/tmp/unused-xpass-report'))
    node = collection(1).nodeids[0]
    item = SimpleNamespace(nodeid=node, session=SimpleNamespace(config=SimpleNamespace(
        sandbox_retry_recorder=recorder)))
    result = SimpleNamespace(when='call', outcome='passed', wasxfail='expected')
    hook = conftest.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: result))
    assert recorder.phases[0].outcome == 'skipped'
