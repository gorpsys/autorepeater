"""Migration and exact-SHA suite acceptance, with no broker traffic."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import create_autospec
from test.test_sandbox_retry import attempt, store

import pytest

from scripts import sandbox_github, sandbox_retry, sandbox_retry_evidence as evidence
from scripts.sandbox_retry_report import AttemptRecorder

SHA = 'a' * 40
BASE = '49cb338daae8d808706ec9d2480ca3f21ea4dcfd'
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('size', [1, 3, 25])
def test_dynamic_full_collection_and_failed_subset(tmp_path, size):
    """Collection is independent of the attempted subset and has no fixed count."""
    nodes = tuple(f'e2e/test_other.py::test_case_{index}' for index in range(size))
    expected = evidence.SuiteCollection(SHA, nodes)
    store_dynamic(tmp_path, 1, replace(attempt(nodes, nodes[:1]), sha=SHA))
    store_dynamic(tmp_path, 2, replace(attempt(nodes[:1]), sha=SHA))
    states = evidence.inspect_history(tmp_path, require_full=True, expected=expected)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert evidence.verify_history(tmp_path, expected=expected) == size


def store_dynamic(root, number, report):
    """Use original phase fixtures, preserving each node's real module in JUnit."""
    store(root, number, report)
    states = {node: 'failed' if node in evidence.failed_nodes(report) else 'passed'
              for node in report.selected}
    evidence.write_final_junit(states, root / 'attempts' / f'{number:02}' / 'junit.xml')


@pytest.mark.parametrize('fault', [
    'subset', 'extra', 'duplicate', 'sha', 'missing-sha', 'cleanup', 'collection',
    'setup', 'teardown', 'skip', 'incomplete', 'empty', 'phase-extra', 'retry-sha',
])
def test_dynamic_rejects_damaged_originals(tmp_path, fault):
    """A green merged result cannot repair damaged or incomplete originals."""
    nodes = ('e2e/test_other.py::test_one', 'e2e/test_other.py::test_two')
    report = replace(attempt(nodes), sha=SHA)
    if fault in ('subset', 'extra', 'duplicate', 'empty'):
        selected = {'subset': nodes[:1], 'extra': (*nodes, 'e2e/test_other.py::test_extra'),
                    'duplicate': (*nodes, nodes[0]), 'empty': ()}[fault]
        report = replace(attempt(selected), sha=SHA)
    if fault in ('sha', 'missing-sha'):
        report = replace(report, sha='b' * 40 if fault == 'sha' else None)
    if fault == 'cleanup':
        report = replace(report, cleaned=())
    if fault == 'collection':
        report = replace(report, collection_errors=1)
    if fault == 'incomplete':
        report = replace(report, phases=report.phases[:-1])
    if fault in ('setup', 'teardown', 'skip'):
        index = 2 if fault == 'teardown' else 0
        phases = list(report.phases)
        phases[index] = replace(phases[index], outcome='skipped' if fault == 'skip' else 'failed')
        report = replace(report, phases=tuple(phases), exit_status=1)
    if fault == 'phase-extra':
        report = replace(report, phases=(*report.phases, report.phases[0]))
    if fault == 'retry-sha':
        report = replace(attempt(nodes, nodes[:1]), sha=SHA)
    store_dynamic(tmp_path, 1, report)
    if fault == 'retry-sha':
        store_dynamic(tmp_path, 2, replace(attempt(nodes[:1]), sha='b' * 40))
    evidence.write_final_junit(dict.fromkeys(nodes, 'passed'), tmp_path / 'junit.xml')
    with pytest.raises(evidence.RetryFailure):
        evidence.verify_history(tmp_path, expected=evidence.SuiteCollection(SHA, nodes))


def test_v2_cli_requires_collection_and_sha(tmp_path):
    """Selecting the new protocol never falls back to legacy acceptance."""
    with pytest.raises(sandbox_github.GitHubFailure):
        sandbox_github.evidence_result(tmp_path / 'junit.xml', None, 'retry-v2')


@pytest.mark.parametrize('protocol', [None, 'retry-v1', 'retry-v2', 'unknown'])
def test_recorder_protocol_is_explicit(tmp_path, monkeypatch, protocol):
    """Default output is exactly the base schema even if a SHA is available."""
    monkeypatch.delenv('E2E_EVIDENCE_PROTOCOL', raising=False)
    if protocol is not None:
        monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', protocol)
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    recorder = AttemptRecorder(tmp_path / 'report.json')
    if protocol == 'unknown':
        with pytest.raises(evidence.RetryFailure):
            recorder.finish(0)
        return
    recorder.finish(0)
    fields = json.loads(recorder.path.read_text())
    assert ('sha' in fields) is (protocol == 'retry-v2')


def base_verifiers(monkeypatch):
    """Load the immutable base sources in isolated module slots, without edits."""
    modules = {}
    for name in ('sandbox_retry_evidence', 'sandbox_github'):
        source = subprocess.run(['git', 'show', f'{BASE}:scripts/{name}.py'], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout
        module = ModuleType(f'scripts.{name}')
        monkeypatch.setitem(sys.modules, f'scripts.{name}', module)
        code = compile(source, f'<{BASE}:scripts/{name}.py>', 'exec')
        exec(code, module.__dict__)  # pylint: disable=exec-used
        modules[name] = module
    return modules['sandbox_retry_evidence'], modules['sandbox_github']


def offline_legacy_suite(suite, legacy):
    """Make an independent offline suite covering the legacy module and parameters."""
    suite.mkdir()
    # Independent offline execution of every legacy node, never a live fixture.
    functions = ['import pytest']
    groups = {}
    for name in legacy.EXPECTED_SCENARIOS:
        function, _, parameter = name.partition('[')
        groups.setdefault(function, []).append(parameter[:-1] if parameter else None)
    for function, params in groups.items():
        if params == [None]:
            functions.append(f'def {function}(proof):\n    assert proof\n')
        else:
            functions.append(f'@pytest.mark.parametrize("value", {params!r})\n'
                             f'def {function}(proof, value):\n    assert proof and value\n')
    (suite / 'test_sandbox.py').write_text('\n'.join(functions))
    (suite / 'conftest.py').write_text(f'''import pytest
import runpy
hooks = runpy.run_path({str(ROOT / 'e2e/conftest.py')!r})
globals().update({{name: value for name, value in hooks.items() if name.startswith('pytest_')}})
from scripts.sandbox_retry_report import attempt_recorder
@pytest.fixture
def proof(request):
    recorder = attempt_recorder(request.session)
    recorder.entered.append(request.node.nodeid)
    yield True
    recorder.cleaned.append(request.node.nodeid)
    recorder.session_cleanup = True
''')


def test_new_legacy_output_is_accepted_by_unchanged_base_verifier(tmp_path, monkeypatch):
    """Execute real offline pytest phases; load the trusted verifier from the base."""
    monkeypatch.delenv('E2E_EVIDENCE_PROTOCOL', raising=False)
    monkeypatch.delenv('E2E_TESTED_SHA', raising=False)
    legacy, trusted = base_verifiers(monkeypatch)
    offline_legacy_suite(tmp_path / 'e2e', legacy)
    import os  # pylint: disable=import-outside-toplevel
    directory = tmp_path / 'attempts/01'
    directory.mkdir(parents=True)
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS='',
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                       E2E_ATTEMPT_REPORT=str(directory / 'report.json'))
    result = subprocess.run([sys.executable, '-m', 'pytest', '--confcutdir=e2e', 'e2e/',
                             f'--junitxml={directory / "junit.xml"}'], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / 'retry.json').write_text(json.dumps({
        'version': 1, 'attempts': [{'number': 1, 'returncode': 0}]}))
    states = legacy.inspect_history(tmp_path, require_full=True)
    evidence.write_final_junit(states, tmp_path / 'junit.xml')
    assert trusted.evidence_result(
        tmp_path / 'junit.xml', None, 'retry-v1').passed == len(legacy.EXPECTED_NODEIDS)


def collection_file(root, nodes):
    """The expected suite is written separately, before the controller sees attempts."""
    path = root / 'collection.json'
    path.write_text(json.dumps({'version': 1, 'sha': SHA, 'nodeids': nodes}))
    return path


@pytest.mark.parametrize('fault', ['none', 'subset', 'sha', 'legacy', 'missing'])
def test_dynamic_cli_derives_completion(tmp_path, monkeypatch, fault):
    """Only exact dynamic evidence can publish complete; count is diagnostic."""
    nodes = ('e2e/test_other.py::test_one', 'e2e/test_other.py::test_two')
    path = collection_file(tmp_path, nodes)
    report = replace(attempt(nodes[:1] if fault == 'subset' else nodes),
                     sha=None if fault == 'legacy' else SHA)
    store_dynamic(tmp_path, 1, report)
    evidence.write_final_junit(dict.fromkeys(report.selected, 'passed'), tmp_path / 'junit.xml')
    if fault == 'missing':
        path.unlink()
    output = tmp_path / 'output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    result = sandbox_github.main([
        'evidence', '--evidence-format', 'retry-v2', '--junit', str(tmp_path / 'junit.xml'),
        '--collection', str(path), '--sha', 'b' * 40 if fault == 'sha' else SHA])
    assert result == (0 if fault == 'none' else 1)
    if fault == 'none':
        assert output.read_text() == 'complete=true\npassed=2\n'
    else:
        assert not output.exists()


@pytest.mark.parametrize('subset', [False, True])
def test_v2_controller_uses_full_collection_before_any_retry(tmp_path, monkeypatch, subset):
    """A partial initial attempt cannot redefine the expected set in the controller."""
    nodes = ('e2e/test_other.py::test_one', 'e2e/test_other.py::test_two')
    collection_file(tmp_path, nodes)
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v2')
    monkeypatch.setenv('E2E_TESTED_SHA', SHA)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')

    def run(command, **_):
        number = runner.call_count
        selected = nodes[:1] if subset or number == 2 else nodes
        report = replace(attempt(selected, nodes[:1] if number == 1 else ()), sha=SHA)
        store_dynamic(tmp_path, number, report)
        (tmp_path / 'retry.json').unlink()
        return subprocess.CompletedProcess(command, report.exit_status)

    runner = create_autospec(subprocess.run, spec_set=True, side_effect=run)
    sleep = create_autospec(sandbox_retry.time.sleep, spec_set=True)
    runtime = sandbox_retry.RetryRuntime(runner, lambda: 0, sleep)
    if subset:
        with pytest.raises(evidence.RetryFailure, match='full initial'):
            sandbox_retry.run_attempts(['e2e/'], tmp_path, 100, runtime=runtime)
        assert runner.call_count == 1
        sleep.assert_not_called()
    else:
        assert sandbox_retry.run_attempts(['e2e/'], tmp_path, 100, runtime=runtime) == 0
        assert runner.call_count == 2
        assert runner.call_args.args[0][3] == nodes[0]
        assert runner.call_args.kwargs['env']['E2E_EVIDENCE_PROTOCOL'] == 'retry-v2'
        sleep.assert_called_once_with(10)
        assert evidence.verify_history(tmp_path, expected=evidence.SuiteCollection(SHA, nodes)) == 2


def test_protocol_errors_never_downgrade_to_v1(tmp_path, monkeypatch):
    """Unknown versions or missing v2 collection stop before any child process."""
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'unknown')
    with pytest.raises(evidence.RetryFailure, match='protocol'):
        sandbox_retry.selected_collection(tmp_path)
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', 'retry-v2')
    with pytest.raises(evidence.RetryFailure):
        sandbox_retry.selected_collection(tmp_path)
    with pytest.raises(evidence.RetryFailure, match='protocol'):
        evidence.read_report(tmp_path / 'report.json', protocol='unknown')
    with pytest.raises(evidence.RetryFailure, match='full collection'):
        evidence.inspect_history(tmp_path, expected=evidence.SuiteCollection(SHA, ()))
    with pytest.raises(sandbox_github.GitHubFailure, match='protocol'):
        sandbox_github.evidence_result(tmp_path / 'junit.xml', None, 'unknown')
