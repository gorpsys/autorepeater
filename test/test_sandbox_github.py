"""Offline security and full-suite evidence checks for GitHub sandbox runs."""
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import create_autospec
from urllib.error import URLError

import pytest

from scripts import sandbox_github as subject
from scripts.retry_settings import RunSettings

SHA = 'a' * 40
REPO = 'gorpsys/autorepeater'


def pull_request(number: int = 27) -> dict[str, object]:
    """Minimal GitHub response; no credential or SDK-shaped facts."""
    return {'number': number, 'state': 'open',
            'base': {'ref': 'master', 'repo': {'full_name': REPO}},
            'head': {'sha': SHA, 'repo': {'full_name': REPO}}}


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


@pytest.mark.parametrize('passed', [1, 26])
def test_evidence_cli_reports_completion_only_after_validation(
        tmp_path: Path, monkeypatch, capsys, passed: int) -> None:
    """The generic verifier checks originals and accepts any nonempty actual suite."""
    output = tmp_path / 'outputs'
    settings = RunSettings(timeout_seconds=2400, max_attempts=2, retry_delay_seconds=0)
    verifier = create_autospec(subject.retry_evidence.verify_history, spec_set=True)
    verifier.return_value = passed
    monkeypatch.setattr(subject.retry_evidence, 'verify_history', verifier)
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('E2E_RUN_SETTINGS', settings.to_json())
    arguments = ['evidence', '--junit', str(tmp_path / 'junit.xml'), '--sha', SHA]
    assert subject.main(arguments) == 0
    verifier.assert_called_once_with(tmp_path, settings, expected_sha=SHA)
    assert output.read_text(encoding='utf-8') == f'complete=true\npassed={passed}\n'
    output.unlink()
    verifier.side_effect = ValueError('synthetic private evidence')
    assert subject.main(arguments) == 1
    assert not output.exists()
    assert capsys.readouterr().err == (
        'retry history does not prove full sandbox acceptance; no success status\n')


@pytest.mark.parametrize('raw_settings', ['{}', '{"timeout_seconds":2400,"max_attempts":2}'])
def test_prepare_dispatch_checks_both_initiator_and_rerunner(
        tmp_path: Path, monkeypatch, raw_settings: str) -> None:
    """Reruns check both identities and preserve custom authorized budgets."""
    event = tmp_path / 'event.json'
    inputs = {'pr_number': '27', 'run_settings': raw_settings}
    event.write_text(json.dumps({'inputs': inputs}), encoding='utf-8')
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
    settings = subject.select_settings({'inputs': inputs}, 'workflow_dispatch')
    assert output.read_text(encoding='utf-8') == (
        f'head_sha={SHA}\npr_number=27\ntrusted_sha={"b" * 40}\n'
        f'run_settings={settings.to_json()}\n'
        f'run_step_minutes={settings.run_step_minutes}\n'
        f'job_minutes={settings.job_minutes}\n'
        f'cleanup_minutes={settings.orphan_cleanup_minutes}\n'
        f'cleanup_seconds={settings.orphan_cleanup_timeout_seconds}\n')


@pytest.mark.parametrize('sha', ['', 'main', '0' * 40, 'A' * 40])
def test_evidence_rejects_untrusted_sha_before_reading_artifacts(tmp_path, monkeypatch, sha):
    """A caller must supply the exact trusted commit before any acceptance check."""
    verifier = create_autospec(subject.retry_evidence.verify_history, spec_set=True)
    monkeypatch.setattr(subject.retry_evidence, 'verify_history', verifier)
    monkeypatch.setenv('E2E_RUN_SETTINGS', RunSettings().to_json())
    output = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    assert subject.main(['evidence', '--junit', str(tmp_path / 'junit.xml'), '--sha', sha]) == 1
    assert not output.exists()
    verifier.assert_not_called()


@pytest.mark.parametrize('raw_settings', [None, '', '{}broken', '{"max_attempts":false}',
                                          '{"unknown":1}', '{"max_attempts":2,"max_attempts":3}'])
def test_evidence_requires_parsed_authorized_settings(tmp_path, monkeypatch, raw_settings):
    """Missing or malformed trusted settings cannot fall back to artifact budgets."""
    verifier = create_autospec(subject.retry_evidence.verify_history, spec_set=True)
    monkeypatch.setattr(subject.retry_evidence, 'verify_history', verifier)
    if raw_settings is None:
        monkeypatch.delenv('E2E_RUN_SETTINGS', raising=False)
    else:
        monkeypatch.setenv('E2E_RUN_SETTINGS', raw_settings)
    output = tmp_path / 'outputs'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    assert subject.main(['evidence', '--junit', str(tmp_path / 'junit.xml'), '--sha', SHA]) == 1
    assert not output.exists()
    verifier.assert_not_called()


def test_evidence_requires_fixed_final_junit_name(tmp_path, monkeypatch):
    """A separately supplied XML must not redirect verification to other evidence."""
    verifier = create_autospec(subject.retry_evidence.verify_history, spec_set=True)
    monkeypatch.setattr(subject.retry_evidence, 'verify_history', verifier)
    with pytest.raises(subject.GitHubFailure, match='fixed merged JUnit'):
        subject.evidence_result(tmp_path / 'other.xml', sha=SHA, settings=RunSettings())
    verifier.assert_not_called()


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
    client.read.side_effect = [{'permission': 'admin'}, pull_request(28)]
    event = {'after': SHA, 'deleted': False} if event_name == 'push' else {
        'pull_request': pull_request(28)}
    target = subject.select_target(event, selection_environment(event_name), client)
    assert target == subject.TestTarget(SHA, 0 if event_name == 'push' else 28)
    assert client.read.call_count == (1 if event_name == 'push' else 2)


@pytest.mark.parametrize('event_name,event,overrides', [
    ('push', {'after': SHA, 'deleted': True}, {}),
    ('push', {'after': 'b' * 40, 'deleted': False}, {}),
    ('push', {'after': SHA, 'deleted': False}, {'GITHUB_REF': 'refs/heads/untrusted'}),
    ('pull_request', {'pull_request': {'number': 27}}, {}),
    ('pull_request', {'pull_request': {'number': 29}}, {}),
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
