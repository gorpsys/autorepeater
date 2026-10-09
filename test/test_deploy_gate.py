"""Production credentials require successful checks on the exact current master SHA."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from scripts import deploy_gate as gate
from scripts.deploy_support import DeployError

SHA = 'a' * 40
REPO = 'gorpsys/autorepeater'


def facts():
    """Use actual GitHub response shapes, with explicit trusted producers."""
    checks = [{'name': name, 'head_sha': SHA, 'status': 'completed',
               'conclusion': 'success', 'app': {'slug': 'github-actions'}}
              for name in ('Lint', 'Offline checks')]
    statuses = {'sha': SHA, 'statuses': [
        {'context': 'Sandbox E2E', 'state': 'success',
         'creator': {'login': 'github-actions[bot]'}}]}
    return checks, statuses


def event():
    """workflow_run is not automatically trusted just because it uses master code."""
    return {'workflow_run': {
        'name': 'Sandbox live E2E', 'event': 'push', 'head_branch': 'master', 'head_sha': SHA,
        'status': 'completed', 'conclusion': 'success', 'head_repository': {'full_name': REPO},
    }}


def environment():
    """No cloud or sandbox credential participates in the read-only gate."""
    return {'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/master',
            'GITHUB_SHA': SHA, 'GITHUB_EVENT_NAME': 'workflow_run'}


@pytest.mark.parametrize('kind', ['workflow_run', 'workflow_dispatch'])
def test_target_is_pinned_to_trusted_master_event(kind):
    """Both accepted triggers resolve to immutable master commits."""
    env = dict(environment(), GITHUB_EVENT_NAME=kind)
    assert gate.select_sha(event() if kind == 'workflow_run' else {}, env) == SHA


@pytest.mark.parametrize('damage', [
    'pr', 'fork', 'failed', 'running', 'other-workflow', 'other-branch',
    'event-branch', 'invalid-sha', 'other-repo', 'unknown-event',
])
def test_unsafe_trigger_cannot_reach_cloud(damage):
    """Forks and unrelated workflows cannot request production credentials."""
    doc, env = event(), environment()
    run = doc['workflow_run']
    if damage == 'pr':
        run['event'] = 'pull_request'
    elif damage == 'fork':
        run['head_repository']['full_name'] = 'fork/autorepeater'
    elif damage == 'failed':
        run['conclusion'] = 'failure'
    elif damage == 'running':
        run['status'] = 'in_progress'
    elif damage == 'other-workflow':
        run['name'] = 'Pylint'
    elif damage == 'other-branch':
        run['head_branch'] = 'feature'
    elif damage == 'event-branch':
        env['GITHUB_REF'] = 'refs/heads/feature'
    elif damage == 'invalid-sha':
        run['head_sha'] = 'master'
    elif damage == 'other-repo':
        env['GITHUB_REPOSITORY'] = 'fork/autorepeater'
    else:
        env['GITHUB_EVENT_NAME'] = 'push'
    with pytest.raises(DeployError):
        gate.select_sha(doc, env)


def test_all_three_checks_required_on_exact_sha():
    """Missing evidence is pending, not successful."""
    checks, statuses = facts()
    assert not gate.pending_checks(checks, statuses, SHA)
    assert gate.pending_checks([], {'sha': SHA, 'statuses': []}, SHA) == gate.REQUIRED_CHECKS


@pytest.mark.parametrize('damage', [
    'lint-failure', 'lint-skipped', 'e2e-error', 'wrong-check-sha',
    'wrong-status-sha', 'untrusted-check', 'untrusted-status', 'duplicate',
])
def test_bad_or_ambiguous_required_facts_are_fatal(damage):
    """Identity and producer mismatches fail closed."""
    checks, statuses = facts()
    if damage.startswith('lint-'):
        checks[0]['conclusion'] = damage.removeprefix('lint-')
    elif damage == 'e2e-error':
        statuses['statuses'][0]['state'] = 'error'
    elif damage == 'wrong-check-sha':
        checks[0]['head_sha'] = 'b' * 40
    elif damage == 'wrong-status-sha':
        statuses['sha'] = 'b' * 40
    elif damage == 'untrusted-check':
        checks[0]['app']['slug'] = 'custom-app'
    elif damage == 'untrusted-status':
        statuses['statuses'][0]['creator']['login'] = 'human'
    else:
        checks.append(deepcopy(checks[0]))
    with pytest.raises(DeployError):
        gate.pending_checks(checks, statuses, SHA)


def test_pending_checks_never_count_as_success():
    """Every required check must finish successfully."""
    checks, statuses = facts()
    checks[0].update(status='in_progress', conclusion=None)
    statuses['statuses'][0]['state'] = 'pending'
    assert gate.pending_checks(checks, statuses, SHA) == ('Lint', 'Sandbox E2E')


def test_superseded_master_is_skipped_without_reading_checks():
    """Out-of-order completions cannot deploy older master heads."""
    client = create_autospec(gate.GitHub, instance=True, spec_set=True)
    client.master_sha.return_value = 'b' * 40
    assert gate.wait_ready(client, SHA, 30) is False
    client.checks.assert_not_called()
    client.statuses.assert_not_called()


def test_wait_uses_one_deadline_and_rechecks_master():
    """Waiting does not reset its deadline or stop checking freshness."""
    client = create_autospec(gate.GitHub, instance=True, spec_set=True)
    client.master_sha.return_value = SHA
    checks, statuses = facts()
    client.checks.side_effect = [[], checks]
    client.statuses.side_effect = [{'sha': SHA, 'statuses': []}, statuses]
    ticks = iter([0, 0, 15])
    pauses = []
    assert gate.wait_ready(client, SHA, 30, clock=lambda: next(ticks), sleep=pauses.append)
    assert pauses == [15]
    assert client.master_sha.call_count == 2


def test_pending_checks_timeout_without_deploying():
    """An unfinished check cannot extend the gate indefinitely."""
    client = create_autospec(gate.GitHub, instance=True, spec_set=True)
    client.master_sha.return_value = SHA
    client.checks.return_value = []
    client.statuses.return_value = {'sha': SHA, 'statuses': []}
    ticks = iter([0, 0, 30])
    with pytest.raises(DeployError, match='timeout'):
        gate.wait_ready(client, SHA, 30, clock=lambda: next(ticks), sleep=lambda _: None)


def test_deployment_workflow_keeps_existing_ci_and_archive_contract():
    """Cloud deployment is separate from unchanged required CI checks."""
    root = Path(__file__).resolve().parents[1]
    text = (root / '.github/workflows/deploy-prod.yml').read_text()
    assert 'workflow_run:' in text and 'workflows: [Sandbox live E2E]' in text
    assert "github.event.workflow_run.event == 'push'" in text
    assert 'workflow_dispatch:' in text and 'pull_request:' not in text
    assert 'environment:' not in text
    assert 'id-token: write' in text and 'cancel-in-progress: false' in text
    assert 'make claude-yandex-archive' in text
    assert 'build/yandex-function.zip' in text
    assert 'make test' not in text and 'make coverage' not in text
    assert 'ref: ${{ needs.gate.outputs.sha }}' in text
    assert 'persist-credentials: false' in text
    assert 'python3 -m scripts.deploy_gate --check-once' in text


def github_transport(monkeypatch, responses):
    """Exercise the reader without network calls or unrestricted mocks."""
    reader = create_autospec(gate.request_json, spec_set=True, side_effect=responses)
    monkeypatch.setattr(gate, 'request_json', reader)
    return gate.GitHub('test-credential'), reader


def test_reader_uses_full_statuses_and_keeps_newest_result(monkeypatch):
    """Combined status lacks creator; historical successes cannot override latest pending."""
    success = facts()[1]['statuses'][0]
    pending = dict(success, state='pending')
    client, reader = github_transport(monkeypatch, [
        {'commit': {'sha': SHA}}, [pending, success],
    ])
    assert client.master_sha() == SHA
    result = client.statuses(SHA)
    assert result == {'sha': SHA, 'statuses': [pending], 'total_count': 1}
    assert gate.pending_checks(facts()[0], result, SHA) == ('Sandbox E2E',)
    requests = [call.args[0] for call in reader.call_args_list]
    assert requests[0].get_header('Authorization') == 'Bearer test-credential'
    assert requests[1].full_url.endswith(f'/commits/{SHA}/statuses?per_page=100&page=1')


def test_readers_paginate_without_hiding_required_facts(monkeypatch):
    """Required results outside the first page are still considered."""
    filler = [{'context': f'unrelated {index}', 'state': 'success'} for index in range(100)]
    client, reader = github_transport(monkeypatch, [
        filler, facts()[1]['statuses'],
        {'total_count': 2, 'check_runs': facts()[0][:1]},
        {'total_count': 2, 'check_runs': facts()[0][1:]},
    ])
    statuses = client.statuses(SHA)
    assert not gate.pending_checks(client.checks(SHA), statuses, SHA)
    assert reader.call_count == 4


@pytest.mark.parametrize('data', [
    {'total_count': True, 'check_runs': []},
    {'total_count': -1, 'check_runs': []},
    {'total_count': '1', 'check_runs': []},
    {'total_count': 1, 'check_runs': None},
    {'total_count': 1, 'check_runs': []},
    {'total_count': 0, 'check_runs': [1]},
])
def test_invalid_check_pagination_is_fatal(monkeypatch, data):
    """Incomplete and malformed check lists never authorize deployment."""
    client, _ = github_transport(monkeypatch, [data])
    with pytest.raises(DeployError):
        client.checks(SHA)


@pytest.mark.parametrize('data', [{}, [None], [{'context': ''}], list(range(101))])
def test_invalid_status_pagination_is_fatal(monkeypatch, data):
    """Status endpoint must return valid bounded status records."""
    client, _ = github_transport(monkeypatch, [data])
    with pytest.raises(DeployError):
        client.statuses(SHA)


@pytest.mark.parametrize('kind', ['checks', 'statuses'])
def test_excessive_pagination_is_bounded(monkeypatch, kind):
    """Malicious response sizes cannot cause unlimited polling."""
    data = ({'total_count': 21, 'check_runs': [{}]} if kind == 'checks'
            else [{'context': 'filler'}] * 100)
    client, reader = github_transport(monkeypatch, [data] * 20)
    with pytest.raises(DeployError, match='too many'):
        getattr(client, kind)(SHA)
    assert reader.call_count == 20


@pytest.mark.parametrize('data', [
    {'sha': SHA, 'statuses': None},
    {'sha': SHA, 'statuses': [], 'total_count': True},
    {'sha': SHA, 'statuses': [], 'total_count': 1},
    {'sha': SHA, 'statuses': facts()[1]['statuses'] * 2},
    {'sha': SHA, 'statuses': [{'context': 'Sandbox E2E', 'state': 'success'}]},
])
def test_incomplete_status_evidence_is_not_success(data):
    """Missing producer evidence in a combined response must fail closed."""
    with pytest.raises(DeployError):
        gate.pending_checks(facts()[0], data, SHA)


@pytest.mark.parametrize('mode', ['ready', 'stale', 'check-once-stale', 'failure', 'invalid'])
def test_main_outputs_only_authorized_facts(monkeypatch, tmp_path, capsys, mode):
    """CLI entrypoint reports safe errors and separates stale skips from strict rechecks."""
    doc = tmp_path / 'event.json'
    doc.write_text(json.dumps(event()), encoding='utf-8')
    output = tmp_path / 'output.txt'
    for key, value in dict(environment(), GITHUB_EVENT_PATH=str(doc),
                           GITHUB_OUTPUT=str(output), GH_TOKEN='test-credential').items():
        monkeypatch.setenv(key, value)
    client = create_autospec(gate.GitHub, instance=True, spec_set=True)
    client.master_sha.return_value = 'b' * 40 if 'stale' in mode else SHA
    client.checks.return_value, client.statuses.return_value = facts()
    factory = create_autospec(gate.GitHub, spec_set=True, return_value=client)
    monkeypatch.setattr(gate, 'GitHub', factory)
    if mode == 'failure':
        client.checks.side_effect = DeployError('required check failed')
    if mode == 'invalid':
        doc.write_text('broken JSON', encoding='utf-8')
    result = gate.main(['--check-once'] if mode == 'check-once-stale' else [])
    if mode in ('ready', 'stale'):
        assert result == 0
        assert output.read_text() == f'sha={SHA}\nready={str(mode == "ready").lower()}\n'
    else:
        assert result == 1 and not output.exists()
    assert 'test-credential' not in capsys.readouterr().err
