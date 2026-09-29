"""Order DTOs and order calculation helpers."""
import dataclasses
from decimal import Decimal

from t_tech.invest import OrderDirection
from t_tech.invest import OrderType


@dataclasses.dataclass
class OrderParams:
    """struct for order params"""
    instrument_id: str
    quantity: int
    direction: OrderDirection
    order_type: OrderType


def get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                sell_prices, buy_prices):
    """get max sum orders price for buy or sell orders"""
    total_sell = Decimal('0')
    for order_params in sell_orders_params:
        total_sell += (sell_prices[order_params.instrument_id] *
                       order_params.quantity)

    total_buy = Decimal('0')
    for order_params in buy_orders_params:
        total_buy += (buy_prices[order_params.instrument_id] *
                      order_params.quantity)

    return max(total_sell, total_buy)
