"""Account source snapshots, target quantities, and synchronization events."""
from decimal import Decimal

from autorepeater import reporting
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_decimal_price
from autorepeater.money import get_quantity_position
from autorepeater.portfolio import TargetPortfolio
from autorepeater.portfolio import get_portfolio
from autorepeater.triggers import check_triggers


class AccountStrategy:
    """Repeat one account using fresh snapshots and one subscription per events call."""

    def __init__(self, src):
        self.src = src

    def load_snapshot(self, client):
        """Read and report the source; cash does not contribute to its target or value."""
        reporting.print_account_header('src')
        portfolio = get_portfolio(client, self.src)
        total = Decimal('0')
        positions = {}
        for position in portfolio.positions:
            reporting.print_position(client, position)
            if position.instrument_type != 'currency':
                positions[position.instrument_uid] = position
                total += currency_to_decimal(position)
        reporting.print_total(total)
        return positions, total

    def build_target(self, snapshot, budget):
        """Scale source quantities without SDK calls; keep the original Decimal order."""
        positions, total = snapshot
        ratio = budget / total
        quantities = {}
        prices = {}
        for uid, position in positions.items():
            quantities[uid] = ratio * get_quantity_position(position)
            prices[uid] = currency_to_decimal_price(position)
        return TargetPortfolio(quantities, prices)

    def events(self, client, dst_account_id):
        """Yield sync decisions for one stream; let the engine handle errors and retries."""
        for response in client.operations_stream.positions_stream(
                accounts=[self.src, dst_account_id]):
            triggered = check_triggers(response.position, self.src, dst_account_id)
            if not triggered:
                reporting.print_skipped_event(response)
            yield triggered
