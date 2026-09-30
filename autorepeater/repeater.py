"""Account synchronization logic."""
from decimal import Decimal

from t_tech.invest import InstrumentIdType
from t_tech.invest import OrderDirection
from t_tech.invest import OrderType
from t_tech.invest import RequestError
from t_tech.invest import SecurityTradingStatus

from autorepeater.constants import THRESHOLD
from autorepeater.logging_config import logger
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_decimal_price
from autorepeater.money import get_quantity_position
from autorepeater.orders import OrderParams
from autorepeater.orders import get_max_sum_positions_price
from autorepeater.portfolio import get_portfolio
from autorepeater.portfolio import validate_target
from autorepeater.strategy_contract import validate_strategy
from autorepeater.strategy_data import DataAccessError
from autorepeater import reporting


class AutoRepeater:
    """Rebalance a destination using a strategy's targets and synchronization events."""

    def __init__(self, client, strategy, data):
        self.client = client
        self.strategy = validate_strategy(strategy)
        self.data = data
        self.debug = False
        self.threshold = Decimal(THRESHOLD)
        self.reserve = strategy.default_reserve

    def set_debug(self, debug):
        """set debug flag"""
        if not isinstance(debug, bool):
            raise TypeError("Debug flag must be boolean")
        self.debug = debug

    def set_threshold(self, threshold):
        """set threshod"""
        if threshold is not None:
            if threshold < 0 or threshold > 1:
                raise ValueError("Threshold must be between 0 and 1")
            # Оставляем преобразование здесь, так как входной параметр float
            self.threshold = Decimal(str(threshold))

    def set_reserve(self, reserve):
        """set reserve"""
        if reserve is not None:
            if isinstance(reserve, bool) or not isinstance(reserve, (int, float, Decimal)):
                raise TypeError("Reserve must be between 0 and 1")
            value = Decimal(str(reserve))
            if not value.is_finite() or value < 0 or value > 1:
                raise ValueError("Reserve must be between 0 and 1")
            self.reserve = value

    def calc_sell_positions(self, dst_positions, target_positions):
        """calc extra positions from dst accounts for sell"""
        result = []
        for item_id, item_value in dst_positions.items():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=item_id).instrument
            if (instrument.trading_status != SecurityTradingStatus.
                    SECURITY_TRADING_STATUS_NORMAL_TRADING):
                continue
            if item_id not in target_positions:
                quantity = round(get_quantity_position(
                    item_value) / Decimal(str(instrument.lot)))
                if quantity > 0:
                    reporting.print_sell(instrument, quantity)
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_SELL,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
            elif target_positions[item_id] < get_quantity_position(item_value):
                quantity = round((get_quantity_position(
                    item_value) - target_positions[item_id]) / Decimal(str(instrument.lot)))
                if quantity > 0:
                    reporting.print_sell(instrument, quantity)
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_SELL,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
        return result

    def calc_buy_positions(self, dst_positions, target_positions):
        """calc missing positions from dst account for buy"""
        result = []
        for item_id, item_value in target_positions.items():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=item_id).instrument
            if (instrument.trading_status != SecurityTradingStatus.
                    SECURITY_TRADING_STATUS_NORMAL_TRADING):
                continue
            if item_id not in dst_positions:
                quantity = round(item_value / Decimal(str(instrument.lot)))
                if quantity > 0:
                    reporting.print_buy(instrument, quantity)
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_BUY,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
            elif item_value > get_quantity_position(dst_positions[item_id]):
                position = dst_positions[item_id]
                quantity = round((item_value -
                                  get_quantity_position(position)) /
                                 Decimal(str(instrument.lot)))
                if quantity > 0:
                    reporting.print_buy(instrument, quantity)
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_BUY,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
        return result

    def post_orders(self, dst_account_id, orders_params_sell,
                    orders_params_buy):
        """post all orders"""
        for order_params in orders_params_sell:
            reporting.print_order(order_params)
            reporting.print_order_result(
                self.client.orders.post_order(
                    instrument_id=order_params.instrument_id,
                    quantity=order_params.quantity,
                    direction=order_params.direction,
                    account_id=dst_account_id,
                    order_type=order_params.order_type).order_id)
        for order_params in orders_params_buy:
            reporting.print_order(order_params)
            reporting.print_order_result(
                self.client.orders.post_order(
                    instrument_id=order_params.instrument_id,
                    quantity=order_params.quantity,
                    direction=order_params.direction,
                    account_id=dst_account_id,
                    order_type=order_params.order_type).order_id)

    def sync_accounts(self, dst_account_id):
        """Build and validate a fresh target before calculating any orders."""
        snapshot = self.strategy.load_snapshot(self.data)
        reporting.print_account_header('dst')
        portfolio_dst = get_portfolio(self.client, dst_account_id)
        total_dst = Decimal('0')
        dst_positions = {}
        for position in portfolio_dst.positions:
            reporting.print_position(self.client, position)
            if position.instrument_type != 'currency':
                dst_positions[position.instrument_uid] = position
            total_dst += currency_to_decimal(position)
        total_dst = total_dst * (Decimal('1') - self.reserve)
        reporting.print_total(total_dst)

        target = self.strategy.build_target(snapshot, total_dst)
        validate_target(target)
        if not target.quantities:
            reporting.print_empty_target(dst_account_id)
            return

        orders_params_sell = self.calc_sell_positions(
            dst_positions, target.quantities)
        orders_params_buy = self.calc_buy_positions(
            dst_positions, target.quantities)

        if (not self.debug) and (
                get_max_sum_positions_price(orders_params_sell, orders_params_buy,
                                            {uid: currency_to_decimal_price(position)
                                             for uid, position in dst_positions.items()},
                                            target.prices) >
                total_dst * self.threshold):
            self.post_orders(
                dst_account_id,
                orders_params_sell,
                orders_params_buy)

    def mainflow(self, dst):
        """sync accounts when changing"""
        try:
            self.sync_accounts(dst)
        except (DataAccessError, RequestError) as err:
            logger.error(err)

        while True:
            try:
                for triggered in self.strategy.events(self.data, dst):
                    if triggered:
                        self.sync_accounts(dst)
            except (DataAccessError, RequestError) as err:
                logger.error(err)
