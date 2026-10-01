"""Ordered composition of opaque child snapshots, targets and event decisions."""
from dataclasses import dataclass
from decimal import Decimal

from autorepeater.composite_config import select_composite_config
from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_contract import PreparedStrategy, create_strategy


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
        for item in config.components))


class CompositeStrategy:
    """Combine complete child targets without knowing their types or reserves."""

    def __init__(self, prepared):
        self.source = prepared
        self.children = tuple(create_strategy(item.prepared) for item in prepared.components)

    def load_snapshot(self, data):
        """Read each child anew, retaining its opaque snapshot in declaration order."""
        return CompositeSnapshot(tuple(child.load_snapshot(data) for child in self.children))

    def build_target(self, snapshot, budget):
        """Allocate full budgets and validate every child before deciding on a skip."""
        if not isinstance(budget, Decimal) or not budget.is_finite() or budget <= 0:
            raise ValueError('composite budget must be a positive finite Decimal')
        targets = [child.build_target(child_snapshot, budget * component.weight)
                   for child, child_snapshot, component in
                   zip(self.children, snapshot.children, self.source.components)]
        for target in targets:
            validate_target(target)
        for component, target in zip(self.source.components, targets):
            if not target.quantities:
                label = f'{component.algoritm}/{component.src}'
                reason = target.empty_reason or 'empty target'
                # A nested composition already includes its own algorithm/source label.
                detail = reason if reason.startswith(label + ' -> ') else f'{label}: {reason}'
                return TargetPortfolio({}, {}, f'COMPOSITE/{self.source.name} -> {detail}')
        quantities = {}
        prices = {}
        for target in targets:
            for uid, quantity in target.quantities.items():
                quantities[uid] = quantities.get(uid, Decimal('0')) + quantity
                price = target.prices[uid]
                prices[uid] = max(prices[uid], price) if uid in prices else price
        return TargetPortfolio(quantities, prices)

    def event_accounts(self, dst_account_id):
        """Validate each child's declaration before forming one ordered account union."""
        accounts = {}
        for child in self.children:
            declared = child.event_accounts(dst_account_id)
            if not isinstance(declared, tuple) or not declared:
                raise ValueError('strategy event_accounts must return a nonempty tuple')
            for account in declared:
                if (not isinstance(account, str) or not account
                        or any(char.isspace() for char in account)):
                    raise ValueError('strategy event_accounts must contain account strings')
            if len(set(declared)) != len(declared):
                raise ValueError('strategy event_accounts must not contain duplicates')
            accounts.update(dict.fromkeys(declared))
        return tuple(accounts)

    def should_rebalance(self, event, dst_account_id):
        """Evaluate and type-check every child before combining their decisions."""
        decisions = [child.should_rebalance(event, dst_account_id) for child in self.children]
        if any(not isinstance(decision, bool) for decision in decisions):
            raise ValueError('strategy should_rebalance must return bool')
        return any(decisions)
