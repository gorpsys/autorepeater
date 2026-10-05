"""Neutral execution models remain independent of SDK services."""
from dataclasses import fields, FrozenInstanceError
from decimal import Decimal

import pytest

from autorepeater.execution_data import (
    ActiveOrder, ExecutionData, ExecutionDataError, ExecutionSnapshot, TradeRules,
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
        'get_destination', 'get_trade_rules',
    }
    # Exercise Protocol default bodies as well as concrete implementations.
    assert ExecutionData.get_destination(None, 'account') is None
    assert ExecutionData.get_trade_rules(None, 'account', ()) is None
