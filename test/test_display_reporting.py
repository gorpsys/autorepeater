"""Own portfolio snapshots render with loaded metadata and no broker I/O."""
from dataclasses import FrozenInstanceError
from decimal import Decimal
import logging
import subprocess
import sys

import pytest

from autorepeater import reporting
from autorepeater.cash_facts import MoneyAmount
from autorepeater.display_data import DisplayAccount
from autorepeater.strategy_data import (
    InstrumentMatch, InstrumentType, PortfolioEntry, PortfolioSnapshot,
)


@pytest.mark.parametrize('kind', list(InstrumentType))
def test_loaded_positions_keep_money_and_quote_units_distinct(kind, caplog):
    """Raw SDK diagnostics never replace readable quantity and broker quote lines."""
    entry = PortfolioEntry('uid', kind, 'rUb', Decimal('1.000000001'), Decimal('-2'), 'raw')
    metadata = {'uid': InstrumentMatch('uid', 'TICK', 'Company', kind, '')}
    line = reporting.display_position_to_string(entry, metadata)
    if kind == InstrumentType.CURRENCY:
        assert line == 'rUb - -2.000000002'
    elif kind in (InstrumentType.SHARE, InstrumentType.ETF):
        assert line == 'Company(TICK) - -2.0 - rUb - -2.000000002'
    else:
        assert 'quantity=-2.0' in line and 'broker quote=1.000000001' in line
        assert '-2.000000002' not in line
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'):
        reporting.print_display_portfolio(PortfolioSnapshot((entry,)), metadata,
                                          MoneyAmount('usd', Decimal(7)))
    assert line in caplog.messages
    assert 'broker portfolio total: 7.0 usd' in caplog.messages
    with pytest.raises(FrozenInstanceError):
        entry.uid = 'changed'


def test_display_formatters_work_with_sdk_and_io_blocked(guarded_import_script):
    """All pure renderers work when the new SDK module and external reads are blocked."""
    script = guarded_import_script(('t_tech', 'grpc', 'autorepeater.tinvest_account_overview'), '''
from decimal import Decimal
from autorepeater.display_data import DisplayAccount
from autorepeater.cash_facts import MoneyAmount
from autorepeater.strategy_data import PortfolioEntry, PortfolioSnapshot, InstrumentMatch, InstrumentType
from autorepeater import reporting
entry = PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('1.5'), Decimal('2'), '')
metadata = {'uid': InstrumentMatch('uid', 'TICK', 'Name', InstrumentType.SHARE, '')}
assert reporting.display_position_to_string(entry, metadata) == 'Name(TICK) - 2.0 - RUB - 3.0'
reporting.print_display_account_header(DisplayAccount('id', 'name'))
reporting.print_display_portfolio(PortfolioSnapshot((entry,)), metadata, MoneyAmount('rub', Decimal('3')))
reporting.print_overview_totals([MoneyAmount('rub', Decimal('3'))], 0)
reporting.print_overview_totals([], 2)
print('pure display ok')
''')
    result = subprocess.run([sys.executable, '-c', script], check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'pure display ok\n'


def test_display_account_is_frozen():
    """Account identity cannot change after detaching from the broker response."""
    account = DisplayAccount('id', 'name')
    with pytest.raises(FrozenInstanceError):
        account.name = 'changed'
