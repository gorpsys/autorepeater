"""Trusted GitHub target selection and exact sandbox-suite evidence validation."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen
from scripts import retry_evidence
from scripts.retry_settings import RunSettings
MAX_EVIDENCE_BYTES = 2_000_000
BOOTSTRAP_PR_NUMBER = 28


class GitHubFailure(Exception):
    """Fixed safe boundary error; never include transport details or credentials."""


@dataclass(frozen=True, slots=True)
class TestTarget:
    """The one immutable commit selected before the credential reaches test code."""
    sha: str
    pr_number: int


@dataclass(frozen=True, slots=True)
class SuiteEvidence:
    """A complete passing collection, not an arbitrary assertion by tested code."""
    passed: int


def record(value: object) -> dict[str, object]:
    """Narrow parsed JSON without coercing malformed GitHub response fields."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise GitHubFailure('expected GitHub response object')
    return value


def commit_sha(value: object) -> str:
    """Reject branch names, output delimiters and the deleted-ref zero sentinel."""
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{40}', value) is None:
        raise GitHubFailure('invalid commit SHA')
    if value == '0' * 40:
        raise GitHubFailure('invalid commit SHA')
    return value


def parse_pr_number(value: object) -> int:
    """Accept only an explicit positive decimal dispatch input, without coercion."""
    if not isinstance(value, str) or re.fullmatch('[1-9][0-9]*', value) is None:
        raise GitHubFailure('invalid PR number')
    return int(value)


def validate_pull_request(document: object, repository: str, number: int,
                          expected_sha: str | None = None) -> TestTarget:
    """Require an open same-repository master-target PR; bootstrap pins event SHA."""
    data = record(document)
    raw_number = data.get('number')
    if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number != number:
        raise GitHubFailure('PR number mismatch')
    if data.get('state') != 'open':
        raise GitHubFailure('PR is not open')
    base, head = record(data.get('base')), record(data.get('head'))
    if base.get('ref') != 'master' or record(base.get('repo')).get('full_name') != repository:
        raise GitHubFailure('PR must target this repository master')
    if record(head.get('repo')).get('full_name') != repository:
        raise GitHubFailure('fork PR cannot receive sandbox secret; E2E not run')
    sha = commit_sha(head.get('sha'))
    if expected_sha is not None and sha != commit_sha(expected_sha):
        raise GitHubFailure('bootstrap head changed; approve the new event')
    return TestTarget(sha, number)


def validate_permission(document: object) -> None:
    """Write, maintain or admin access is required for each initiating identity."""
    if not isinstance(document, dict) or document.get('permission') not in (
            'admin', 'maintain', 'write'):
        raise GitHubFailure('initiator requires write permission')


class GitHubClient:  # pylint: disable=too-few-public-methods
    """Read-only fixed-origin API access, finite timeout and bounded response size."""

    def __init__(self, repository: str, token: str) -> None:
        if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) is None or not token:
            raise GitHubFailure('GitHub repository and token required')
        self.repository = repository
        self.token = token

    def read(self, route: str) -> object:
        """Use generated routes only; suppress arbitrary HTTP/SDK error strings."""
        request = Request(f'https://api.github.com/repos/{self.repository}/{route}', headers={
            'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read(MAX_EVIDENCE_BYTES + 1)
            if len(raw) > MAX_EVIDENCE_BYTES:
                raise GitHubFailure('GitHub response exceeds size limit')
            return json.loads(raw)
        except (OSError, URLError, ValueError):
            raise GitHubFailure('GitHub API request failed') from None


def select_settings(event: object, event_name: str) -> RunSettings:
    """Freeze dispatch budgets before test code runs; push retains the one-hour limit."""
    value = (record(record(event).get('inputs')).get('run_settings', '{}')
             if event_name == 'workflow_dispatch' else '{}')
    if not isinstance(value, str):
        raise GitHubFailure('run settings must be JSON text')
    settings = RunSettings.from_json(value)
    parsed = json.loads(value)
    if 'timeout_seconds' not in parsed:
        parsed['timeout_seconds'] = 3600
        settings = RunSettings.from_json(json.dumps(parsed))
    if settings.job_minutes > 360:
        raise GitHubFailure('run budgets exceed GitHub hosted job limit')
    return settings


def select_target(event: object, environment: dict[str, str], client: GitHubClient) -> TestTarget:
    """Authorize original actor and rerunner before selecting an immutable target."""
    for actor in dict.fromkeys((environment.get('GITHUB_ACTOR', ''),
                               environment.get('GITHUB_TRIGGERING_ACTOR', ''))):
        if re.fullmatch('[A-Za-z0-9-]{1,39}', actor) is None:
            raise GitHubFailure('invalid initiating user')
        validate_permission(client.read(f'collaborators/{actor}/permission'))
    if environment.get('GITHUB_REF') != 'refs/heads/master' and (
            environment.get('GITHUB_EVENT_NAME') != 'pull_request'):
        raise GitHubFailure('dispatch and push require trusted master workflow')
    data = record(event)
    event_name = environment.get('GITHUB_EVENT_NAME')
    if event_name == 'push':
        if data.get('deleted') is not False or data.get('after') != environment.get('GITHUB_SHA'):
            raise GitHubFailure('invalid master push')
        return TestTarget(commit_sha(data['after']), 0)
    if event_name == 'workflow_dispatch':
        number = parse_pr_number(record(data.get('inputs')).get('pr_number'))
        return validate_pull_request(client.read(f'pulls/{number}'), client.repository, number)
    if event_name == 'pull_request':
        pull = record(data.get('pull_request'))
        raw_number = pull.get('number')
        if (isinstance(raw_number, bool) or not isinstance(raw_number, int) or
                raw_number != BOOTSTRAP_PR_NUMBER):
            raise GitHubFailure(
                'bootstrap only supports the workflow repair PR; manual E2E required')
        expected = commit_sha(record(pull.get('head')).get('sha'))
        return validate_pull_request(client.read(f'pulls/{BOOTSTRAP_PR_NUMBER}'),
                                     client.repository, BOOTSTRAP_PR_NUMBER, expected)
    raise GitHubFailure('unsupported GitHub event')


def write_outputs(values: dict[str, str]) -> None:
    """Only validated own facts enter GitHub's single-line output boundary."""
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        raise GitHubFailure('GITHUB_OUTPUT required')
    with Path(path).open('a', encoding='utf-8') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')


def evidence_result(junit: Path, *, sha: str, settings: RunSettings) -> SuiteEvidence:
    """Verify originals against the trusted commit and authorized execution budgets."""
    trusted_sha = commit_sha(sha)
    if junit.name != 'junit.xml':
        raise GitHubFailure('retry evidence requires the fixed merged JUnit path')
    try:
        return SuiteEvidence(retry_evidence.verify_history(
            junit.parent, settings, expected_sha=trusted_sha))
    except ValueError:
        raise GitHubFailure('retry history does not prove full sandbox acceptance') from None


def main(argv: list[str] | None = None) -> int:
    """Run preparation without SDK/secrets, or verify sanitized JUnit evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    evidence = commands.add_parser('evidence')
    evidence.add_argument('--junit', type=Path, required=True)
    evidence.add_argument('--sha', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'evidence':
            facts = evidence_result(
                args.junit, sha=args.sha,
                settings=RunSettings.from_json(os.environ['E2E_RUN_SETTINGS']))
            write_outputs({'complete': 'true', 'passed': str(facts.passed)})
        else:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            client = GitHubClient(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN', ''))
            target = select_target(event, dict(os.environ), client)
            trusted_sha = (target.sha if os.environ['GITHUB_EVENT_NAME'] == 'pull_request'
                           else commit_sha(os.environ.get('GITHUB_SHA')))
            settings = select_settings(event, os.environ['GITHUB_EVENT_NAME'])
            write_outputs({'head_sha': target.sha, 'pr_number': str(target.pr_number),
                           'trusted_sha': trusted_sha,
                           'run_settings': settings.to_json(),
                           'run_step_minutes': str(settings.run_step_minutes),
                           'job_minutes': str(settings.job_minutes),
                           'cleanup_minutes': str(settings.orphan_cleanup_minutes),
                           'cleanup_seconds': str(settings.orphan_cleanup_timeout_seconds)})
    except GitHubFailure as error:
        print(str(error) + '; no success status', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Sandbox GitHub preparation/evidence rejected; no success status', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
