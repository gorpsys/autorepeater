"""Offline security and full-suite evidence checks for GitHub sandbox runs."""
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import create_autospec
from urllib.error import URLError
import xml.etree.ElementTree as ET

import pytest

from scripts import sandbox_github as subject

SHA = 'a' * 40
REPO = 'gorpsys/autorepeater'


def pull_request() -> dict[str, object]:
    """Minimal GitHub response; no credential or SDK-shaped facts."""
    return {'number': 27, 'state': 'open',
            'base': {'ref': 'master', 'repo': {'full_name': REPO}},
            'head': {'sha': SHA, 'repo': {'full_name': REPO}}}


def write_junit(path: Path, names: tuple[str, ...] | None = None) -> ET.Element:
    """Write independent pytest-style evidence, never run live collection."""
    suite = ET.Element('testsuite', tests='17', failures='0', errors='0', skipped='0')
    for name in subject.EXPECTED_SCENARIOS if names is None else names:
        ET.SubElement(suite, 'testcase', classname='e2e.test_sandbox', name=name)
    ET.ElementTree(suite).write(path)
    return suite


def test_open_same_repo_exact_head_is_selected() -> None:
    """The approved bootstrap pins the event's actual head, not a merge SHA."""
    target = subject.validate_pull_request(pull_request(), REPO, 27, SHA)
    assert target == subject.TestTarget(SHA, 27)


@pytest.mark.parametrize('field,value', [
    ('state', 'closed'), ('number', True), ('number', 28), ('head', None), ('base', {}),
])
def test_invalid_pull_request_is_rejected(field: str, value: object) -> None:
    """Malformed facts must not silently select another target."""
    document = pull_request()
    document[field] = value
    with pytest.raises(subject.GitHubFailure):
        subject.validate_pull_request(document, REPO, 27)


@pytest.mark.parametrize('side,field,value', [
    ('head', 'repo', {'full_name': 'attacker/fork'}),
    ('head', 'repo', None), ('base', 'repo', {'full_name': 'attacker/fork'}),
    ('base', 'ref', 'develop'), ('head', 'sha', 'main'),
    ('head', 'sha', 'A' * 40), ('head', 'sha', '0' * 40),
])
def test_fork_wrong_base_and_invalid_sha_fail(side: str, field: str, value: object) -> None:
    """No fork gets either a secret or a positive result."""
    document = pull_request()
    document[side] = dict(document[side], **{field: value})
    with pytest.raises(subject.GitHubFailure):
        subject.validate_pull_request(document, REPO, 27)


def test_updated_bootstrap_head_requires_another_approval() -> None:
    """A later commit cannot be substituted under an older approved event."""
    with pytest.raises(subject.GitHubFailure, match='head changed'):
        subject.validate_pull_request(pull_request(), REPO, 27, 'b' * 40)


@pytest.mark.parametrize('permission', ['admin', 'maintain', 'write'])
def test_permission_allows_only_maintainers(permission: str) -> None:
    """Only write-capable initiators may expose the sandbox credential to code."""
    subject.validate_permission({'permission': permission})


@pytest.mark.parametrize('document', [
    None, {}, {'permission': 'read'}, {'permission': 'triage'}, {'permission': True},
])
def test_missing_or_insufficient_permission_is_fatal(document: object) -> None:
    """Read/triage/faulty responses fail closed before the live job."""
    with pytest.raises(subject.GitHubFailure, match='permission'):
        subject.validate_permission(document)


@pytest.mark.parametrize('number', ['', '0', '-1', '01', '27; echo private', '1.0', True])
def test_dispatch_number_is_strict(number: object) -> None:
    """Inputs cannot select an unintended PR or inject workflow output."""
    with pytest.raises(subject.GitHubFailure):
        subject.parse_pr_number(number)


def test_full_seventeen_scenarios_are_required(tmp_path: Path) -> None:
    """Only exact unique expected scenarios with passed teardown are complete."""
    path = tmp_path / 'junit.xml'
    write_junit(path)
    assert subject.verify_junit(path).passed == 17


@pytest.mark.parametrize('change', [
    'empty', 'missing', 'duplicate', 'foreign', 'skip', 'failure', 'error',
    'suite-error', 'wrong-class',
])
def test_incomplete_evidence_never_becomes_success(tmp_path: Path, change: str) -> None:
    """Counts alone do not prove a full run or clean fixture teardown."""
    path = tmp_path / 'junit.xml'
    suite = write_junit(path)
    cases = list(suite)
    if change == 'empty':
        suite.clear()
    elif change == 'missing':
        suite.remove(cases[0])
    elif change == 'duplicate':
        cases[0].set('name', cases[1].get('name', ''))
    elif change == 'foreign':
        cases[0].set('name', 'test_unrelated')
    elif change == 'wrong-class':
        cases[0].set('classname', 'test.something')
    elif change == 'suite-error':
        suite.set('errors', '1')
    else:
        ET.SubElement(cases[0], {'skip': 'skipped'}.get(change, change))
    ET.ElementTree(suite).write(path)
    with pytest.raises(subject.GitHubFailure):
        subject.verify_junit(path)


@pytest.mark.parametrize('content', ['', '<invalid', '<!DOCTYPE x><testsuite/>'])
def test_invalid_or_doctype_xml_is_fatal(tmp_path: Path, content: str) -> None:
    """Evidence parsing rejects unsupported declarations and broken artifacts."""
    path = tmp_path / 'junit.xml'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(subject.GitHubFailure):
        subject.verify_junit(path)


def test_github_client_uses_fixed_endpoint_timeout_and_no_error_details(monkeypatch) -> None:
    """API transport cannot print a token, URL/query or arbitrary server text."""
    opener = create_autospec(subject.urlopen, spec_set=True)
    opener.side_effect = URLError('private credential')
    monkeypatch.setattr(subject, 'urlopen', opener)
    client = subject.GitHubClient(REPO, 'synthetic')
    with pytest.raises(subject.GitHubFailure, match='GitHub API request failed') as error:
        client.read('pulls/27')
    assert 'private credential' not in str(error.value)
    request = opener.call_args.args[0]
    assert request.full_url == f'https://api.github.com/repos/{REPO}/pulls/27'
    assert opener.call_args.kwargs == {'timeout': 15}


def test_evidence_cli_reports_completion_only_after_validation(tmp_path: Path, monkeypatch) -> None:
    """The live job's flag is absent on incomplete evidence."""
    path = tmp_path / 'junit.xml'
    output = tmp_path / 'outputs'
    run_log = tmp_path / 'run.log'
    run_log.write_text('Sandbox scenario cleanup complete\n' * 17 +
                       'Sandbox session cleanup complete\n', encoding='utf-8')
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    write_junit(path)
    arguments = ['evidence', '--junit', str(path), '--run-log', str(run_log)]
    assert subject.main(arguments) == 0
    assert output.read_text(encoding='utf-8') == 'complete=true\npassed=17\n'
    output.unlink()
    write_junit(path, ())
    assert subject.main(arguments) == 1
    assert not output.exists()


def test_prepare_dispatch_checks_both_initiator_and_rerunner(tmp_path: Path, monkeypatch) -> None:
    """Reruns cannot bypass either identity check and selection is fixed once."""
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'inputs': {'pr_number': '27'}}), encoding='utf-8')
    output = tmp_path / 'outputs'
    for key, value in {'GITHUB_EVENT_PATH': str(event), 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                       'GITHUB_REPOSITORY': REPO, 'GITHUB_ACTOR': 'owner',
                       'GITHUB_TRIGGERING_ACTOR': 'rerunner', 'GITHUB_REF': 'refs/heads/master',
                       'GITHUB_OUTPUT': str(output), 'GITHUB_SHA': 'b' * 40,
                       'GH_TOKEN': 'synthetic'}.items():
        monkeypatch.setenv(key, value)
    reader = create_autospec(subject.GitHubClient.read, spec_set=True)
    reader.side_effect = [{'permission': 'admin'}, {'permission': 'write'}, pull_request()]
    monkeypatch.setattr(subject.GitHubClient, 'read', reader)
    assert subject.main(['prepare']) == 0
    assert [call.args[1] for call in reader.call_args_list] == [
        'collaborators/owner/permission', 'collaborators/rerunner/permission', 'pulls/27']
    assert output.read_text(encoding='utf-8') == (
        f'head_sha={SHA}\npr_number=27\ntrusted_sha={"b" * 40}\n')


@pytest.mark.parametrize('scenario_count,session_count', [
    (0, 0), (16, 1), (17, 0), (17, 2), (18, 1),
])
def test_passed_junit_without_exact_cleanup_is_incomplete(
        tmp_path: Path, scenario_count: int, session_count: int) -> None:
    """A valid pass-only XML cannot hide missing finalizers or another execution."""
    path = tmp_path / 'junit.xml'
    write_junit(path)
    run_log = tmp_path / 'run.log'
    run_log.write_text('Sandbox scenario cleanup complete\n' * scenario_count +
                       'Sandbox session cleanup complete\n' * session_count, encoding='utf-8')
    with pytest.raises(subject.GitHubFailure, match='cleanup'):
        subject.verify_evidence(path, run_log)


@pytest.mark.parametrize('repository,token', [('invalid', 'synthetic'), (REPO, '')])
def test_github_credentials_and_repository_are_required(repository: str, token: str) -> None:
    """No credential fallback or attacker-controlled API origin."""
    with pytest.raises(subject.GitHubFailure):
        subject.GitHubClient(repository, token)


@pytest.mark.parametrize('raw', [
    b'{"state":"open"}', b'broken', b'x' * (subject.MAX_EVIDENCE_BYTES + 1),
])
def test_bounded_github_json_boundary(monkeypatch, raw: bytes) -> None:
    """Malformed or oversized API responses are refused without printing them."""
    opener = create_autospec(subject.urlopen, spec_set=True)
    response = opener.return_value.__enter__.return_value
    response.read.return_value = raw
    monkeypatch.setattr(subject, 'urlopen', opener)
    client = subject.GitHubClient(REPO, 'synthetic')
    if raw.startswith(b'{'):
        assert client.read('pulls/27') == {'state': 'open'}
    else:
        with pytest.raises(subject.GitHubFailure):
            client.read('pulls/27')
    response.read.assert_called_once_with(subject.MAX_EVIDENCE_BYTES + 1)


def selection_environment(event_name: str) -> dict[str, str]:
    """One authorized workflow actor; the read port remains an autospec double."""
    return {'GITHUB_ACTOR': 'owner', 'GITHUB_TRIGGERING_ACTOR': 'owner',
            'GITHUB_EVENT_NAME': event_name, 'GITHUB_REF': 'refs/heads/master', 'GITHUB_SHA': SHA}


@pytest.mark.parametrize('event_name', ['push', 'pull_request'])
def test_master_and_bootstrap_select_exact_immutable_sha(event_name: str) -> None:
    """Master and approved bootstrap use the same authorization boundary."""
    client = create_autospec(subject.GitHubClient(REPO, 'synthetic'), spec_set=True)
    client.repository = REPO
    client.read.side_effect = [{'permission': 'admin'}, pull_request()]
    event = {'after': SHA, 'deleted': False} if event_name == 'push' else {
        'pull_request': pull_request()}
    target = subject.select_target(event, selection_environment(event_name), client)
    assert target == subject.TestTarget(SHA, 0 if event_name == 'push' else 27)
    assert client.read.call_count == (1 if event_name == 'push' else 2)


@pytest.mark.parametrize('event_name,event,overrides', [
    ('push', {'after': SHA, 'deleted': True}, {}),
    ('push', {'after': 'b' * 40, 'deleted': False}, {}),
    ('push', {'after': SHA, 'deleted': False}, {'GITHUB_REF': 'refs/heads/untrusted'}),
    ('pull_request', {'pull_request': {'number': 28}}, {}),
    ('pull_request', {'pull_request': {'number': True}}, {}),
    ('schedule', {}, {}), ('workflow_dispatch', {}, {'GITHUB_ACTOR': 'injected/user'}),
])
def test_untrusted_or_unsupported_events_fail_closed(
        event_name: str, event: object, overrides: dict[str, str]) -> None:
    """No deleted/stale push, other bootstrap PR or arbitrary workflow ref is valid."""
    client = create_autospec(subject.GitHubClient(REPO, 'synthetic'), spec_set=True)
    client.repository = REPO
    client.read.return_value = {'permission': 'admin'}
    with pytest.raises(subject.GitHubFailure):
        subject.select_target(event, dict(selection_environment(event_name), **overrides), client)


@pytest.mark.parametrize('kind', [
    'missing', 'large', 'binary', 'symlink', 'root', 'counts', 'nested-errors',
])
def test_unreadable_or_invalid_junit_fails(tmp_path: Path, kind: str) -> None:
    """XML facts, not suite display text, decide whether a collection completed."""
    path = tmp_path / 'junit.xml'
    if kind == 'large':
        path.write_bytes(b'x' * (subject.MAX_EVIDENCE_BYTES + 1))
    elif kind == 'binary':
        path.write_bytes(b'\xff')
    elif kind == 'symlink':
        path.symlink_to(tmp_path / 'absent')
    elif kind == 'root':
        path.write_text('<unrelated/>', encoding='utf-8')
    elif kind in ('counts', 'nested-errors'):
        suite = write_junit(path)
        if kind == 'counts':
            suite.set('tests', '18')
            ET.ElementTree(suite).write(path)
        else:
            suite.set('errors', '1')
            root = ET.Element('testsuites')
            root.append(suite)
            ET.ElementTree(root).write(path)
    with pytest.raises(subject.GitHubFailure):
        subject.verify_junit(path)


@pytest.mark.parametrize('kind', ['missing', 'large', 'binary', 'symlink'])
def test_cleanup_evidence_boundary_is_bounded(tmp_path: Path, kind: str) -> None:
    """Reading malformed diagnostics does not enable acceptance or secret output."""
    path = tmp_path / 'junit.xml'
    write_junit(path)
    run_log = tmp_path / 'run.log'
    if kind == 'large':
        run_log.write_bytes(b'x' * (subject.MAX_EVIDENCE_BYTES + 1))
    elif kind == 'binary':
        run_log.write_bytes(b'\xff')
    elif kind == 'symlink':
        run_log.symlink_to(tmp_path / 'absent')
    with pytest.raises(subject.GitHubFailure):
        subject.verify_evidence(path, run_log)


def test_missing_github_output_fails(monkeypatch) -> None:
    """A validated result cannot disappear silently before status publication."""
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    with pytest.raises(subject.GitHubFailure):
        subject.write_outputs({'complete': 'true'})


def test_cli_missing_event_never_leaks_environment(monkeypatch, capsys) -> None:
    """Missing process inputs produce fixed safe text rather than tracebacks."""
    monkeypatch.delenv('GITHUB_EVENT_PATH', raising=False)
    assert subject.main(['prepare']) == 1
    assert capsys.readouterr().err == (
        'Sandbox GitHub preparation/evidence rejected; no success status\n')


def test_module_entrypoint_is_offline_bounded(monkeypatch) -> None:
    """Cover the real executable boundary without API or sandbox traffic."""
    monkeypatch.delenv('GITHUB_EVENT_PATH', raising=False)
    monkeypatch.setattr(sys, 'argv', ['sandbox_github', 'prepare'])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(subject.__file__)), run_name='__main__')
    assert error.value.code == 1
