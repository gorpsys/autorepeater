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
import xml.etree.ElementTree as ET

EXPECTED_SCENARIOS = (
    'test_initial_monthly2000_and_noop[INDEX-PAIR]',
    'test_initial_monthly2000_and_noop[COMPOSITE-MIX]',
    'test_initial_monthly2000_and_noop[ACCOUNT-SOURCE]',
    'test_staged_holdings_drift[INDEX-PAIR-.55-False]',
    'test_staged_holdings_drift[INDEX-PAIR-.80-True]',
    'test_staged_holdings_drift[COMPOSITE-MIX-.55-False]',
    'test_staged_holdings_drift[COMPOSITE-MIX-.80-True]',
    'test_staged_holdings_drift[ACCOUNT-SOURCE-.55-False]',
    'test_staged_holdings_drift[ACCOUNT-SOURCE-.80-True]',
    'test_child_own_drift_below_component_limit',
    'test_reserve_deficit_insufficient_cash_does_not_sell[INDEX-SOLO0]',
    'test_reserve_deficit_insufficient_cash_does_not_sell[COMPOSITE-MIX]',
    'test_reserve_deficit_insufficient_cash_does_not_sell[ACCOUNT-SOURCE]',
    'test_small_budget_cannot_submit_orders[INDEX-SOLO0]',
    'test_small_budget_cannot_submit_orders[COMPOSITE-MIX]',
    'test_small_budget_cannot_submit_orders[ACCOUNT-SOURCE]',
    'test_full_balanced_imoex_oblg_gold',
)
MAX_EVIDENCE_BYTES = 2_000_000


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
        if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number != 27:
            raise GitHubFailure('bootstrap only supports PR27; manual E2E required')
        expected = commit_sha(record(pull.get('head')).get('sha'))
        return validate_pull_request(client.read('pulls/27'), client.repository, 27, expected)
    raise GitHubFailure('unsupported GitHub event')


def read_junit(path: Path) -> ET.Element:
    """Read bounded UTF-8 XML without symlinks or entity declarations."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise GitHubFailure('symlink evidence is not accepted')
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_EVIDENCE_BYTES + 1)
        text = raw.decode('utf-8')
        if len(raw) > MAX_EVIDENCE_BYTES or '<!DOCTYPE' in text or '<!ENTITY' in text:
            raise GitHubFailure('unsupported JUnit evidence')
        return ET.fromstring(text)
    except (OSError, ET.ParseError, UnicodeError):
        raise GitHubFailure('cannot read JUnit evidence') from None


def validate_junit_counts(root: ET.Element) -> None:
    """Reject suite-level failures and counters inconsistent with their cases."""
    if any(root.get(key, '0') != '0' for key in ('failures', 'errors', 'skipped')):
        raise GitHubFailure('sandbox suite failed or skipped')
    for suite in root.iter('testsuite'):
        for key in ('failures', 'errors', 'skipped'):
            if suite.get(key, '0') != '0':
                raise GitHubFailure('sandbox suite failed or skipped')
        if suite.get('tests') != str(len(list(suite.iter('testcase')))):
            raise GitHubFailure('JUnit counts mismatch')


def verify_junit(path: Path) -> SuiteEvidence:
    """Prove all 17 cases passed including setup/teardown, with no hidden selection."""
    root = read_junit(path)
    if root.tag not in ('testsuite', 'testsuites'):
        raise GitHubFailure('invalid JUnit root')
    validate_junit_counts(root)
    cases = list(root.iter('testcase'))
    names = [case.get('name') for case in cases]
    if len(cases) != len(EXPECTED_SCENARIOS) or set(names) != set(EXPECTED_SCENARIOS):
        raise GitHubFailure('full 17 sandbox scenarios required')
    for case in cases:
        if case.get('classname') != 'e2e.test_sandbox' or any(
                case.find(tag) is not None for tag in ('failure', 'error', 'skipped')):
            raise GitHubFailure('sandbox scenario not passed')
    return SuiteEvidence(len(cases))


def verify_evidence(junit: Path, run_log: Path) -> SuiteEvidence:
    """Require the full suite plus existing fixture finalizer completion markers."""
    facts = verify_junit(junit)
    if run_log.is_symlink() or any(parent.is_symlink() for parent in run_log.parents):
        raise GitHubFailure('symlink cleanup evidence is not accepted')
    try:
        with run_log.open('rb') as source:
            raw = source.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise GitHubFailure('cleanup evidence exceeds size limit')
        lines = raw.decode('utf-8').splitlines()
    except (OSError, UnicodeError):
        raise GitHubFailure('cannot read cleanup evidence') from None
    if lines.count('Sandbox scenario cleanup complete') != facts.passed or (
            lines.count('Sandbox session cleanup complete') != 1):
        raise GitHubFailure('exact fixture cleanup evidence required')
    return facts


def write_outputs(values: dict[str, str]) -> None:
    """Only validated own facts enter GitHub's single-line output boundary."""
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        raise GitHubFailure('GITHUB_OUTPUT required')
    with Path(path).open('a', encoding='utf-8') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')


def main(argv: list[str] | None = None) -> int:
    """Run preparation without SDK/secrets, or verify sanitized JUnit evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    evidence = commands.add_parser('evidence')
    evidence.add_argument('--junit', type=Path, required=True)
    evidence.add_argument('--run-log', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'evidence':
            facts = verify_evidence(args.junit, args.run_log)
            write_outputs({'complete': 'true', 'passed': str(facts.passed)})
        else:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            client = GitHubClient(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN', ''))
            target = select_target(event, dict(os.environ), client)
            trusted_sha = (target.sha if os.environ['GITHUB_EVENT_NAME'] == 'pull_request'
                           else commit_sha(os.environ.get('GITHUB_SHA')))
            write_outputs({'head_sha': target.sha, 'pr_number': str(target.pr_number),
                           'trusted_sha': trusted_sha})
    except GitHubFailure as error:
        print(str(error) + '; no success status', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Sandbox GitHub preparation/evidence rejected; no success status', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
