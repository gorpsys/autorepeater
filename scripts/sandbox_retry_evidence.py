"""Pure, strict retry evidence and independent reconstruction of final results."""
from dataclasses import dataclass
import json
from pathlib import Path
import xml.etree.ElementTree as ET

MAX_ATTEMPTS = 4
MAX_BYTES = 2_000_000
TRANSPORT_ROOTS = frozenset(('RequestError', 'SandboxFailure', 'DataAccessError',
                             'ExecutionDataError', 'OrderExecutionError'))
TRANSIENT_STATUSES = frozenset(('DEADLINE_EXCEEDED', 'UNAVAILABLE'))
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
EXPECTED_NODEIDS = tuple('e2e/test_sandbox.py::' + name for name in EXPECTED_SCENARIOS)


class RetryFailure(Exception):
    """Fixed safe reason; incomplete evidence is not repaired or assumed passing."""


@dataclass(frozen=True, slots=True)
class PhaseFact:
    """A sanitized original pytest phase, with allowlisted transport provenance."""
    nodeid: str
    when: str
    outcome: str
    transport_chain: tuple[str, ...]
    grpc_status: str | None


@dataclass(frozen=True, slots=True)
class AttemptReport:  # pylint: disable=too-many-instance-attributes
    """Original collection, phase results and actual fixture finalizer evidence."""
    selected: tuple[str, ...]
    phases: tuple[PhaseFact, ...]
    entered: tuple[str, ...]
    cleaned: tuple[str, ...]
    session_cleanup: bool
    collection_errors: int
    exit_status: int


def read_bytes(path: Path) -> bytes:
    """Bound artifact reads and reject file/ancestor symlinks before opening."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise RetryFailure('symlink retry evidence rejected')
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
    except OSError:
        raise RetryFailure('retry evidence missing or unreadable') from None
    if len(raw) > MAX_BYTES:
        raise RetryFailure('retry evidence exceeds size limit')
    return raw


def object_fields(value: object, fields: set[str]) -> dict[str, object]:
    """Reject coercion, unknown fields and absent evidence at the JSON boundary."""
    if not isinstance(value, dict) or set(value) != fields:
        raise RetryFailure('invalid retry evidence fields')
    return value


def read_json(path: Path) -> object:
    """Never use string/repr fallback for diagnostics or an invalid JSON artifact."""
    try:
        return json.loads(read_bytes(path))
    except (ValueError, UnicodeError):
        raise RetryFailure('invalid retry JSON') from None


def nodeids(value: object) -> tuple[str, ...]:
    """Only the suite's exact node IDs can select another live process."""
    if not isinstance(value, list) or any(item not in EXPECTED_NODEIDS for item in value):
        raise RetryFailure('invalid retry node IDs')
    if len(set(value)) != len(value):
        raise RetryFailure('duplicate retry node IDs')
    return tuple(value)


def phase_fact(value: object) -> PhaseFact:
    """Read own typed phase fields; arbitrary error text has no classification role."""
    fields = object_fields(value, {'nodeid', 'when', 'outcome', 'transport_chain', 'grpc_status'})
    node = nodeids([fields['nodeid']])[0]
    when, outcome = fields['when'], fields['outcome']
    if when not in ('setup', 'call', 'teardown') or outcome not in ('passed', 'failed', 'skipped'):
        raise RetryFailure('invalid retry phase outcome')
    chain = fields['transport_chain']
    if not isinstance(chain, list) or len(chain) > 8 or any(
            not isinstance(item, str) or item not in TRANSPORT_ROOTS | {'other'} for item in chain):
        raise RetryFailure('invalid transport provenance')
    status = fields['grpc_status']
    if status is not None and (not isinstance(status, str) or status not in TRANSIENT_STATUSES):
        raise RetryFailure('invalid retry transport status')
    if outcome == 'passed' and (chain or status is not None):
        raise RetryFailure('passed phase contains error facts')
    return PhaseFact(node, str(when), str(outcome), tuple(chain), status)


def integer(value: object) -> int:
    """Bool/float/string values cannot masquerade as process or collection facts."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetryFailure('retry integer required')
    return value


def read_report(path: Path) -> AttemptReport:
    """Reconstruct the original immutable report without permissive defaults."""
    fields = object_fields(read_json(path), {
        'selected', 'phases', 'entered', 'cleaned', 'session_cleanup',
        'collection_errors', 'exit_status',
    })
    raw_phases = fields['phases']
    cleanup = fields['session_cleanup']
    if not isinstance(raw_phases, list) or not isinstance(cleanup, bool):
        raise RetryFailure('invalid retry phase or cleanup facts')
    return AttemptReport(
        nodeids(fields['selected']), tuple(map(phase_fact, raw_phases)),
        nodeids(fields['entered']), nodeids(fields['cleaned']), cleanup,
        integer(fields['collection_errors']), integer(fields['exit_status']))


def case_phases(report: AttemptReport, node: str) -> tuple[PhaseFact, ...]:
    """Both normal and failed-setup lifecycles must contain a passed teardown."""
    phases = tuple(phase for phase in report.phases if phase.nodeid == node)
    names = tuple(phase.when for phase in phases)
    if names not in (('setup', 'call', 'teardown'), ('setup', 'teardown')):
        raise RetryFailure('incomplete or duplicate retry phases')
    if phases[-1].outcome != 'passed' or any(phase.outcome == 'skipped' for phase in phases):
        raise RetryFailure('teardown failure or skipped scenario cannot pass')
    if names == ('setup', 'teardown') and phases[0].outcome != 'failed':
        raise RetryFailure('missing call phase')
    if names == ('setup', 'call', 'teardown') and phases[0].outcome != 'passed':
        raise RetryFailure('call after failed setup')
    return phases


def failed_nodes(report: AttemptReport) -> tuple[str, ...]:
    """Derive case failures from any original phase, not from a producer verdict."""
    return tuple(node for node in report.selected if any(
        phase.nodeid == node and phase.outcome != 'passed' for phase in report.phases))


def transient(phase: PhaseFact) -> bool:
    """Assertions with a transport cause cannot gain retry permission."""
    return (phase.when != 'teardown' and phase.outcome == 'failed' and
            phase.grpc_status in TRANSIENT_STATUSES and bool(phase.transport_chain) and
            phase.transport_chain[-1] == 'RequestError' and
            all(name in TRANSPORT_ROOTS - {'RequestError'}
                for name in phase.transport_chain[:-1]))


def retry_nodes(report: AttemptReport) -> tuple[str, ...]:
    """A permanent failure stops this run; never retry it or hide it behind successes."""
    failed = failed_nodes(report)
    for node in failed:
        if any(not transient(phase) for phase in case_phases(report, node)
               if phase.outcome == 'failed'):
            return ()
    return failed


def validate_report(report: AttemptReport, returncode: int) -> None:
    """Require complete phases and confirmed cleanup, including failed session setup."""
    if not report.selected or report.collection_errors != 0:
        raise RetryFailure('empty collection or collection error')
    if report.exit_status != returncode or returncode not in (0, 1):
        raise RetryFailure('process canceled or exit status inconsistent')
    if not report.session_cleanup or set(report.entered) != set(report.cleaned):
        raise RetryFailure('incomplete fixture cleanup')
    if not set(report.entered) <= set(report.selected) or any(
            phase.nodeid not in report.selected for phase in report.phases):
        raise RetryFailure('phase or fixture outside selected cases')
    for node in report.selected:
        phases = case_phases(report, node)
        if phases[0].outcome == 'passed' and node not in report.entered:
            raise RetryFailure('successful setup has no fixture cleanup proof')
    if (returncode == 0) != (not failed_nodes(report)):
        raise RetryFailure('phase outcomes disagree with process exit')


def xml_states(path: Path) -> dict[str, str]:
    """Read exact unique case states from sanitized original or final JUnit."""
    try:
        text = read_bytes(path).decode('utf-8')
        if '<!DOCTYPE' in text or '<!ENTITY' in text:
            raise RetryFailure('unsupported retry XML')
        root = ET.fromstring(text)
    except (ET.ParseError, UnicodeError):
        raise RetryFailure('invalid retry XML') from None
    if root.tag not in ('testsuite', 'testsuites'):
        raise RetryFailure('invalid retry JUnit root')
    validate_xml_counts(root)
    states: dict[str, str] = {}
    for case in root.iter('testcase'):
        node = 'e2e/test_sandbox.py::' + case.get('name', '')
        if (node not in EXPECTED_NODEIDS or node in states or
                case.get('classname') != 'e2e.test_sandbox'):
            raise RetryFailure('invalid or duplicate retry JUnit case')
        states[node] = 'failed' if any(
            case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) else 'passed'
    return states


def validate_xml_counts(root: ET.Element) -> None:
    """Collection/teardown errors cannot be hidden in inconsistent suite counters."""
    suites = list(root.iter('testsuite'))
    if not suites:
        raise RetryFailure('JUnit suite missing')
    for suite in [root, *suites] if root.tag == 'testsuites' else suites:
        cases = list(suite.iter('testcase'))
        expected = {'tests': len(cases),
                    'failures': sum(case.find('failure') is not None for case in cases),
                    'errors': sum(case.find('error') is not None for case in cases),
                    'skipped': sum(case.find('skipped') is not None for case in cases)}
        for key, number in expected.items():
            value = suite.get(key)
            if (value is not None or suite.tag == 'testsuite') and value != str(number):
                raise RetryFailure('JUnit counters disagree with original cases')


def verify_attempt(directory: Path, returncode: int) -> AttemptReport:
    """Original JSON, JUnit and cleanup markers must corroborate one execution."""
    report = read_report(directory / 'report.json')
    validate_report(report, returncode)
    expected = {node: 'failed' if node in failed_nodes(report) else 'passed'
                for node in report.selected}
    if xml_states(directory / 'junit.xml') != expected:
        raise RetryFailure('original JUnit disagrees with original phases')
    try:
        markers = read_bytes(directory / 'cleanup.log').decode('utf-8').splitlines()
    except UnicodeError:
        raise RetryFailure('invalid cleanup markers') from None
    wanted = (['Sandbox scenario cleanup complete'] * len(report.cleaned) +
              ['Sandbox session cleanup complete'])
    if markers != wanted:
        raise RetryFailure('cleanup markers disagree with finalizers')
    return report


def history_entries(root: Path) -> list[dict[str, object]]:
    """Only consecutive original attempts in the bounded versioned format are valid."""
    fields = object_fields(read_json(root / 'retry.json'), {'version', 'attempts'})
    entries = fields['attempts']
    if (integer(fields['version']) != 1 or not isinstance(entries, list) or
            not 1 <= len(entries) <= MAX_ATTEMPTS):
        raise RetryFailure('invalid retry history version or attempt count')
    records = [object_fields(entry, {'number', 'returncode'}) for entry in entries]
    for number, record in enumerate(records, 1):
        if integer(record['number']) != number:
            raise RetryFailure('nonconsecutive retry attempts')
    directories = tuple(sorted(path.name for path in (root / 'attempts').iterdir()))
    if directories != tuple(f'{number:02}' for number in range(1, len(entries) + 1)):
        raise RetryFailure('retry directories disagree with manifest')
    return records


def inspect_history(root: Path, *, require_full: bool = False) -> dict[str, str]:
    """Derive legal selection, rerun count and final outcomes from every original."""
    states: dict[str, str] = {}
    next_nodes: tuple[str, ...] | None = None
    for number, entry in enumerate(history_entries(root), 1):
        report = verify_attempt(root / 'attempts' / f'{number:02}', integer(entry['returncode']))
        if number == 1 and require_full and set(report.selected) != set(EXPECTED_NODEIDS):
            raise RetryFailure('full initial 17-case collection required')
        if next_nodes is not None and (not next_nodes or report.selected != next_nodes):
            raise RetryFailure('illegal retry selection or retry after permanent failure')
        states.update({node: 'failed' if node in failed_nodes(report) else 'passed'
                       for node in report.selected})
        next_nodes = retry_nodes(report)
    return states


def write_final_junit(states: dict[str, str], path: Path) -> None:
    """Write unique final cases without copying arbitrary XML text or transport data."""
    suite = ET.Element('testsuite', name='sandbox-final', tests=str(len(states)),
                       failures=str(sum(state != 'passed' for state in states.values())),
                       errors='0', skipped='0')
    for node, state in states.items():
        case = ET.SubElement(suite, 'testcase', classname='e2e.test_sandbox',
                             name=node.split('::')[1])
        if state != 'passed':
            ET.SubElement(case, 'failure', message='sandbox scenario did not pass')
    ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


def verify_history(root: Path, *, require_full: bool = True) -> int:
    """Merged green output is accepted only when its originals independently prove it."""
    states = inspect_history(root, require_full=require_full)
    if not states or any(state != 'passed' for state in states.values()):
        raise RetryFailure('sandbox cases remain failed')
    if xml_states(root / 'junit.xml') != states:
        raise RetryFailure('merged JUnit disagrees with originals')
    return len(states)
