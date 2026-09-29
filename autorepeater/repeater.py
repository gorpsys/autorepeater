"""Account synchronization logic."""
from decimal import Decimal

from t_tech.invest import InstrumentIdType
from t_tech.invest import OrderDirection
from t_tech.invest import OrderType
from t_tech.invest import RequestError
from t_tech.invest import SecurityTradingStatus

from autorepeater.constants import DST_MONEY_RESERVED
from autorepeater.constants import THRESHOLD
from autorepeater.logging_config import logger
from autorepeater.money import currency_to_decimal
from autorepeater.money import get_quantity_position
from autorepeater.orders import OrderParams
from autorepeater.orders import get_max_sum_positions_price
from autorepeater.portfolio import get_portfolio
from autorepeater import reporting
from autorepeater.triggers import check_triggers


class AutoRepeater:
    """Main class for automatically repeating operations of one account over another account."""

    def __init__(self, client):
        self.client = client
        self.debug = False
        self.threshold = Decimal(THRESHOLD)
        self.reserve = Decimal(DST_MONEY_RESERVED)

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
            if reserve < 0 or reserve > 1:
                raise ValueError("Reserve must be between 0 and 1")
            # Оставляем преобразование здесь, так как входной параметр float
            self.reserve = Decimal(str(reserve))

    def calc_ratio(self, src_account_id, dst_account_id):
        """calc ratio and print src and dst accounts"""
        reporting.print_account_header('src')
        portfolio_src = get_portfolio(self.client, src_account_id)
        total_src = Decimal('0')
        src_positions = {}
        for position in portfolio_src.positions:
            reporting.print_position(self.client, position)
            if position.instrument_type != 'currency':
                src_positions[position.instrument_uid] = position
                total_src += currency_to_decimal(position)
        reporting.print_total(total_src)

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

        ratio = total_dst / total_src
        return (src_positions, dst_positions, ratio, total_dst)

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

    def calc_buy_positions(self, src_positions, dst_positions,
                           target_positions):
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
                position = src_positions[item_id]
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

    def sync_accounts(self, src_account_id, dst_account_id):
        """sync positions from src account to dst account"""
        (src_positions, dst_positions, ratio, total_dst) = (
            self.calc_ratio(src_account_id, dst_account_id))
        target_positions = {}
        for item_id, item_value in src_positions.items():
            target_positions[item_id] = ratio * \
                get_quantity_position(item_value)

        orders_params_sell = self.calc_sell_positions(
            dst_positions, target_positions)
        orders_params_buy = self.calc_buy_positions(
            src_positions, dst_positions, target_positions)

        if (not self.debug) and (
                get_max_sum_positions_price(orders_params_sell, orders_params_buy,
                                            src_positions, dst_positions) >
                total_dst * self.threshold):
            self.post_orders(
                dst_account_id,
                orders_params_sell,
                orders_params_buy)

    def mainflow(self, src, dst):
        """sync accounts when changing"""
        try:
            self.sync_accounts(src, dst)
        except RequestError as err:
            logger.error(err)

        while True:
            try:
                for response in self.client.operations_stream.positions_stream(
                        accounts=[src, dst]):
                    if check_triggers(response.position, src, dst):
                        self.sync_accounts(src, dst)
                    else:
                        reporting.print_skipped_event(response)
            except RequestError as err:
                logger.error(err)
