"""Test-owned retry decisions and generic validation of pytest phase results."""
from dataclasses import dataclass


def checked_nodeid(value: object) -> str:
    """A collected selector is data, never an option or a multiline argument."""
    if (not isinstance(value, str) or not value or value.startswith('-')
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('invalid test node ID')
    module, separator, member = value.partition('::')
    if (not separator or not member or not module.endswith('.py') or '\\' in module
            or any(part in ('', '.', '..') for part in module.split('/'))):
        raise ValueError('invalid test node ID')
    return value


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """A failed phase explicitly grants or denies retry; causes stay in test code."""
    nodeid: str
    when: str
    outcome: str
    retryable: bool

    def __post_init__(self) -> None:
        checked_nodeid(self.nodeid)
        if self.when not in ('setup', 'call', 'teardown') or self.outcome not in (
                'passed', 'failed', 'skipped'):
            raise ValueError('invalid test phase result')
        if not isinstance(self.retryable, bool):
            raise ValueError('explicit boolean retry decision required')
        if self.retryable and (self.outcome != 'failed' or self.when == 'teardown'):
            raise ValueError('only failed setup or call can request retry')


def case_phases(results: tuple[PhaseResult, ...], node: str) -> tuple[PhaseResult, ...]:
    """A completed test has either failed setup or a complete setup/call lifecycle."""
    phases = tuple(result for result in results if result.nodeid == node)
    names = tuple(phase.when for phase in phases)
    if names not in (('setup', 'call', 'teardown'), ('setup', 'teardown')):
        raise ValueError('incomplete or duplicate test phases')
    if phases[-1].outcome != 'passed' or any(phase.outcome == 'skipped' for phase in phases):
        raise ValueError('cleanup failure or skipped test cannot pass')
    if names == ('setup', 'teardown') and phases[0].outcome != 'failed':
        raise ValueError('missing test call phase')
    if names == ('setup', 'call', 'teardown') and phases[0].outcome != 'passed':
        raise ValueError('test call after failed setup')
    return phases


def result_states(results: tuple[PhaseResult, ...], selected: tuple[str, ...]) -> dict[str, str]:
    """Every selected test must have one complete original lifecycle."""
    if not selected or len(set(selected)) != len(selected):
        raise ValueError('nonempty unique test selection required')
    if any(result.nodeid not in selected for result in results):
        raise ValueError('test result outside selected collection')
    return {node: 'failed' if any(phase.outcome == 'failed' for phase in case_phases(
        results, node)) else 'passed' for node in selected}


def retry_nodes(results: tuple[PhaseResult, ...], selected: tuple[str, ...]) -> tuple[str, ...]:
    """Retry only failed tests whose every failed phase explicitly permits it."""
    states = result_states(results, selected)
    return tuple(node for node in selected if states[node] == 'failed' and all(
        phase.retryable for phase in case_phases(results, node) if phase.outcome == 'failed'))
