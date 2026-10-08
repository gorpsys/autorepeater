"""Sandbox workflow trust, serialization and result-publication fitness checks."""
from pathlib import Path
import re

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/sandbox-e2e.yml'


def job(name: str) -> str:
    """Inspect a top-level job without adding a YAML dependency."""
    text = WORKFLOW.read_text(encoding='utf-8')
    section = text.split(f'  {name}:\n', 1)[1]
    return re.split(r'\n  [a-z][a-z-]*:\n', section, maxsplit=1)[0]


def test_shared_workflow_concurrency_covers_all_jobs() -> None:
    """Queue documented max pending runs; never cancel another sandbox session."""
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'queue: max' in text
    assert 'group: autorepeater-sandbox' in text
    assert 'cancel-in-progress: false' in text
    assert text.index('concurrency:') < text.index('jobs:')
    assert 'workflow_dispatch:' in text and 'push:' in text and 'pull_request:' in text
    assert 'pull_request_target' not in text


def test_bootstrap_is_explicitly_approval_gated_for_pr27_only() -> None:
    """Future PR events must not implicitly run sandbox code with credentials."""
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'sandbox-e2e-bootstrap' in job('bootstrap-approval')
    assert 'github.event.pull_request.number == 27' in job('bootstrap-approval')
    assert 'github.event.pull_request.number != 27' in job('manual-required')
    assert 'head.repo.full_name == github.repository' in job('bootstrap-approval')
    assert 'github.ref == \'refs/heads/master\'' in job('prepare')
    assert 'secrets.SANDBOX_TOKEN' not in job('prepare')
    assert 'secrets.SANDBOX_TOKEN' not in job('manual-required')
    assert 'secrets.SANDBOX_TOKEN' not in job('bootstrap-approval')
    assert 'Sandbox E2E' in text


def test_live_is_read_only_pinned_and_bounded() -> None:
    """Every mutation, cleanup and diagnostic upload stays under serialization."""
    text = job('live')
    assert 'statuses: write' not in text
    assert 'contents: read' in text
    assert 'ref: ${{ needs.prepare.outputs.head_sha }}' in text
    assert 'persist-credentials: false' in text
    assert 'timeout-minutes: 55' in text
    assert 'SANDBOX_CI_SERIALIZED: "1"' in text
    assert 'SANDBOX_TOKEN: ${{ secrets.SANDBOX_TOKEN }}' in text
    assert 'make e2e' in text and 'E2E_TIMEOUT=1800' in text
    assert 'PYTEST_ADDOPTS: ""' in text
    assert '--junitxml=' in text
    assert 'scripts.sandbox_lifecycle cleanup --namespace ci' in text
    assert "if: ${{ always() && steps.run.outcome != 'skipped' }}" in text
    assert 'retention-days: 7' in text
    assert 'sandbox-results/*.json' in text and 'sandbox-results/*.xml' in text
    assert 'sandbox-results/cleanup.log' in text
    assert 'sandbox-results/run.log\n' not in text.split('path: |', 1)[1]
    assert "shell: bash" in text


def test_evidence_is_verified_on_a_separate_readonly_runner() -> None:
    """Tested code cannot replace the trusted verifier in the same workspace."""
    text = job('verify')
    assert 'contents: read' in text and 'statuses: write' not in text
    assert 'secrets.SANDBOX_TOKEN' not in text
    assert 'actions/download-artifact@v4' in text
    assert 'ref: ${{ needs.prepare.outputs.trusted_sha }}' in text
    assert 'scripts/sandbox_github.py evidence' in text
    assert '--run-log sandbox-results/cleanup.log' in text
    assert "needs.live.result == 'success'" in text


def test_live_step_deadlines_reserve_cleanup_artifact_and_overhead_time() -> None:
    """Even worst-case step deadlines leave time before the active job expires."""
    text = job('live')
    deadline = int(re.findall(r'^    timeout-minutes: ([0-9]+)$', text, re.MULTILINE)[0])
    steps = re.split(r'^      - ', text.split('    steps:\n', 1)[1], flags=re.MULTILINE)[1:]
    deadlines = [re.findall(r'^        timeout-minutes: ([0-9]+)$', step, re.MULTILINE)
                 for step in steps]
    assert all(len(values) == 1 for values in deadlines)
    assert sum(int(values[0]) for values in deadlines) <= deadline - 7
    assert deadline <= 60
    assert 'Queue and environment approval wait are outside this active job deadline.' in text


def test_publisher_cancellation_after_successful_verification_is_blocked() -> None:
    """An already passed live/verify pair is not enough after workflow cancellation."""
    text = job('publish')
    assert "if: ${{ always() && !cancelled() && needs.prepare.result == 'success' }}" in text
    assert 'RUN_CANCELLED: ${{ cancelled() }}' in text
    assert "process.env.RUN_CANCELLED === 'false'" in text


def test_publication_cannot_execute_tested_head_and_checks_full_success() -> None:
    """Write credentials are only present in inline trusted status jobs."""
    for name in ('pending', 'publish'):
        text = job(name)
        assert 'statuses: write' in text
        assert 'actions/checkout' not in text
        assert 'context: \'Sandbox E2E\'' in text
        assert 'TESTED_SHA: ${{ needs.prepare.outputs.head_sha }}' in text
        assert 'sha: process.env.TESTED_SHA' in text
        assert 'request: { timeout: 15000 }' in text
    text = job('publish')
    assert "process.env.LIVE_RESULT === 'success'" in text
    assert "process.env.COMPLETE === 'true'" in text
    assert "process.env.PASSED === '17'" in text
    assert "process.env.VERIFY_RESULT === 'success'" in text
    assert 'COMPLETE: ${{ needs.verify.outputs.complete }}' in text
    assert "state: complete ? 'success' : 'failure'" in text
    assert 'always()' in text
    assert 'github.sha' not in text
    assert 'getPullRequest' not in text


@pytest.mark.parametrize('live,verify,complete,passed,cancelled,expected', [
    ('success', 'success', 'true', '17', 'false', True),
    ('failure', 'success', 'true', '17', 'false', False),
    ('cancelled', 'success', 'true', '17', 'false', False),
    ('skipped', 'success', 'true', '17', 'false', False),
    ('success', 'failure', 'true', '17', 'false', False),
    ('success', 'cancelled', 'true', '17', 'false', False),
    ('success', 'skipped', 'true', '17', 'false', False),
    ('success', 'success', '', '17', 'false', False),
    ('success', 'success', 'false', '17', 'false', False),
    ('success', 'success', 'true', '16', 'false', False),
    ('success', 'success', 'true', '18', 'false', False),
    ('success', 'success', 'true', '17', 'true', False),
    ('success', 'success', 'true', '17', '', False),
])
def test_inline_publication_predicate_is_fail_closed(
        live: str, verify: str, complete: str, passed: str, cancelled: str, expected: bool) -> None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Inspect the actual inline predicate's deliberately restricted AND grammar."""
    predicate = job('publish').split('const complete = ', 1)[1].split(';', 1)[0]
    clauses = [clause.strip() for clause in predicate.split('&&')]
    parsed = [re.fullmatch(r"process.env.([A-Z_]+) === '([a-z0-9]+)'", clause)
              for clause in clauses]
    assert len(parsed) == 5 and all(match is not None for match in parsed)
    environment = {'LIVE_RESULT': live, 'VERIFY_RESULT': verify,
                   'COMPLETE': complete, 'PASSED': passed, 'RUN_CANCELLED': cancelled}
    result = all(environment[match.group(1)] == match.group(2) for match in parsed
                 if match is not None)
    assert result is expected
