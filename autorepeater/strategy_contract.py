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
class PreparedStrategy:
    """One launch's factory and opaque prepared source, with a display label."""
    create: Callable[[object], 'Strategy']
    prepared_source: object
    source_display: str


class PreparationContext(Protocol):  # pylint: disable=too-few-public-methods
    """Prepare nested sources without depending on a registry or concrete strategies."""

    def prepare(self, algoritm: str, src: str) -> PreparedStrategy:
        """Prepare one child in the current launch's active path."""


@dataclass(frozen=True)
class AlgorithmDefinition:
    """Separate source preparation from strategy construction."""
    prepare_source: Callable[[str, PreparationContext], object]
    create: Callable[[object], 'Strategy']


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


def create_strategy(prepared):
    """Use the saved factory and data, then validate without registry or file access."""
    if not isinstance(prepared, PreparedStrategy):
        raise TypeError('create_strategy requires PreparedStrategy')
    return validate_strategy(prepared.create(prepared.prepared_source))
