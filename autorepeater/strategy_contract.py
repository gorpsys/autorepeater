"""Shared structural contract and runtime validation for strategies."""
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import PositionEvent, StrategyData


class UnsupportedSourceError(ValueError):
    """The selected algorithm cannot prepare this source."""


@dataclass(frozen=True)
class AlgorithmDefinition:
    """Separate source preparation from strategy construction."""
    prepare_source: Callable[[str], object]
    create: Callable[[object], 'Strategy']


@dataclass(frozen=True)
class PreparedStrategy:
    """One launch's factory and opaque prepared source, with a display label."""
    create: Callable[[object], 'Strategy']
    prepared_source: object
    source_display: str


class Strategy(Protocol):
    """Behavior required by the rebalancing engine."""

    def load_snapshot(self, data: StrategyData) -> object:
        """Load one source snapshot."""

    def build_target(self, snapshot: object, budget: Decimal) -> TargetPortfolio:
        """Build a complete target from a snapshot and the full allocated budget."""

    def event_accounts(self, dst_account_id: str) -> tuple[str, ...]:
        """Declare nonempty, ordered, unique account IDs without reading data."""

    def should_rebalance(self, event: PositionEvent, dst_account_id: str) -> bool:
        """Decide strictly bool from one event without data reads or logging."""


def validate_strategy(strategy):
    """Validate the runtime surface without invoking methods or reading settings."""
    for method_name in ('load_snapshot', 'build_target', 'event_accounts', 'should_rebalance'):
        if not callable(getattr(strategy, method_name, None)):
            raise TypeError(f'strategy {method_name} must be callable')

    return strategy
