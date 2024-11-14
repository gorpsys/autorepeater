"""A robot for automatically repeating operations of one account over another account"""
import os
import argparse
import dataclasses

from tinkoff.invest import Client
from tinkoff.invest.constants import INVEST_GRPC_API
from tinkoff.invest import InstrumentIdType
from tinkoff.invest import OrderDirection
from tinkoff.invest import OrderType
from tinkoff.invest import SecurityTradingStatus
from tinkoff.invest import RequestError

DST_MONEY_RESERVED = 0.005
THRESHOLD = 0.001

class GetInstrumentException(Exception):
    """instrument not found by instrument_id"""

def money_to_string(money):
    """convert money to human-readable string"""
    result = money.currency
    result += ' - ' + str(money.units + money.nano / 1000000000.)
    return result


def blocked_to_string(blocked):
    """convert blocked money to human-readable string"""
    result = 'blocked ' + money_to_string(blocked)
    return result


def no_money_to_string(share):
    """convert no money instrument to human-readable string"""
    result = share.name + '(' + share.ticker + ')'
    return result


def currency_to_float(position):
    """convert position full price value to float"""
    result = (position.current_price.units + position.current_price.nano /
              1000000000.) * (position.quantity.units +
                              position.quantity.nano / 1000000000.)
    return result


def currency_to_float_price(position):
    """convert position price value to float"""
    result = (position.current_price.units +
              position.current_price.nano / 1000000000.)
    return result


def currency_to_string(position):
    """convert position price value to human-readable string"""
    result = position.current_price.currency
    result += ' - ' + str(currency_to_float(position))
    return result


def get_quantity_position(position):
    """get quantity from position"""
    return position.quantity.units + position.quantity.nano / 1000000000.


def check_triggers(position, src_account, dst_account):
    """check triggers for sync accounts"""
    return (position is not None and position.account_id == src_account
            and len(position.securities) > 0 and position.securities[0].blocked
            == 0) or (position is not None and position.account_id
                      == dst_account and len(position.securities) == 0
                      and position.money[0].blocked_value.units == 0
                      and position.money[0].blocked_value.nano == 0)


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
        total_sell += currency_to_float_price(position) * order_params.quantity

    total_buy = 0
    for order_params in buy_orders_params:
        position = src_positions[order_params.instrument_id]
        total_buy += currency_to_float_price(position) * order_params.quantity

    return max(total_sell, total_buy)

class AutoRepeater:
    """Main class for automatically repeating operations of one account over another account."""

    def __init__(self, client):
        self.client = client
        self.debug = False
        self.threshold = THRESHOLD
        self.reserve = DST_MONEY_RESERVED

    def set_debug(self, debug):
        """set debug flag"""
        self.debug = debug

    def set_threshold(self, threshold):
        """set threshod"""
        if threshold is not None:
            self.threshold = threshold

    def set_reserve(self, reserve):
        """set reserve"""
        if reserve is not None:
            self.reserve = reserve

    def postiton_to_string(self, position):
        """convert position to human-readable string"""
        if position.instrument_type == 'currency':
            return currency_to_string(position)
        if position.instrument_type in ['share', 'etf']:
            instrument = self.get_instrument(position.instrument_uid)
            return no_money_to_string(instrument) + ' - ' + str(
                get_quantity_position(position)) + ' - ' + currency_to_string(
                    position)
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
        print(account.name + ' (' + str(account.id) + ')')
        print('------------')
        portfolio = self.client.operations.get_portfolio(account_id=account.id)
        total = 0.0
        for position in portfolio.positions:
            print(self.postiton_to_string(position))
            total += currency_to_float(position)
        print('total: ' + str(total))
        print('============')

    def print_all_portfolio(self):
        """print detailed information about all accounts"""
        response = self.client.users.get_accounts()
        for account in response.accounts:
            self.print_portfolio_by_account(account)

    def calc_ratio(self, src_account_id, dst_account_id):
        """calc ratio and print src and dst accounts"""
        print("src account")
        portfolio_src = self.client.operations.get_portfolio(
            account_id=src_account_id)
        total_src = 0.0
        src_positions = {}
        for position in portfolio_src.positions:
            print(self.postiton_to_string(position))
            if position.instrument_type != 'currency':
                src_positions[position.instrument_uid] = position
                total_src += currency_to_float(position)
        print('total: ' + str(total_src))

        print("dst account")
        portfolio_dst = self.client.operations.get_portfolio(
            account_id=dst_account_id)
        total_dst = 0.0
        dst_positions = {}
        for position in portfolio_dst.positions:
            print(self.postiton_to_string(position))
            if position.instrument_type != 'currency':
                dst_positions[position.instrument_uid] = position
            total_dst += currency_to_float(position)
        total_dst = total_dst * (1 - self.reserve)
        print('total: ' + str(total_dst))

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
                quantity = round(
                    get_quantity_position(item_value) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_SELL,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
            elif target_positions[item_id] < get_quantity_position(item_value):
                quantity = round((get_quantity_position(item_value) -
                                  target_positions[item_id]) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
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
                quantity = round(item_value / instrument.lot)
                if quantity > 0:
                    print('Купить: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    result.append(
                        OrderParams(
                            instrument_id=item_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_BUY,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE))
            elif item_value > get_quantity_position(dst_positions[item_id]):
                position = dst_positions[item_id]
                quantity = round(
                    (item_value - get_quantity_position(position)) /
                    instrument.lot)
                if quantity > 0:
                    print('Купить: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
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
            print(order_params)
            print(
                self.client.orders.post_order(
                    instrument_id=order_params.instrument_id,
                    quantity=order_params.quantity,
                    direction=order_params.direction,
                    account_id=dst_account_id,
                    order_type=order_params.order_type).order_id)
        for order_params in orders_params_buy:
            print(order_params)
            print(
                self.client.orders.post_order(
                    instrument_id=order_params.instrument_id,
                    quantity=order_params.quantity,
                    direction=order_params.direction,
                    account_id=dst_account_id,
                    order_type=order_params.order_type).order_id)

    def sync_accounts(self, src_account_id, dst_account_id):
        """sync positions from src account to dst account"""
        (src_positions, dst_positions, ratio,
         total_dst) = self.calc_ratio(src_account_id, dst_account_id)
        target_positions = {}

        for item_id, item_value in src_positions.items():
            target_positions[item_id] = ratio * \
                get_quantity_position(item_value)

        orders_params_sell = self.calc_sell_positions(dst_positions,
                                                      target_positions)
        orders_params_buy = self.calc_buy_positions(src_positions,
                                                    dst_positions,
                                                    target_positions)

        if (not self.debug) and (get_max_sum_positions_price(
                orders_params_sell, orders_params_buy, src_positions,
                dst_positions) > total_dst * self.threshold):
            self.post_orders(dst_account_id, orders_params_sell,
                             orders_params_buy)

    def mainflow(self, src, dst):
        """sync accounts when changing"""
        try:
            self.sync_accounts(src, dst)
        except RequestError as err:
            print(err)

        while True:
            try:
                for response in self.client.operations_stream.positions_stream(
                        accounts=[src, dst]):
                    if check_triggers(response.position, src, dst):
                        self.sync_accounts(src, dst)
            except RequestError as err:
                print(err)


def main():
    """main function"""
    parser = argparse.ArgumentParser(description="autorepeater")

    parser.add_argument("--debug", action='store_true', help="режим отладки")
    parser.add_argument("-s", "--src", type=str, help="id счёта источника")
    parser.add_argument("-d", "--dst", type=str, help="id счёта назначения")
    parser.add_argument("-t", "--threshold", type=float, help="порог стоимости, ниже "
                        "которого не выполняется синхронизация - доля стоимости счёта"
                        " назначения. По умолчанию 0.001")
    parser.add_argument("-r", "--reserve", type=float, help="резев на счёте назначения"
                        " для округлений и комиссий. Доля стоимости счёта назначения. "
                        "По умолчания 0.005")
    args = parser.parse_args()

    invest_token = os.environ["INVEST_TOKEN"]

    with Client(token=invest_token, target=INVEST_GRPC_API) as client:
        autorepeater = AutoRepeater(client)
        autorepeater.print_all_portfolio()
        autorepeater.set_debug(args.debug)
        autorepeater.set_threshold(args.threshold)
        autorepeater.set_reserve(args.reserve)
        if args.src and args.dst:
            autorepeater.mainflow(args.src, args.dst)


if __name__ == "__main__":
    main()
