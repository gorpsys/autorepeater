"""Leaf reserves preserve Decimal arithmetic and the pure calculator's budget."""
from dataclasses import replace
from decimal import Decimal

import pytest

from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.index_config import IndexConfig, IndexInstrument
from autorepeater.index_strategy import IndexQuote, IndexStrategy, build_index_target
from autorepeater.strategy_budget import available_budget
from autorepeater.strategy_data import InstrumentType, PortfolioEntry


@pytest.mark.parametrize('gross, reserve, expected', [
    ('170', '0.1', '153.0'), ('100', '0', '100'),
    ('0', '0.01', '0.00'), ('-100', '0.01', '-99.00'),
])
def test_available_budget_preserves_sign(gross, reserve, expected):
    """The shared arithmetic does not impose INDEX's positive-budget requirement."""
    assert available_budget(Decimal(gross), Decimal(reserve)) == Decimal(expected)


@pytest.mark.parametrize('gross, expected', [('170', '7.875'), ('0', '0'), ('-170', '-7.875')])
def test_account_reserve_before_ratio(gross, expected):
    """ACCOUNT reserves gross value once, then divides before scaling quantities."""
    strategy = AccountStrategy(PreparedAccountSource('00123', AccountConfig(Decimal('0.1'))))
    entry = PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('4'), Decimal('10.5'), 'one')
    snapshot = ({'uid': entry}, Decimal('204'))
    target = strategy.build_target(snapshot, Decimal(gross))
    assert target.quantities == {'uid': Decimal(expected)}
    assert target.prices == {'uid': Decimal('4')}


def test_account_reserve_decimal_operation_order():
    """Moving reserve after division or multiplying by source weights changes the last digit."""
    strategy = AccountStrategy(PreparedAccountSource('00123', AccountConfig(Decimal('0.01'))))
    entry = PortfolioEntry('uid', InstrumentType.SHARE, 'RUB', Decimal('1'), Decimal('3'), 'one')
    target = strategy.build_target(({'uid': entry}, Decimal('7')), Decimal('1'))
    assert target.quantities == {'uid': Decimal('0.4242857142857142857142857142')}


def single_index(reserve):
    """One instrument exposes double reserving at an exact lot boundary."""
    config = IndexConfig('ONE', Decimal('0.05'), [IndexInstrument(
        'ONE', Decimal('1'), Decimal('1'), Decimal('1'), Decimal('10'),
        Decimal('100'), Decimal('100'))], reserve=Decimal(reserve))
    return config, {'ONE': IndexQuote('uid', Decimal('10'), 1)}


@pytest.mark.parametrize('reserve, quantity', [('0.1', '9'), ('0', '10')])
def test_index_leaf_reserves_gross_once(reserve, quantity):
    """Only the strategy subtracts reserve; pure callers already supply available money."""
    config, snapshot = single_index(reserve)
    assert IndexStrategy(config).build_target(snapshot, Decimal('100')).quantities == {
        'uid': Decimal(quantity)}
    target = build_index_target(config, snapshot, Decimal('90'))
    assert target.quantities == {'uid': Decimal('9')}
    assert build_index_target(
        replace(config, reserve=Decimal('0.5')), snapshot, Decimal('90')) == target


@pytest.mark.parametrize('budget', [Decimal('0'), Decimal('-1'), Decimal('NaN'),
                                   Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity'),
                                   100, True, '100', None])
def test_index_leaf_keeps_budget_errors(budget):
    """Invalid gross budgets still fail with a descriptive ValueError before snapshot access."""
    config, _ = single_index('0.1')
    with pytest.raises(ValueError, match='budget'):
        IndexStrategy(config).build_target({}, budget)
