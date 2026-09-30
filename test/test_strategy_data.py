"""Tests for the SDK-independent strategy read contract."""
import subprocess
import sys
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from autorepeater.strategy_data import DataAccessError
from autorepeater.strategy_data import InstrumentInfo
from autorepeater.strategy_data import InstrumentMatch
from autorepeater.strategy_data import InstrumentType
from autorepeater.strategy_data import MoneyBlocking
from autorepeater.strategy_data import PortfolioEntry
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_data import PositionEvent
from autorepeater.strategy_data import PriceQuote
from autorepeater.strategy_data import SecurityBlocking
from autorepeater.strategy_data import StrategyData


def test_portfolio_snapshot_preserves_order_and_exact_values():
    """Portfolio DTOs retain source order and do not normalize Decimal values."""
    first = PortfolioEntry(
        uid='share-uid',
        instrument_type=InstrumentType.SHARE,
        currency='RUB',
        current_price=Decimal('123.450000000'),
        quantity=Decimal('-2.000000001'),
        diagnostic_text='share diagnostic',
    )
    second = PortfolioEntry(
        uid='other-uid',
        instrument_type=InstrumentType.OTHER,
        currency='USD',
        current_price=Decimal('0.000000001'),
        quantity=Decimal('3.1400'),
        diagnostic_text='raw diagnostic text',
    )

    snapshot = PortfolioSnapshot(positions=(first, second))

    assert snapshot.positions == (first, second)
    assert snapshot.positions[0].quantity.as_tuple() == Decimal('-2.000000001').as_tuple()
    assert snapshot.positions[1].current_price.as_tuple() == Decimal('0.000000001').as_tuple()
    assert snapshot.positions[1].diagnostic_text == 'raw diagnostic text'


def test_instrument_models_separate_search_and_full_metadata():
    """Search results do not pretend to contain fields loaded by UID."""
    match = InstrumentMatch(
        uid='uid',
        ticker='TEST',
        name='Test share',
        instrument_type=InstrumentType.SHARE,
        class_code='TQBR',
    )
    info = InstrumentInfo(
        uid='uid',
        ticker='TEST',
        name='Test share',
        instrument_type=InstrumentType.SHARE,
        class_code='TQBR',
        lot=10,
        currency='RUB',
    )

    assert {field.name for field in fields(InstrumentMatch)} == {
        'uid', 'ticker', 'name', 'instrument_type', 'class_code'}
    assert not hasattr(match, 'currency')
    assert not hasattr(match, 'lot')
    assert info.currency == 'RUB'
    assert info.lot == 10
    assert InstrumentType.ETF.value == 'etf'
    assert InstrumentType.CURRENCY.value == 'currency'


def test_price_quote_preserves_datetime_and_decimal_without_conversion():
    """Quote time and value pass through the own model unchanged."""
    quotation_time = datetime(
        2026, 9, 30, 12, 34, 56, 789012, tzinfo=timezone.utc)
    price = Decimal('17.0000000010')

    quote = PriceQuote(uid='uid', price=price, time=quotation_time)

    assert quote.price is price
    assert quote.price.as_tuple() == Decimal('17.0000000010').as_tuple()
    assert quote.time is quotation_time


def test_position_event_preserves_service_event_and_blocking_order():
    """Events retain all ordered blocking data and explicit service events."""
    securities = (SecurityBlocking(blocked=0), SecurityBlocking(blocked=7))
    money = (
        MoneyBlocking(blocked_value=Decimal('0E-9')),
        MoneyBlocking(blocked_value=Decimal('-0.000000001')),
    )
    position = PositionEvent(
        has_position=True,
        account_id='account',
        securities=securities,
        money=money,
        diagnostic_text='position event',
    )
    service = PositionEvent(
        has_position=False,
        account_id='',
        securities=(),
        money=(),
        diagnostic_text='subscription ping',
    )

    assert position.securities == securities
    assert position.money == money
    assert position.money[0].blocked_value.as_tuple() == Decimal('0E-9').as_tuple()
    assert position.money[1].blocked_value == Decimal('-0.000000001')
    assert service.has_position is False
    assert service.diagnostic_text == 'subscription ping'


@pytest.mark.parametrize(
    'model',
    [
        PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('1'),
                       Decimal('1'), 'diagnostic'),
        PortfolioSnapshot(()),
        InstrumentMatch('uid', 'TEST', 'Test', InstrumentType.SHARE, 'TQBR'),
        InstrumentInfo('uid', 'TEST', 'Test', InstrumentType.SHARE, 'TQBR', 1, 'RUB'),
        PriceQuote('uid', Decimal('1'), None),
        SecurityBlocking(0),
        MoneyBlocking(Decimal('0')),
        PositionEvent(False, '', (), (), 'service'),
    ],
)
def test_models_do_not_accept_arbitrary_raw_objects(model):
    """Slotted own DTOs cannot be extended with SDK/raw payloads."""
    with pytest.raises((AttributeError, TypeError)):
        model.raw_response = object()


def test_strategy_data_protocol_has_only_five_read_operations():
    """A standard autospec enforces the own read-only port surface."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    account_ids = ['source', 'destination']
    uids = ['uid-1', 'uid-2']

    data.get_portfolio('source')
    data.find_instruments('TEST')
    data.get_instrument('uid-1')
    data.get_last_prices(uids)
    data.position_events(account_ids)

    data.get_portfolio.assert_called_once_with('source')
    data.find_instruments.assert_called_once_with('TEST')
    data.get_instrument.assert_called_once_with('uid-1')
    data.get_last_prices.assert_called_once_with(uids)
    data.position_events.assert_called_once_with(account_ids)
    with pytest.raises(AttributeError):
        data.orders = object()
    with pytest.raises(TypeError):
        data.get_portfolio()


def test_data_access_error_is_an_sdk_independent_domain_error():
    """The port exposes its own transport failure without SDK inheritance."""
    error = DataAccessError('read failed')

    assert isinstance(error, Exception)
    assert str(error) == 'read failed'


def test_strategy_data_import_has_no_sdk_dependency_or_import_time_io():
    """A fresh interpreter can import the contract with SDK and I/O blocked."""
    project_root = Path(__file__).resolve().parents[1]
    script = r'''
import builtins
import pathlib
import socket

original_import = builtins.__import__
blocked_modules = (
    'grpc',
    't_tech',
    'autorepeater.account_strategy',
    'autorepeater.index_strategy',
    'autorepeater.strategies',
)

def guarded_import(name, *args, **kwargs):
    if name.startswith(blocked_modules):
        raise AssertionError(f'blocked import: {name}')
    return original_import(name, *args, **kwargs)

def blocked_io(*args, **kwargs):
    raise AssertionError('import-time I/O')

builtins.__import__ = guarded_import
builtins.open = blocked_io
pathlib.Path.open = blocked_io
pathlib.Path.read_text = blocked_io
pathlib.Path.read_bytes = blocked_io
socket.socket = blocked_io
socket.create_connection = blocked_io

import autorepeater.strategy_data as strategy_data

assert strategy_data.InstrumentType.SHARE.value == 'share'
assert len(strategy_data.StrategyData.__dict__) > 0
print('isolated import ok')
'''

    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == 'isolated import ok\n'
