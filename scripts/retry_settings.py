"""Immutable execution budgets shared by the launcher and its CI verifier."""
from dataclasses import asdict, dataclass, fields
import json


def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Duplicate JSON keys must not change meaning between consumers."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate run settings field')
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RunSettings:  # pylint: disable=too-many-instance-attributes
    """Finite run/retry/cleanup budgets, with no knowledge of test failure causes."""
    timeout_seconds: int = 1800
    max_attempts: int = 4
    retry_delay_seconds: int = 10
    termination_grace_seconds: int = 15
    termination_kill_seconds: int = 5
    cleanup_timeout_seconds: int = 60
    orphan_cleanup_timeout_seconds: int = 90
    workflow_overhead_seconds: int = 1290

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            minimum = 0 if name == 'retry_delay_seconds' else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError('run settings require integer execution budgets')

    @classmethod
    def from_json(cls, value: str | None) -> 'RunSettings':
        """Missing input selects defaults; explicit malformed input never does."""
        if value is None:
            return cls()
        if not isinstance(value, str):
            raise ValueError('run settings must be JSON text')
        try:
            parsed = json.loads(value, object_pairs_hook=unique_fields)
        except ValueError:
            raise ValueError('invalid run settings JSON') from None
        if not isinstance(parsed, dict) or not set(parsed) <= {item.name for item in fields(cls)}:
            raise ValueError('invalid run settings fields')
        return cls(**parsed)

    def to_json(self) -> str:
        """Persist and transmit the effective snapshot in one canonical form."""
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))

    @property
    def run_step_minutes(self) -> int:
        """Allow safe termination and current-run cleanup after the shared deadline."""
        seconds = (self.timeout_seconds + 2 * self.termination_grace_seconds
                   + self.termination_kill_seconds + self.cleanup_timeout_seconds)
        return (seconds + 59) // 60

    @property
    def job_minutes(self) -> int:
        """Reserve explicit workflow setup, final cleanup and artifact time."""
        seconds = (self.timeout_seconds + 2 * self.termination_grace_seconds
                   + self.termination_kill_seconds + self.cleanup_timeout_seconds
                   + max(self.workflow_overhead_seconds, 960)
                   + self.orphan_cleanup_timeout_seconds)
        return (seconds + 59) // 60

    @property
    def orphan_cleanup_minutes(self) -> int:
        """Reserve a minute for starting and reporting the final cleanup process."""
        return (self.orphan_cleanup_timeout_seconds + 59) // 60 + 1
