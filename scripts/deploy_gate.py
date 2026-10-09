"""Authorize production publication only after all checks on the current master commit."""
import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request

from scripts.deploy_support import DeployError, commit_sha, record, request_json, text

REPOSITORY = 'gorpsys/autorepeater'
REQUIRED_CHECKS = ('Lint', 'Offline checks', 'Sandbox E2E')
POLL_SECONDS = 15


def select_sha(event: object, environment: dict[str, str]) -> str:
    """workflow_run uses trusted master code but may describe an unsafe PR or fork."""
    if environment.get('GITHUB_REPOSITORY') != REPOSITORY or (
            environment.get('GITHUB_REF') != 'refs/heads/master'):
        raise DeployError('deployment requires this repository master')
    name = environment.get('GITHUB_EVENT_NAME')
    if name == 'workflow_dispatch':
        return commit_sha(environment.get('GITHUB_SHA'))
    if name != 'workflow_run':
        raise DeployError('unsupported deployment event')
    run = record(record(event).get('workflow_run'))
    expected = {'name': 'Sandbox live E2E', 'event': 'push', 'head_branch': 'master',
                'status': 'completed', 'conclusion': 'success'}
    if any(run.get(key) != value for key, value in expected.items()) or (
            record(run.get('head_repository')).get('full_name') != REPOSITORY):
        raise DeployError('only a successful master push E2E can initiate deployment')
    return commit_sha(run.get('head_sha'))


class GitHub:
    """Read-only fixed-origin check facts; production OIDC is unavailable in this job."""

    def __init__(self, token: str):
        self.token = text(token, 'GitHub token')

    def read(self, route: str) -> object:
        """Routes are generated locally from validated full SHA values."""
        request = Request(f'https://api.github.com/repos/{REPOSITORY}/{route}',
                          headers={'Authorization': f'Bearer {self.token}',
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28'})
        return request_json(request)

    def master_sha(self) -> str:
        """Never silently deploy a superseded head when workflows finish out of order."""
        return commit_sha(record(record(self.read('branches/master')).get('commit')).get('sha'))

    def checks(self, sha: str) -> list[object]:
        """Pagination cannot hide a failed required check behind the first hundred."""
        result: list[object] = []
        for page in range(1, 21):
            route = f'commits/{commit_sha(sha)}/check-runs?filter=latest&per_page=100&page={page}'
            data = record(self.read(route))
            count, checks = data.get('total_count'), data.get('check_runs')
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or (
                    not isinstance(checks, list)):
                raise DeployError('invalid check-run pagination')
            result.extend(checks)
            if len(result) == count:
                return result
            if not checks or len(result) > count:
                raise DeployError('incomplete check-run pagination')
        raise DeployError('too many check-run pages')

    def statuses(self, sha: str) -> dict[str, object]:
        """Full statuses include creator; newest-first history must not revive old success."""
        latest: dict[str, object] = {}
        sha = commit_sha(sha)
        for page in range(1, 21):
            items = self.read(f'commits/{sha}/statuses?per_page=100&page={page}')
            if not isinstance(items, list) or len(items) > 100:
                raise DeployError('invalid commit-status pagination')
            for item in items:
                data = record(item)
                context = data.get('context')
                if not isinstance(context, str) or not context:
                    raise DeployError('invalid status context')
                latest.setdefault(context, data)
            if len(items) < 100:
                values = list(latest.values())
                return {'sha': sha, 'statuses': values, 'total_count': len(values)}
        raise DeployError('too many commit-status pages')


def pending_checks(checks: list[object], statuses: object, sha: str) -> tuple[str, ...]:
    """Known successful names alone are insufficient without producer and SHA identity."""
    pending = []
    for name in REQUIRED_CHECKS[:2]:
        selected = [record(item) for item in checks if record(item).get('name') == name]
        if not selected:
            pending.append(name)
            continue
        if len(selected) != 1:
            raise DeployError(f'ambiguous required check: {name}')
        check = selected[0]
        if check.get('head_sha') != sha or record(check.get('app')).get('slug') != 'github-actions':
            raise DeployError(f'untrusted required check: {name}')
        if check.get('status') in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
            pending.append(name)
        elif check.get('status') != 'completed' or check.get('conclusion') != 'success':
            raise DeployError(f'required check failed: {name}')
    if sandbox_pending(statuses, sha):
        pending.append('Sandbox E2E')
    return tuple(pending)


def sandbox_pending(statuses: object, sha: str) -> bool:
    """Sandbox is a commit status, not a Check Run; validate its own evidence boundary."""
    data = record(statuses)
    items = data.get('statuses')
    count = data.get('total_count', len(items) if isinstance(items, list) else -1)
    if (data.get('sha') != sha or not isinstance(items, list) or isinstance(count, bool)
            or not isinstance(count, int) or count != len(items)):
        raise DeployError('incomplete or mismatched commit statuses')
    selected_status = [record(item) for item in items
                       if record(item).get('context') == 'Sandbox E2E']
    if not selected_status:
        return True
    if len(selected_status) != 1 or (
            record(selected_status[0].get('creator')).get('login') != 'github-actions[bot]'):
        raise DeployError('ambiguous or untrusted Sandbox E2E status')
    if selected_status[0].get('state') == 'pending':
        return True
    if selected_status[0].get('state') != 'success':
        raise DeployError('required check failed: Sandbox E2E')
    return False


def wait_ready(client: GitHub, sha: str, wait_seconds: int, *,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    """A single bounded window; stale heads skip without obtaining cloud credentials."""
    deadline = clock() + wait_seconds
    while True:
        if client.master_sha() != sha:
            print(f'Skip superseded master commit {sha}', flush=True)
            return False
        pending = pending_checks(client.checks(sha), client.statuses(sha), sha)
        if not pending:
            print(f'All required checks successful for {sha}', flush=True)
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeployError('required checks timeout: ' + ', '.join(pending))
        print('Waiting for checks: ' + ', '.join(pending), flush=True)
        sleep(min(POLL_SECONDS, remaining))


def main(argv: list[str] | None = None) -> int:
    """Write immutable outputs only after read-only authorization has succeeded."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-once', action='store_true')
    args = parser.parse_args(argv)
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        sha = select_sha(event, dict(os.environ))
        ready = wait_ready(GitHub(os.environ.get('GH_TOKEN', '')), sha,
                           0 if args.check_once else 600)
        if args.check_once and not ready:
            raise DeployError('master changed before cloud publication')
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
            output.write(f'sha={sha}\nready={str(ready).lower()}\n')
    except DeployError as error:
        print(f'Deployment gate rejected: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Deployment gate configuration invalid', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
