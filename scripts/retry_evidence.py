"""Generic verification of original test results, retry selection and cleanup."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from scripts.retry_results import PhaseResult, checked_nodeid, result_states, retry_nodes
from scripts.retry_settings import RunSettings, unique_fields

MAX_BYTES = 2_000_000


def checked_sha(value: object) -> str:
    """Attempts belong to one immutable nonzero commit when a SHA is supplied."""
    if (not isinstance(value, str) or re.fullmatch('[0-9a-f]{40}', value) is None
            or value == '0' * 40):
        raise ValueError('invalid test SHA')
    return value


def nodeids(value: object) -> tuple[str, ...]:
    """Only unique explicit selectors can define a collection or retry subset."""
    if not isinstance(value, (list, tuple)):
        raise ValueError('test node IDs must be a sequence')
    nodes = tuple(checked_nodeid(node) for node in value)
    if len(set(nodes)) != len(nodes):
        raise ValueError('duplicate test node IDs')
    return nodes


@dataclass(frozen=True, slots=True)
class AttemptEvidence:  # pylint: disable=too-many-instance-attributes
    """Original process results and proof that owned fixture lifecycles ended."""
    selected: tuple[str, ...]
    phases: tuple[PhaseResult, ...]
    entered: tuple[str, ...]
    cleaned: tuple[str, ...]
    sha: str | None
    session_cleanup: bool
    collection_errors: int
    exit_status: int


def read_bytes(path: Path) -> bytes:
    """Bound artifact reads and reject symlink files and ancestors."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('symlink test evidence rejected')
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
    except OSError:
        raise ValueError('test evidence missing or unreadable') from None
    if len(raw) > MAX_BYTES:
        raise ValueError('test evidence exceeds size limit')
    return raw


def read_json(path: Path) -> object:
    """Invalid JSON and Unicode are failures, not missing facts with defaults."""
    try:
        return json.loads(read_bytes(path), object_pairs_hook=unique_fields)
    except (ValueError, UnicodeError):
        raise ValueError('invalid test evidence JSON') from None


def object_fields(value: object, names: set[str]) -> dict[str, object]:
    """Unknown or omitted fields cannot change the result contract implicitly."""
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError('invalid test evidence fields')
    return value


def integer(value: object) -> int:
    """Boolean values and numeric strings are not counters or exit codes."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('integer test evidence required')
    return value


def phase_result(value: object) -> PhaseResult:
    """Failure classification is one explicit boolean supplied by test code."""
    fields = object_fields(value, {'nodeid', 'when', 'outcome', 'retryable'})
    when, outcome, allowed = fields['when'], fields['outcome'], fields['retryable']
    if not isinstance(when, str) or not isinstance(outcome, str) or not isinstance(allowed, bool):
        raise ValueError('invalid test phase fields')
    return PhaseResult(checked_nodeid(fields['nodeid']), when, outcome, allowed)


def read_result(path: Path) -> AttemptEvidence:
    """No SDK errors, currencies, prices or clock thresholds are interpreted here."""
    fields = object_fields(read_json(path), {
        'version', 'selected', 'phases', 'entered', 'cleaned', 'session_cleanup',
        'collection_errors', 'exit_status', 'sha'})
    phases, cleanup, sha = fields['phases'], fields['session_cleanup'], fields['sha']
    if (integer(fields['version']) != 1 or not isinstance(phases, list)
            or not isinstance(cleanup, bool)):
        raise ValueError('invalid original test results')
    return AttemptEvidence(
        selected=nodeids(fields['selected']), phases=tuple(phase_result(phase) for phase in phases),
        entered=nodeids(fields['entered']), cleaned=nodeids(fields['cleaned']),
        sha=None if sha is None else checked_sha(sha), session_cleanup=cleanup,
        collection_errors=integer(fields['collection_errors']),
        exit_status=integer(fields['exit_status']))


def validate_result(report: AttemptEvidence, returncode: int) -> dict[str, str]:
    """Result flags never replace process consistency or successful cleanup."""
    states = result_states(report.phases, report.selected)
    if report.collection_errors != 0 or returncode not in (0, 1):
        raise ValueError('collection error or invalid test process exit')
    if report.exit_status != returncode or (returncode == 0) != all(
            state == 'passed' for state in states.values()):
        raise ValueError('test phases disagree with process exit')
    if not report.session_cleanup or set(report.entered) != set(report.cleaned):
        raise ValueError('incomplete fixture cleanup')
    if not set(report.entered) <= set(report.selected):
        raise ValueError('fixture outside selected collection')
    for node in report.selected:
        setup = next(phase for phase in report.phases if phase.nodeid == node)
        if setup.outcome == 'passed' and node not in report.entered:
            raise ValueError('successful setup has no fixture cleanup proof')
    return states


def junit_key(node: str) -> tuple[str, str]:
    """Match pytest JUnit identity, including classes and parameterized methods."""
    module, _, member = checked_nodeid(node).partition('::')
    label, bracket, parameters = member.partition('[')
    members = label.split('::')
    classname = '.'.join([module[:-3].replace('/', '.'), *members[:-1]])
    return classname, members[-1] + bracket + parameters


def xml_states(path: Path, selected: tuple[str, ...]) -> dict[str, str]:
    """Original JUnit must contain exactly the reported tests and truthful counters."""
    try:
        text = read_bytes(path).decode('utf-8')
        if '<!DOCTYPE' in text or '<!ENTITY' in text:
            raise ValueError('unsupported test XML')
        root = ET.fromstring(text)
    except (ET.ParseError, UnicodeError):
        raise ValueError('invalid test XML') from None
    suites = list(root.iter('testsuite'))
    if root.tag not in ('testsuite', 'testsuites') or not suites:
        raise ValueError('invalid test JUnit root')
    validate_xml_counts(root, suites)
    expected = {junit_key(node): node for node in selected}
    if len(expected) != len(selected):
        raise ValueError('ambiguous JUnit test identity')
    states = {}
    for case in root.iter('testcase'):
        if case.find('skipped') is not None:
            raise ValueError('skipped original test cannot pass or retry')
        node = expected.get((case.get('classname', ''), case.get('name', '')))
        if node is None or node in states:
            raise ValueError('unknown or duplicate JUnit test')
        states[node] = ('failed' if any(case.find(tag) is not None
                                        for tag in ('failure', 'error')) else 'passed')
    return states


def validate_xml_counts(root: ET.Element, suites: list[ET.Element]) -> None:
    """Suite counters corroborate the actual case elements, not a green summary."""
    for suite in [root, *suites] if root.tag == 'testsuites' else suites:
        cases = list(suite.iter('testcase'))
        for key, tag in {'tests': None, 'failures': 'failure',
                         'errors': 'error', 'skipped': 'skipped'}.items():
            number = len(cases) if tag is None else sum(
                case.find(tag) is not None for case in cases)
            value = suite.get(key)
            if (value is not None or suite.tag == 'testsuite') and value != str(number):
                raise ValueError('JUnit counters disagree with original cases')


def verify_attempt(directory: Path, returncode: int) -> AttemptEvidence:
    """Original JSON, JUnit and cleanup evidence must describe the same process."""
    report = read_result(directory / 'result.json')
    states = validate_result(report, returncode)
    if xml_states(directory / 'junit.xml', report.selected) != states:
        raise ValueError('original JUnit disagrees with test phases')
    try:
        markers = read_bytes(directory / 'cleanup.log').decode('utf-8').splitlines()
    except UnicodeError:
        raise ValueError('invalid cleanup markers') from None
    wanted = (['Sandbox scenario cleanup complete'] * len(report.cleaned)
              + ['Sandbox session cleanup complete'])
    if markers != wanted:
        raise ValueError('cleanup markers disagree with original finalizers')
    return report


def history_entries(root: Path, settings: RunSettings) -> list[dict[str, object]]:
    """The launch snapshot bounds attempts, without a separate master constant."""
    fields = object_fields(read_json(root / 'retry.json'), {'version', 'attempts'})
    entries = fields['attempts']
    if (integer(fields['version']) != 1 or not isinstance(entries, list)
            or not 1 <= len(entries) <= settings.max_attempts):
        raise ValueError('invalid retry history or attempt count')
    records = [object_fields(entry, {'number', 'returncode'}) for entry in entries]
    for number, record in enumerate(records, 1):
        if integer(record['number']) != number:
            raise ValueError('nonconsecutive retry attempts')
    try:
        actual = {path.name for path in (root / 'attempts').iterdir()}
    except OSError:
        raise ValueError('retry attempt directories missing') from None
    if actual != {f'{number:02}' for number in range(1, len(records) + 1)}:
        raise ValueError('retry directories disagree with history')
    return records


def inspect_history(root: Path, settings: RunSettings, *,
                    expected_sha: str | None = None) -> dict[str, str]:
    """Reconstruct full selection and every permitted retry from original results."""
    if expected_sha is not None:
        checked_sha(expected_sha)
    try:
        fields = object_fields(read_json(root / 'run-settings.json'), set(asdict(settings)))
        recorded = RunSettings.from_json(json.dumps(fields))
    except ValueError:
        raise ValueError('invalid run settings evidence') from None
    if recorded != settings:
        raise ValueError('run settings differ from authorized launch')
    states: dict[str, str] = {}
    next_nodes = None
    run_sha = expected_sha
    for number, entry in enumerate(history_entries(root, settings), 1):
        report = verify_attempt(root / 'attempts' / f'{number:02}', integer(entry['returncode']))
        if number == 1 and expected_sha is None:
            run_sha = report.sha
        if report.sha != run_sha:
            raise ValueError('attempt SHA differs from run SHA')
        if next_nodes is not None and (not next_nodes or report.selected != next_nodes):
            raise ValueError('illegal retry selection')
        states.update(result_states(report.phases, report.selected))
        next_nodes = retry_nodes(report.phases, report.selected)
    return states


def write_junit(states: dict[str, str], path: Path) -> None:
    """Write unique final cases without copying diagnostic text into the result."""
    suite = ET.Element('testsuite', name='tests-final', tests=str(len(states)),
                       failures=str(sum(state != 'passed' for state in states.values())),
                       errors='0', skipped='0')
    for node, state in states.items():
        classname, name = junit_key(node)
        case = ET.SubElement(suite, 'testcase', classname=classname, name=name)
        if state != 'passed':
            ET.SubElement(case, 'failure', message='test did not pass')
    ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


def verify_history(root: Path, settings: RunSettings, *, expected_sha: str | None = None) -> int:
    """A green final file is accepted only when all original results prove it."""
    states = inspect_history(root, settings, expected_sha=expected_sha)
    if not states or any(state != 'passed' for state in states.values()):
        raise ValueError('tests remain failed')
    if xml_states(root / 'junit.xml', tuple(states)) != states:
        raise ValueError('final JUnit disagrees with original results')
    return len(states)
