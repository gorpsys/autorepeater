"""Run parameters are independent of trading rules and immutable per launch."""
from dataclasses import FrozenInstanceError
import json

import pytest

from scripts.retry_settings import RunSettings


def test_defaults_preserve_current_local_budget_and_retry_policy():
    """A local launch without overrides keeps the existing finite defaults."""
    settings = RunSettings.from_json(None)
    assert settings.timeout_seconds == 1800
    assert settings.max_attempts == 4
    assert settings.retry_delay_seconds == 10
    assert settings.termination_grace_seconds == 15
    assert settings.termination_kill_seconds == 5
    assert settings.cleanup_timeout_seconds == 60
    assert settings.orphan_cleanup_timeout_seconds == 90


def test_all_parameters_can_be_overridden_without_changing_defaults():
    """Settings can change at dispatch, not only by committing new constants."""
    values = {
        'timeout_seconds': 4200, 'max_attempts': 7, 'retry_delay_seconds': 0,
        'termination_grace_seconds': 25, 'termination_kill_seconds': 8,
        'cleanup_timeout_seconds': 75, 'orphan_cleanup_timeout_seconds': 120,
        'workflow_overhead_seconds': 1800,
    }
    settings = RunSettings.from_json(json.dumps(values))
    assert json.loads(settings.to_json()) == values
    assert RunSettings.from_json(settings.to_json()) == settings
    assert RunSettings().timeout_seconds == 1800
    with pytest.raises(FrozenInstanceError):
        settings.max_attempts = 2


@pytest.mark.parametrize('field', [
    'timeout_seconds', 'max_attempts', 'retry_delay_seconds',
    'termination_grace_seconds', 'termination_kill_seconds',
    'cleanup_timeout_seconds', 'orphan_cleanup_timeout_seconds',
    'workflow_overhead_seconds',
])
@pytest.mark.parametrize('value', [True, False, None, '30', 1.5, -1])
def test_invalid_types_or_negative_values_are_not_coerced(field, value):
    """A boolean, numeric string or fraction is not an execution budget."""
    with pytest.raises(ValueError, match='settings'):
        RunSettings.from_json(json.dumps({field: value}))


@pytest.mark.parametrize('field', [
    'timeout_seconds', 'max_attempts', 'termination_grace_seconds',
    'termination_kill_seconds', 'cleanup_timeout_seconds',
    'orphan_cleanup_timeout_seconds', 'workflow_overhead_seconds',
])
def test_zero_is_rejected_except_for_retry_delay(field):
    """There is no implicit unlimited timeout or an empty attempt budget."""
    with pytest.raises(ValueError, match='settings'):
        RunSettings.from_json(json.dumps({field: 0}))


@pytest.mark.parametrize('value', [
    '', '{', 'null', 'true', '[]', '1', '{"unknown": 1}',
    '{"timeout_seconds": 30, "timeout_seconds": 60}',
])
def test_malformed_ambiguous_or_unknown_settings_fail_before_execution(value):
    """An explicit invalid override cannot silently fall back to defaults."""
    with pytest.raises(ValueError, match='settings'):
        RunSettings.from_json(value)


def test_partial_override_keeps_other_defaults_and_uses_canonical_json():
    """The same effective settings are passed to every child and the verifier."""
    settings = RunSettings.from_json('{"max_attempts":2}')
    assert settings.timeout_seconds == 1800
    assert settings.max_attempts == 2
    assert settings.retry_delay_seconds == 10
    assert settings.to_json() == json.dumps(
        json.loads(settings.to_json()), sort_keys=True, separators=(',', ':'))


@pytest.mark.parametrize('value', [True, 30, {}, []])
def test_non_text_settings_are_rejected_before_json_parsing(value):
    """The external input contract does not guess how to serialize caller objects."""
    with pytest.raises(ValueError, match='settings'):
        RunSettings.from_json(value)


def test_workflow_budgets_include_termination_cleanup_and_configured_overhead():
    """Workflow time limits are derived, not copied independently from run limits."""
    settings = RunSettings.from_json('{"timeout_seconds":3600}')
    assert settings.run_step_minutes == 62
    assert settings.job_minutes == 85
    changed = RunSettings.from_json('{"timeout_seconds":4200}')
    assert changed.run_step_minutes == 72
    assert changed.job_minutes == 95
