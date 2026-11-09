"""Presentation accepts frozen application data without broker dependencies."""
from dataclasses import FrozenInstanceError
from decimal import Decimal
import logging
import subprocess
import sys

import pytest

from autorepeater.display_data import DisplayAccount, DisplayPosition
from autorepeater import reporting


@pytest.mark.parametrize('kind', ['currency', 'share', 'etf', 'bond', 'unknown'])
def test_display_position_formats_only_loaded_values(kind, caplog):
    """Pure renderers retain the old numbers, text and normal-level summary."""
    position = DisplayPosition('uid', kind, 'rUb', Decimal('1.000000001'),
                               Decimal('-2'), 'Company(TICK)', 'raw SDK diagnostic')
    assert reporting.display_position_value(
        position.current_price, position.quantity) == Decimal('-2.000000002')
    expected = ('rUb - -2.000000002' if kind == 'currency' else
                'Company(TICK) - -2.0 - rUb - -2.000000002'
                if kind in ('share', 'etf') else 'raw SDK diagnostic')
    assert reporting.display_position_to_string(position) == expected
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'):
        reporting.print_display_position(position)
    assert caplog.messages == ([expected] if kind in ('currency', 'share', 'etf') else [
        'raw SDK diagnostic',
        'Position: uid=uid type=' + kind + ' quantity=-2.0 currency=rUb price=1.000000001'])
    with pytest.raises(FrozenInstanceError):
        position.uid = 'changed'


def test_display_headers_and_footer(caplog):
    """Header and footer are independent of account reads."""
    account = DisplayAccount('id', 'name')
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'):
        reporting.print_display_account_header(account)
        reporting.print_display_account_footer(Decimal('0'))
    assert caplog.messages == ['name (id)', '------------', 'total: 0', '============']
    with pytest.raises(FrozenInstanceError):
        account.name = 'changed'


def test_display_formatters_work_with_sdk_and_io_blocked(guarded_import_script):
    """Loading frozen data and invoking every pure display renderer needs no SDK/I/O."""
    script = guarded_import_script(('t_tech', 'grpc', 'autorepeater.legacy_display'), '''
from decimal import Decimal
from autorepeater.display_data import DisplayAccount, DisplayPosition
from autorepeater import reporting
position = DisplayPosition('uid', 'share', 'RUB', Decimal('1.5'), Decimal('2'), 'Name(TICK)')
assert reporting.display_instrument_label('Name', 'TICK') == 'Name(TICK)'
assert reporting.display_position_value(position.current_price, position.quantity) == Decimal('3')
assert reporting.display_position_to_string(position) == 'Name(TICK) - 2.0 - RUB - 3.0'
reporting.print_display_account_header(DisplayAccount('id', 'name'))
reporting.print_display_position(position)
reporting.print_display_account_footer(Decimal('3'))
print('pure display ok')
''')
    result = subprocess.run([sys.executable, '-c', script], check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'pure display ok\n'
