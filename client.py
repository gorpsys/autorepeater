"""A robot for automatically repeating operations of one account over another account"""
import os
import argparse

from tinkoff.invest import Client
from tinkoff.invest.constants import INVEST_GRPC_API
from tinkoff.invest import InstrumentIdType
from tinkoff.invest import OrderDirection
from tinkoff.invest import OrderType

TOKEN = os.environ["INVEST_TOKEN"]
DST_MONEY_RESERVED = 0.005


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
    """convert position price value to float"""
    result = (position.current_price.units + position.current_price.nano /
              1000000000.) * (position.quantity.units +
                              position.quantity.nano / 1000000000.)
    return result


def currency_to_string(position):
    """convert position price value to human-readable string"""
    result = position.current_price.currency
    result += ' - ' + str(currency_to_float(position))
    return result


def get_quantity_position(position):
    """get quantity from position"""
    return position.quantity.units + position.quantity.nano / 1000000000.


class AutoRepeater:
    """Main class for automatically repeating operations of one account over another account."""

    def __init__(self, client):
        self.client = client

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
        result = self.client.instruments.find_instrument(query=instrument_id).instruments
        if len(result) == 1:
            return result[0]
        raise Exception('error get instrument')

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

    def sync_accounts(self, src_account_id, dst_account_id, debug):
        """sync positions from src account to dst account"""
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
        total_dst = total_dst * (1 - DST_MONEY_RESERVED)
        print('total: ' + str(total_dst))

        ratio = total_dst / total_src
        target_positions = {}

        for item_id, item_value in src_positions.items():
            target_positions[item_id] = ratio * \
                get_quantity_position(item_value)

        for item_id, item_value in dst_positions.items():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=item_id).instrument
            if item_id not in target_positions:
                quantity = round(
                    get_quantity_position(item_value) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    if not debug:
                        print(
                            self.client.orders.post_order(
                                instrument_id=item_id,
                                quantity=quantity,
                                direction=OrderDirection.ORDER_DIRECTION_SELL,
                                account_id=dst_account_id,
                                order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                        )
            elif target_positions[item_id] < get_quantity_position(
                    item_value):
                quantity = round(
                    (get_quantity_position(item_value) -
                     target_positions[item_id]) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    if not debug:
                        print(
                            self.client.orders.post_order(
                                instrument_id=item_id,
                                quantity=quantity,
                                direction=OrderDirection.ORDER_DIRECTION_SELL,
                                account_id=dst_account_id,
                                order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                        )

        for item_id, item_value in target_positions.items():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=item_id).instrument
            if item_id not in dst_positions:
                position = src_positions[item_id]
                quantity = round(item_value /
                                 instrument.lot)
                if quantity > 0:
                    print('Купить: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    if not debug:
                        print(
                            self.client.orders.post_order(
                                instrument_id=item_id,
                                quantity=quantity,
                                direction=OrderDirection.ORDER_DIRECTION_BUY,
                                account_id=dst_account_id,
                                order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                        )
            elif item_value > get_quantity_position(
                    dst_positions[item_id]):
                position = dst_positions[item_id]
                quantity = round(
                    (item_value -
                     get_quantity_position(position)) / instrument.lot)
                if quantity > 0:
                    print('Купить: ' + no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    if not debug:
                        print(
                            self.client.orders.post_order(
                                instrument_id=item_id,
                                quantity=quantity,
                                direction=OrderDirection.ORDER_DIRECTION_BUY,
                                account_id=dst_account_id,
                                order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                        )

    def mainflow(self, src, dst, debug):
        """sync accounts when changing"""
        self.sync_accounts(src, dst, debug)

        for response in self.client.operations_stream.positions_stream(
                accounts=[src, dst]):
            if (response.position is not None and response.position.account_id
                    == src and len(response.position.securities) > 0
                    and response.position.securities[0].blocked == 0
                ) or (response.position is not None
                      and response.position.account_id == dst
                      and len(response.position.securities) == 0
                      and response.position.money[0].blocked_value.units == 0
                      and response.position.money[0].blocked_value.nano == 0):
                self.sync_accounts(src, dst, debug)


def main():
    """main function"""
    parser = argparse.ArgumentParser(description="autorepeater")

    with Client(token=TOKEN, target=INVEST_GRPC_API) as client:
        autorepeater = AutoRepeater(client)
        autorepeater.print_all_portfolio()
        parser.add_argument("--debug", type=bool, help="режим отладки")
        parser.add_argument("src", help="id счёта источника")
        parser.add_argument("dst", help="id счёта назначения")
        args = parser.parse_args()
        autorepeater.mainflow(args.src, args.dst, args.debug)


if __name__ == "__main__":
    main()
