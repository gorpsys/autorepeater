"""Ordered composition of opaque child snapshots, targets and event decisions."""
from dataclasses import dataclass
from decimal import Decimal

from autorepeater.composite_config import select_composite_config
from autorepeater.portfolio import TargetPortfolio
from autorepeater.rebalance_policy import allocate_component_budgets
from autorepeater.strategy_contract import PreparedStrategy, create_strategy
from autorepeater.strategy_contract import validate_event_accounts
from autorepeater.strategy_allocation import combine_profiles
from autorepeater.strategy_plan import StrategyPlan, exact_sum, validate_context, validate_plan


@dataclass(frozen=True)
class PreparedCompositeComponent:
    """One occurrence's allocation, diagnostic reference and saved child factory."""
    algoritm: str
    src: str
    weight: Decimal
    prepared: PreparedStrategy


@dataclass(frozen=True)
class PreparedCompositeSource:
    """A selected composition fixed during preparation of one launch."""
    name: str
    components: tuple[PreparedCompositeComponent, ...]
    component_drift_limit: Decimal


@dataclass(frozen=True)
class CompositeSnapshot:
    """Ordered child snapshots whose contents belong only to their strategies."""
    children: tuple[object, ...]


def prepare_composite_source(src, context):
    """Select only this config and prepare each child through the neutral context."""
    config = select_composite_config(src)
    return PreparedCompositeSource(config.name, tuple(
        PreparedCompositeComponent(item.algoritm, item.src, item.weight,
                                   context.prepare(item.algoritm, item.src))
        for item in config.components), config.component_drift_limit)


class CompositeStrategy:
    """Combine complete child targets without knowing their types or reserves."""

    def __init__(self, prepared):
        self.source = prepared
        self.children = tuple(create_strategy(item.prepared) for item in prepared.components)

    def load_snapshot(self, data):
        """Read each child anew, retaining its opaque snapshot in declaration order."""
        return CompositeSnapshot(tuple(child.load_snapshot(data) for child in self.children))

    def event_accounts(self, dst_account_id):
        """Validate each child's declaration before forming one ordered account union."""
        accounts = {}
        for child in self.children:
            declared = validate_event_accounts(child.event_accounts(dst_account_id))
            accounts.update(dict.fromkeys(declared))
        return tuple(accounts)

    def build_plan(self, snapshot, context):
        """Keep opaque child boundaries and permissions through adaptive allocation."""
        validate_context(context)
        profiles = self._child_profiles(snapshot)
        allocation = allocate_component_budgets(
            context, profiles, tuple(item.weight for item in self.source.components),
            self.source.component_drift_limit)
        plans = []
        for child, child_snapshot, child_context in zip(
                self.children, snapshot.children, allocation.children):
            plan = child.build_plan(child_snapshot, child_context)
            validate_plan(plan, child_context)
            plans.append(plan)
        quantities, prices = {}, {}
        for plan in plans:
            for uid, quantity in plan.target.quantities.items():
                quantities[uid] = quantities.get(uid, Decimal(0)) + quantity
                price = plan.target.prices[uid]
                prices[uid] = max(prices[uid], price) if uid in prices else price
        plan = StrategyPlan(
            context.path, context.budget, TargetPortfolio(quantities, prices),
            exact_sum((allocation.unallocated_budget, *(item.cash_floor for item in plans))),
            allocation.decision, tuple(plans), allocation.unassigned, context.positions)
        validate_plan(plan, context)
        return plan

    def allocation_profile(self, snapshot):
        """Compose opaque child profiles without knowing their types or snapshots."""
        profiles = self._child_profiles(snapshot)
        weights = tuple(component.weight for component in self.source.components)
        return combine_profiles(profiles, weights)

    def _child_profiles(self, snapshot):
        """Validate occurrence count before returning snapshots to their owners."""
        if (not isinstance(snapshot, CompositeSnapshot)
                or not isinstance(snapshot.children, tuple)
                or len(snapshot.children) != len(self.children)):
            raise ValueError('composite profile snapshot must match child occurrences')
        return tuple(child.allocation_profile(child_snapshot)
                     for child, child_snapshot in zip(self.children, snapshot.children))

    def should_rebalance(self, event, dst_account_id):
        """Evaluate and type-check every child before combining their decisions."""
        decisions = [child.should_rebalance(event, dst_account_id) for child in self.children]
        if any(not isinstance(decision, bool) for decision in decisions):
            raise ValueError('strategy should_rebalance must return bool')
        return any(decisions)
