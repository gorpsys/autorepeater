"""Order DTOs and order calculation helpers."""
import dataclasses

from t_tech.invest import OrderDirection
from t_tech.invest import OrderType

from autorepeater.money import currency_to_decimal_price


@dataclasses.dataclass
class OrderParams:
    """struct for order params"""
    instrument_id: str
    quantity: int
    direction: OrderDirection
    order_type: OrderType


def get_max_sum_positions_price(sell_orders_params, buy_orders_params,
                                src_positions, dst_positions):
    """get max sum orders price for buy or sell orders"""
    total_sell = 0
    for order_params in sell_orders_params:
        position = dst_positions[order_params.instrument_id]
        total_sell += (currency_to_decimal_price(position) *
                       order_params.quantity)

    total_buy = 0
    for order_params in buy_orders_params:
        position = src_positions[order_params.instrument_id]
        total_buy += (currency_to_decimal_price(position) *
                      order_params.quantity)

    return max(total_sell, total_buy)
