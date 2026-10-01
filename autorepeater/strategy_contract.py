"""Shared structural contract and runtime validation for strategies."""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_data import StrategyData


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

    def events(self, data: StrategyData, dst_account_id: str) -> Iterable[bool]:
        """Yield synchronization decisions from one event subscription."""


def validate_strategy(strategy):
    """Validate the runtime surface without invoking methods or reading settings."""
    for method_name in ('load_snapshot', 'build_target', 'events'):
        if not callable(getattr(strategy, method_name, None)):
            raise TypeError(f'strategy {method_name} must be callable')

    return strategy
