"""Selection warnings use bounded identifier facts without dumping loaded metadata."""
from dataclasses import replace
import logging
from test.test_instrument_currency_selection import instrument

import pytest

from autorepeater import reporting
from autorepeater.logging_config import LOGGER_NAME


@pytest.mark.parametrize('count', [2, 20, 21, 100])
def test_selection_warning_limits_candidates_in_sdk_order(caplog, count):
    """The first twenty identifiers keep their order; the remainder is only a count."""
    candidates = [instrument(f'uid-{i}', 'rub') for i in range(count)]
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reporting.print_index_selection_warning('RU000A105104', candidates[0], candidates)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    expected = ', '.join(f'uid-{i}/TQCB' for i in range(min(count, 20)))
    if count > 20:
        expected += f'; omitted_count={count - 20}'
    assert record.message == (
        'Multiple API-tradable index instruments: RU000A105104; '
        'selected first uid=uid-0 class_code=TQCB; candidates: ' + expected)
    assert len(record.message) < 1500


def test_selection_warning_never_stringifies_invalid_identifiers_or_names(caplog):
    """Malformed fields and arbitrary reprs cannot reach the public warning."""
    secret = 'Bearer simulated-credential-DO-NOT-LOG'
    chosen = replace(instrument('first', 'rub'), uid={'credential': secret},
                     class_code='X' * 1000, name=secret)
    alternative = replace(instrument('second', 'rub'), uid=object(), class_code=secret)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reporting.print_index_selection_warning(secret, chosen, [chosen, alternative])
    assert len(caplog.records) == 1
    assert caplog.records[0].message == (
        'Multiple API-tradable index instruments: invalid; '
        'selected first uid=invalid class_code=invalid; '
        'candidates: invalid/invalid, invalid/invalid')
    assert secret not in caplog.text


def test_selection_warning_does_not_include_instrument_names(caplog):
    """Even normal metadata names are not selection identifiers."""
    candidates = [replace(instrument(str(i), 'cny'), name='private instrument name')
                  for i in range(2)]
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reporting.print_index_selection_warning('RU000A105104', candidates[0], candidates)
    assert len(caplog.records) == 1
    assert 'private instrument name' not in caplog.text
