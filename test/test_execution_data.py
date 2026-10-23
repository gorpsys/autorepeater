"""Neutral execution models remain independent of SDK services."""
from dataclasses import fields, FrozenInstanceError
from decimal import Decimal
from itertools import product
from unittest.mock import create_autospec

import pytest

from autorepeater.execution_data import (
    AccountReadiness, ActiveOrder, ExecutionData, ExecutionDataError, ExecutionSnapshot, TradeRules,
)
from autorepeater.strategy_data import PortfolioSnapshot


def test_neutral_snapshot_and_rules():
    """Native money, pieces and lots occupy separate fields."""
    rules = TradeRules(10, 'rub', True, True, Decimal('12.5'), 2, 3)
    order = ActiveOrder('order', 'uid', 'NEW', 3, 0)
    snapshot = ExecutionSnapshot(PortfolioSnapshot(()), Decimal(100), {}, {},
                                 {'rub': Decimal('12.5')}, {}, (order,), False)
    assert rules.lot == 10
    assert rules.buy_max_lots == 2
    assert snapshot.active_orders == (order,)
    with pytest.raises(FrozenInstanceError):
        rules.lot = 5
    assert [field.name for field in fields(rules)] == [
        'lot', 'currency', 'api_trade_available', 'bestprice_order_available',
        'buy_money_amount', 'buy_max_lots', 'sell_max_lots',
    ]
    assert issubclass(ExecutionDataError, Exception)


def test_read_port_has_no_write_surface():
    """Protocol declares account-dependent reads only."""
    assert {name for name in vars(ExecutionData) if not name.startswith('_')} == {
        'get_readiness', 'get_destination', 'get_trade_rules',
    }
    # Exercise Protocol default bodies as well as concrete implementations.
    assert ExecutionData.get_destination(None, 'account') is None
    assert ExecutionData.get_trade_rules(None, 'account', ()) is None
    assert ExecutionData.get_readiness(None, 'account', timeout=1) is None


@pytest.mark.parametrize('loading,portfolio,positions,orders',
                         list(product((False, True), repeat=4)))
def test_readiness_predicate_and_frozen_fields(loading, portfolio, positions, orders):
    """Every blocker independently prevents readiness without financial fields."""
    result = AccountReadiness(loading, portfolio, positions, int(orders))
    assert result.ready is not any((loading, portfolio, positions, orders))
    assert [field.name for field in fields(result)] == [
        'loading', 'portfolio_blocked', 'positions_blocked', 'active_order_count']
    with pytest.raises(FrozenInstanceError):
        result.loading = False
    with pytest.raises((AttributeError, TypeError)):
        result.ready = True


@pytest.mark.parametrize('field', ['loading', 'portfolio_blocked', 'positions_blocked'])
@pytest.mark.parametrize('value', [None, 0, 1, 'false', Decimal(0)])
def test_readiness_requires_boolean_flags(field, value):
    """Malformed flags must never authorize a read as ready."""
    kwargs = {'loading': False, 'portfolio_blocked': False, 'positions_blocked': False,
              'active_order_count': 0}
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        AccountReadiness(**kwargs)


@pytest.mark.parametrize('value', [None, True, False, -1, 0.0, '0', Decimal(0)])
def test_readiness_requires_nonnegative_integer_count(value):
    """bool and coercible numbers are not order counts."""
    with pytest.raises(ValueError, match='active_order_count'):
        AccountReadiness(False, False, False, value)


def test_readiness_port_requires_keyword_timeout():
    """Strict port doubles enforce the caller's remaining shared budget."""
    data = create_autospec(ExecutionData, instance=True, spec_set=True)
    data.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    assert data.get_readiness('account', timeout=2).ready
    with pytest.raises(TypeError):
        data.get_readiness('account')
    with pytest.raises(TypeError):
        data.get_readiness('account', 2)
