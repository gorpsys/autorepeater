"""A robot for automatically repeating operations of one account over another account"""
import dataclasses
import logging
from decimal import Decimal, getcontext

from tinkoff.invest import Client
from tinkoff.invest.constants import INVEST_GRPC_API
from tinkoff.invest import InstrumentIdType
from tinkoff.invest import OrderDirection
from tinkoff.invest import OrderType
from tinkoff.invest import SecurityTradingStatus
from tinkoff.invest import RequestError

DST_MONEY_RESERVED = '0.01'
THRESHOLD = '0.004'
IMPORTANT = 25

# Устанавливаем точность для Decimal
getcontext().prec = 28


class GetInstrumentException(Exception):
    """instrument not found by instrument_id"""


def format_decimal(value):
    """Format Decimal to string with proper formatting
    Args:
        value: Decimal value to format
    Returns:
        str: Formatted string representation of Decimal
    """
    # Форматируем Decimal в строку, избегая научной нотации
    formatted = format(value, 'f')  # Используем 'f' для десятичного формата
    # Проверяем, есть ли точка в числе
    if '.' in formatted:
        # Разделяем на целую и дробную части
        integer_part, fractional_part = formatted.split('.')
        # Убираем лишние нули в дробной части
        fractional_part = fractional_part.rstrip('0')
        # Если дробная часть пустая, добавляем .0
        if not fractional_part:
            return integer_part + '.0'
        return integer_part + '.' + fractional_part
    # Если число целое, добавляем .0
    return formatted + '.0'


def money_to_string(money):
    """convert money to human-readable string"""
    result = money.currency
    value = (Decimal(money.units) +
             Decimal(money.nano) / Decimal('1000000000'))
    formatted = format_decimal(value)
    result += ' - ' + formatted
    return result


def blocked_to_string(blocked):
    """convert blocked money to human-readable string"""
    result = 'blocked ' + money_to_string(blocked)
    return result


def no_money_to_string(share):
    """convert no money instrument to human-readable string"""
    result = share.name + '(' + share.ticker + ')'
    return result


def currency_to_decimal(position):
    """convert position full price value to Decimal"""
    price = (Decimal(position.current_price.units) +
             Decimal(position.current_price.nano) / Decimal('1000000000'))
    quantity = (Decimal(position.quantity.units) +
                Decimal(position.quantity.nano) / Decimal('1000000000'))
    # Округляем результат до 9 знаков после запятой (максимальная точность
    # nano)
    return (price * quantity).quantize(Decimal('0.000000001'))


def currency_to_decimal_price(position):
    """convert position price value to Decimal"""
    return (Decimal(position.current_price.units) +
            Decimal(position.current_price.nano) / Decimal('1000000000'))


def currency_to_string(position):
    """convert position price value to human-readable string"""
    result = position.current_price.currency
    value = currency_to_decimal(position)

    formatted = format_decimal(value)

    result += ' - ' + formatted
    return result


def get_quantity_position(position):
    """get quantity from position as Decimal"""
    return (Decimal(position.quantity.units) +
            Decimal(position.quantity.nano) / Decimal('1000000000'))


def check_triggers(position, src_account, dst_account):
    """check triggers for sync accounts"""
    # Проверяем, что все ценные бумаги разблокированы
    all_securities_unblocked = (
        position is not None and
        position.account_id == src_account and
        len(position.securities) > 0 and
        all(sec.blocked == 0 for sec in position.securities)
    )

    # Проверяем, что нет ценных бумаг и деньги разблокированы
    no_securities_and_money_unblocked = (
        position is not None and
        position.account_id == dst_account and
        len(position.securities) == 0 and
        len(position.money) > 0 and
        position.money[0].blocked_value.units == 0 and
        position.money[0].blocked_value.nano == 0
    )

    return all_securities_unblocked or no_securities_and_money_unblocked


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

    def postiton_to_string(self, position):
        """convert position to human-readable string"""
        if position.instrument_type == 'currency':
            return currency_to_string(position)
        if position.instrument_type in ['share', 'etf']:
            instrument = self.get_instrument(position.instrument_uid)
            quantity = format_decimal(get_quantity_position(position))
            return (no_money_to_string(instrument) + ' - ' +
                    quantity + ' - ' + currency_to_string(position))
        return str(position)

    def get_instrument(self, instrument_id):
        """get instrument by instrument id"""
        result = self.client.instruments.find_instrument(
            query=instrument_id).instruments
        if len(result) == 1:
            return result[0]
        raise GetInstrumentException('error get instrument')

    def print_portfolio_by_account(self, account):
        """print detailed information about account"""
        logging.log(IMPORTANT, '%s (%s)', account.name, account.id)
        logging.log(IMPORTANT, '------------')
        portfolio = self.client.operations.get_portfolio(account_id=account.id)
        total = Decimal('0')
        for position in portfolio.positions:
            logging.log(IMPORTANT, self.postiton_to_string(position))
            total += currency_to_decimal(position)
        logging.log(IMPORTANT, 'total: %s', str(total))
        logging.log(IMPORTANT, '============')

    def print_all_portfolio(self):
        """print detailed information about all accounts"""
        response = self.client.users.get_accounts()
        for account in response.accounts:
            self.print_portfolio_by_account(account)

    def calc_ratio(self, src_account_id, dst_account_id):
        """calc ratio and print src and dst accounts"""
        logging.log(IMPORTANT, "src account")
        portfolio_src = self.client.operations.get_portfolio(
            account_id=src_account_id)
        total_src = Decimal('0')
        src_positions = {}
        for position in portfolio_src.positions:
            logging.log(IMPORTANT, self.postiton_to_string(position))
            if position.instrument_type != 'currency':
                src_positions[position.instrument_uid] = position
                total_src += currency_to_decimal(position)
        logging.log(IMPORTANT, 'total: %s', str(total_src))

        logging.log(IMPORTANT, "dst account")
        portfolio_dst = self.client.operations.get_portfolio(
            account_id=dst_account_id)
        total_dst = Decimal('0')
        dst_positions = {}
        for position in portfolio_dst.positions:
            logging.log(IMPORTANT, self.postiton_to_string(position))
            if position.instrument_type != 'currency':
                dst_positions[position.instrument_uid] = position
            total_dst += currency_to_decimal(position)
        total_dst = total_dst * (Decimal('1') - self.reserve)
        logging.log(IMPORTANT, 'total: %s', str(total_dst))

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
                    logging.log(IMPORTANT,
                                'Продать: %s %d лотов',
                                no_money_to_string(instrument),
                                quantity)
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
                    logging.log(IMPORTANT,
                                'Продать: %s %d лотов',
                                no_money_to_string(instrument),
                                quantity)
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
                    logging.log(IMPORTANT,
                                'Купить: %s %d лотов',
                                no_money_to_string(instrument),
                                quantity)
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
                    logging.log(IMPORTANT,
                                'Купить: %s %d лотов',
                                no_money_to_string(instrument),
                                quantity)
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
            logging.log(IMPORTANT, order_params)
            logging.log(IMPORTANT,
                        self.client.orders.post_order(
                            instrument_id=order_params.instrument_id,
                            quantity=order_params.quantity,
                            direction=order_params.direction,
                            account_id=dst_account_id,
                            order_type=order_params.order_type).order_id)
        for order_params in orders_params_buy:
            logging.log(IMPORTANT, order_params)
            logging.log(IMPORTANT,
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
            logging.error(err)

        while True:
            try:
                for response in self.client.operations_stream.positions_stream(
                        accounts=[src, dst]):
                    if check_triggers(response.position, src, dst):
                        self.sync_accounts(src, dst)
                    else:
                        logging.log(IMPORTANT, response)
            except RequestError as err:
                logging.error(err)


@dataclasses.dataclass
class RunnerParams:
    """params for init Runner class"""
    debug: bool
    threshold: float
    reserve: float


class Runner:
    """wrapper for launch autorwpeater"""

    def __init__(self,
                 token,
                 src,
                 dst,
                 params=RunnerParams(debug=False,
                                     threshold=None,
                                     reserve=None)):
        self.token = token
        self.params = params
        self.src = src
        self.dst = dst
        logging.addLevelName(IMPORTANT, 'IMPORTANT')
        logging.getLogger().setLevel(IMPORTANT)

    def run(self):
        """run mainflow for server variant"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = AutoRepeater(client)
            autorepeater.print_all_portfolio()
            autorepeater.set_debug(self.params.debug)
            autorepeater.set_threshold(self.params.threshold)
            autorepeater.set_reserve(self.params.reserve)
            if self.src and self.dst:
                autorepeater.mainflow(self.src, self.dst)

    def run_sync(self):
        """run one sync for serverless varian"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = AutoRepeater(client)
            autorepeater.set_debug(self.params.debug)
            autorepeater.set_threshold(self.params.threshold)
            autorepeater.set_reserve(self.params.reserve)
            if self.src and self.dst:
                autorepeater.sync_accounts(self.src, self.dst)
