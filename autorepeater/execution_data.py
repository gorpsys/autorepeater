"""Neutral read-only execution state; quantities are pieces and caps are lots."""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from autorepeater.strategy_data import PortfolioSnapshot

if TYPE_CHECKING:
    from autorepeater.fx import FxSnapshot
    from autorepeater.valuation import Valuation


@dataclass(frozen=True, slots=True)
class AccountReadiness:
    """Validated account blockers without prices, cash or a financial plan."""
    loading: bool
    portfolio_blocked: bool
    positions_blocked: bool
    active_order_count: int

    def __post_init__(self):
        for name in ('loading', 'portfolio_blocked', 'positions_blocked'):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f'{name} must be bool')
        if (isinstance(self.active_order_count, bool)
                or not isinstance(self.active_order_count, int) or self.active_order_count < 0):
            raise ValueError('active_order_count must be a nonnegative integer')

    @property
    def ready(self) -> bool:
        """All explicit blockers must be clear before planning can begin."""
        return not (self.loading or self.portfolio_blocked or self.positions_blocked
                    or self.active_order_count)


@dataclass(frozen=True, slots=True)
class ActiveOrder:
    """An outstanding order without SDK enums or monetary estimates."""
    order_id: str
    uid: str
    status: str
    lots_requested: int
    lots_executed: int


@dataclass(frozen=True, slots=True)
class TradeRules:  # pylint: disable=too-many-instance-attributes
    """Current permissions and own, nonmargin limits in native currency."""
    lot: int
    currency: str
    api_trade_available: bool
    bestprice_order_available: bool
    buy_money_amount: Decimal
    buy_max_lots: int
    sell_max_lots: int


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:  # pylint: disable=too-many-instance-attributes
    """Full valuation plus conservative cash/piece bounds for one account read."""
    portfolio: PortfolioSnapshot
    budget: Decimal
    quantities: dict[str, Decimal]
    marks: dict[str, Decimal]
    available_cash: dict[str, Decimal]
    available_quantities: dict[str, Decimal]
    active_orders: tuple[ActiveOrder, ...]
    limits_ready: bool
    fx_snapshot: 'FxSnapshot | None' = None


class ExecutionDataError(Exception):
    """Transport failure reading execution state, with the original cause."""


class ExecutionData(Protocol):
    """Account-dependent reads only; never submits or cancels orders."""

    def get_readiness(self, account_id: str, *, timeout: float) -> AccountReadiness:
        """Read blockers only, within one positive finite timeout for all reads."""

    def get_destination(self, account_id: str, *,
                        valuation: 'Valuation | None' = None) -> ExecutionSnapshot:
        """Read a fresh destination, including readiness and physical limits."""

    def get_trade_rules(self, account_id: str, uids: Sequence[str]) -> dict[str, TradeRules]:
        """Read current own caps and API/BESTPRICE permissions per instrument."""
