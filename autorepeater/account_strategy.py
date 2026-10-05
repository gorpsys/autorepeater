"""Account source snapshots, target quantities, and synchronization events."""
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from autorepeater import reporting
from autorepeater.account_config import AccountConfig, select_account_config
from autorepeater.portfolio import TargetPortfolio
from autorepeater.rebalance_policy import leaf_decision
from autorepeater.strategy_budget import available_budget
from autorepeater.strategy_contract import PreparationContext
from autorepeater.strategy_data import InstrumentType, PortfolioEntry, PositionEvent, StrategyData
from autorepeater.strategy_allocation import normalize_profile, position_value
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, StrategyPlan, finite_decimal,
    validate_context, validate_plan, validate_positions,
)
from autorepeater.triggers import check_triggers


NANO_QUANT = Decimal('0.000000001')


@dataclass(frozen=True)
class PreparedAccountSource:
    """Account ID and settings fixed during preparation of one launch."""
    src: str
    config: AccountConfig


def prepare_account_source(src: str, context: PreparationContext) -> PreparedAccountSource:
    # pylint: disable=unused-argument
    """Resolve an exact config name; the source ID belongs only to its document."""
    config = select_account_config(src)
    return PreparedAccountSource(config.source_account_id, config)


def _position_value(position: PortfolioEntry) -> Decimal:
    """Preserve the original per-position nano quantization before summing."""
    return (position.current_price * position.quantity).quantize(NANO_QUANT)


class AccountStrategy:
    """Repeat one account using fresh snapshots and pure event predicates."""

    def __init__(self, prepared: PreparedAccountSource) -> None:
        self.src = prepared.config.source_account_id
        self.config = prepared.config

    def load_snapshot(self, data: StrategyData) -> tuple[dict[str, PortfolioEntry], Decimal]:
        """Read and report the source; cash does not contribute to its target or value."""
        reporting.print_account_header('src')
        portfolio = data.get_portfolio(self.src)
        total = Decimal('0')
        positions = {}
        for position in portfolio.positions:
            reporting.print_strategy_position(position)
            if position.instrument_type != InstrumentType.CURRENCY:
                positions[position.uid] = position
                total += _position_value(position)
        reporting.print_total(total)
        return positions, total

    def build_target(self, snapshot: tuple[dict[str, PortfolioEntry], Decimal],
                     budget: Decimal) -> TargetPortfolio:
        """Scale source quantities without SDK calls; keep the original Decimal order."""
        positions, total = snapshot
        budget = available_budget(budget, self.config.reserve)
        ratio = budget / total
        quantities = {}
        prices = {}
        for uid, position in positions.items():
            quantities[uid] = ratio * position.quantity
            prices[uid] = position.current_price
        return TargetPortfolio(quantities, prices)

    def event_accounts(self, dst_account_id: str) -> tuple[str, ...]:
        """Watch source then destination, keeping a shared account only once."""
        return tuple(dict.fromkeys((self.src, dst_account_id)))

    def build_plan(self, snapshot: object, context: StrategyContext) -> StrategyPlan:
        """Build main and invested-value control from one source, without data reads."""
        validate_context(context)
        self.allocation_profile(snapshot)
        snapshot = cast(tuple[dict[str, PortfolioEntry], Decimal], snapshot)
        target = self.build_target(snapshot, context.budget)
        cash_floor = context.budget * self.config.reserve
        invested = position_value(context.positions, context.marks)
        control = self.build_target(snapshot, invested) if invested > 0 else TargetPortfolio({}, {})
        plan = StrategyPlan(
            context.path, context.budget, target, cash_floor,
            leaf_decision(context, control, self.config.allocation_drift_limit), (), {},
            context.positions)
        validate_plan(plan, context)
        return plan

    def allocation_profile(self, snapshot: object) -> AllocationProfile:
        """Expose full source weights with the original per-position nano valuation."""
        if not isinstance(snapshot, tuple) or len(snapshot) != 2:
            raise ValueError('account profile snapshot: expected positions and total')
        positions, total = snapshot
        finite_decimal(total, 'account source total', positive=True)
        if not isinstance(positions, dict):
            raise ValueError('account profile snapshot positions: expected a dict')
        for uid, position in positions.items():
            if not isinstance(position, PortfolioEntry) or uid != position.uid:
                raise ValueError(f'account profile snapshot: invalid position for UID {uid}')
        securities = tuple(position for position in positions.values()
                           if position.instrument_type != InstrumentType.CURRENCY)
        validate_positions(securities)
        values = {position.uid: _position_value(position) for position in securities}
        if sum(values.values(), Decimal(0)) != total:
            raise ValueError('account profile snapshot: source total mismatch')
        prices = {position.uid: position.current_price for position in securities}
        return normalize_profile(values, prices, self.config.reserve)

    def should_rebalance(self, event: PositionEvent, dst_account_id: str) -> bool:
        """Source changes and unblocked populated destinations initiate a check."""
        return check_triggers(event, self.src, dst_account_id)
