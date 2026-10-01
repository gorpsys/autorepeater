"""Account source snapshots, target quantities, and synchronization events."""
from dataclasses import dataclass
from decimal import Decimal

from autorepeater import reporting
from autorepeater.account_config import AccountConfig, load_account_config
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_budget import available_budget
from autorepeater.strategy_data import InstrumentType
from autorepeater.strategy_contract import UnsupportedSourceError
from autorepeater.triggers import check_triggers


NANO_QUANT = Decimal('0.000000001')


@dataclass(frozen=True)
class PreparedAccountSource:
    """Account ID and settings fixed during preparation of one launch."""
    src: str
    config: AccountConfig


def prepare_account_source(src):
    """Validate an ASCII account ID, then load only its own settings."""
    if not isinstance(src, str) or not src.isascii() or not src.isdecimal():
        raise UnsupportedSourceError(f'unsupported src: {src}')
    return PreparedAccountSource(src, load_account_config())


def _position_value(position):
    """Preserve the original per-position nano quantization before summing."""
    return (position.current_price * position.quantity).quantize(NANO_QUANT)


class AccountStrategy:
    """Repeat one account using fresh snapshots and pure event predicates."""

    def __init__(self, prepared):
        self.src = prepared.src
        self.config = prepared.config

    def load_snapshot(self, data):
        """Read and report the source; cash does not contribute to its target or value."""
        reporting.print_account_header('src')
        portfolio = data.get_portfolio(self.src)
        total = Decimal('0')
        positions = {}
        for position in portfolio.positions:
            reporting.print_strategy_position(data, position)
            if position.instrument_type != InstrumentType.CURRENCY:
                positions[position.uid] = position
                total += _position_value(position)
        reporting.print_total(total)
        return positions, total

    def build_target(self, snapshot, budget):
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

    def event_accounts(self, dst_account_id):
        """Watch source then destination, keeping a shared account only once."""
        return tuple(dict.fromkeys((self.src, dst_account_id)))

    def should_rebalance(self, event, dst_account_id):
        """Preserve the source-securities and first destination-money conditions."""
        return check_triggers(event, self.src, dst_account_id)
