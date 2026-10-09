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
    assert 'workflow_dispatch:' in text and 'push:' in text
    assert 'pull_request:' not in text
    assert 'pull_request_target' not in text


def test_prs_require_manual_dispatch_without_automatic_failed_marker() -> None:
    """Opening or updating a PR must not create deliberately failed workflows."""
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'pull_request:' not in text and 'pull_request_target' not in text
    assert 'manual-required:' not in text and 'bootstrap-approval:' not in text
    assert 'sandbox-e2e-bootstrap' not in text
    assert 'exit 1' not in text.split('  prepare:', 1)[0]
    assert 'github.ref == \'refs/heads/master\'' in job('prepare')
    assert 'secrets.SANDBOX_TOKEN' not in job('prepare')
    assert 'Sandbox E2E' in text


def test_preparation_uses_only_trusted_master_workflow_code() -> None:
    """No bootstrap ancestors or PR-head code may participate in preparation."""
    text = job('prepare')
    assert 'needs:' not in text
    assert "if: ${{ !cancelled() && github.ref == 'refs/heads/master' }}" in text
    assert 'ref: ${{ github.sha }}' in text
    assert 'github.event.pull_request' not in text


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
    assert 'sandbox-results/attempts/**/cleanup.log' in text
    assert 'sandbox-results/attempts/**/*.json' in text
    assert 'sandbox-results/attempts/**/*.xml' in text
    assert 'sandbox-results/run.log\n' not in text.split('path: |', 1)[1]
    assert "shell: bash" in text


def test_evidence_is_verified_on_a_separate_readonly_runner() -> None:
    """Tested code cannot replace the trusted verifier in the same workspace."""
    text = job('verify')
    assert 'contents: read' in text and 'statuses: write' not in text
    assert 'secrets.SANDBOX_TOKEN' not in text
    assert 'actions/download-artifact@v4' in text
    assert 'ref: ${{ needs.prepare.outputs.trusted_sha }}' in text
    assert '-m scripts.sandbox_github evidence' in text
    assert '--evidence-format retry-v1' in text
    assert "needs.live.result == 'success'" in text


def test_trusted_helpers_use_package_invocation() -> None:
    """Package imports must work without an inherited local PYTHONPATH."""
    assert '-m scripts.sandbox_github prepare' in job('prepare')
    assert 'python3 scripts/sandbox_github.py' not in WORKFLOW.read_text(encoding='utf-8')


@pytest.mark.parametrize('name,dependencies', [
    ('pending', ('prepare',)),
    ('live', ('prepare', 'pending')),
])
@pytest.mark.parametrize('result,cancelled,expected', [
    ('success', False, True),
    ('success', True, False),
    ('failure', False, False),
    ('cancelled', False, False),
    ('skipped', False, False),
])
def test_live_chain_uses_explicit_status_guards_after_skipped_bootstrap(
        name: str, dependencies: tuple[str, ...], result: str,
        cancelled: bool, expected: bool) -> None:
    """A skipped optional ancestor must not add GitHub's implicit success guard."""
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    condition = re.search(r'^    if: \$\{\{ (.*?) \}\}$', job(name), re.MULTILINE)
    assert condition is not None, 'job requires an explicit status guard'
    clauses = condition.group(1).split(' && ')
    assert clauses[0] == '!cancelled()'
    assert clauses[1:] == [f"needs.{dependency}.result == 'success'"
                           for dependency in dependencies]
    assert ((not cancelled) and all(result == 'success' for _ in dependencies)) is expected


def test_incomplete_publication_fails_workflow_after_publishing_failure() -> None:
    """A red required status must not leave the workflow misleadingly green."""
    text = job('publish')
    assert "if (!complete) core.setFailed('Sandbox E2E not fully verified');" in text
    assert text.index('await github.rest.repos.createCommitStatus') < text.index('core.setFailed')


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
    assert 'RUN_CANCELLED: ${{ job.status == \'cancelled\' }}' in text
    assert "        if: ${{ !cancelled() }}" in text
    assert "process.env.RUN_CANCELLED === 'false'" in text


def test_status_functions_are_used_only_in_if_expressions() -> None:
    """GitHub permits status functions in conditions, not env or other fields."""
    text = WORKFLOW.read_text(encoding='utf-8')
    for expression in re.finditer(r'\$\{\{(.*?)\}\}', text, re.DOTALL):
        if re.search(r'\b(always|cancelled|failure|success)\s*\(', expression.group(1)):
            key = text[:expression.start()].rsplit('\n', 1)[-1].strip()
            assert key.startswith('if:'), f'status function outside if: {key}'


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
