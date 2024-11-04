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

class AutoRepeater:
    """Main class for automatically repeating operations of one account over another account."""

    def __init__(self, client):
        self.client = client

    def money_to_string(self, money):
        """convert money to human-readable string"""
        result = money.currency
        result += ' - ' + str(money.units + money.nano / 1000000000.)
        return result

    def blocked_to_string(self, blocked):
        """convert blocked money to human-readable string"""
        result = 'blocked ' + self.money_to_string(blocked)
        return result

    def no_money_to_string(self, share):
        """convert no money instrument to human-readable string"""
        result = share.name + '(' + share.ticker + ')'
        return result

    def currency_to_float(self, position):
        """convert position price value to float"""
        result = (position.current_price.units + position.current_price.nano /
                  1000000000.) * (position.quantity.units +
                                  position.quantity.nano / 1000000000.)
        return result

    def currency_to_string(self, position):
        """convert position price value to human-readable string"""
        result = position.current_price.currency
        result += ' - ' + str(self.currency_to_float(position))
        return result

    def get_quantity_position(self, position):
        """get quantity from position"""
        return position.quantity.units + position.quantity.nano / 1000000000.

    def postiton_to_string(self, position):
        if position.instrument_type == 'currency':
            return self.currency_to_string(position)
        elif position.instrument_type == 'share' or position.instrument_type == 'etf':
            instrument = self.get_instrument(position.instrument_uid)
            return self.no_money_to_string(instrument) + ' - ' + str(
                self.get_quantity_position(
                    position)) + ' - ' + self.currency_to_string(position)
        else:
            return str(position)

    def get_instrument(self, id):
        result = self.client.instruments.find_instrument(query=id).instruments
        if len(result) == 1:
            return result[0]
        raise Exception('error get instrument')

    def print_portfolio_by_account(self, account):
        print(account.name + ' (' + str(account.id) + ')')
        print('------------')
        portfolio = self.client.operations.get_portfolio(account_id=account.id)
        total = 0.0
        for position in portfolio.positions:
            print(self.postiton_to_string(position))
            total += self.currency_to_float(position)
        print('total: ' + str(total))
        print('============')

    def print_all_portfolio(self):
        response = self.client.users.get_accounts()
        for account in response.accounts:
            self.print_portfolio_by_account(account)

    def sync_accounts(self, src_account_id, dst_account_id):
        print("src account")
        portfolio_src = self.client.operations.get_portfolio(
            account_id=src_account_id)
        total_src = 0.0
        src_positions = {}
        for position in portfolio_src.positions:
            print(self.postiton_to_string(position))
            if position.instrument_type != 'currency':
                src_positions[position.instrument_uid] = position
                total_src += self.currency_to_float(position)
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
            total_dst += self.currency_to_float(position)
        total_dst = total_dst * (1 - DST_MONEY_RESERVED)
        print('total: ' + str(total_dst))

        ratio = total_dst / total_src
        target_positions = {}

        for instrument_id in src_positions.keys():
            position = src_positions[instrument_id]
            target_positions[instrument_id] = ratio * \
                self.get_quantity_position(position)

        for instrument_id in dst_positions.keys():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=instrument_id).instrument
            position = dst_positions[instrument_id]
            if instrument_id not in target_positions.keys():
                quantity = round(
                    self.get_quantity_position(position) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + self.no_money_to_string(instrument) +
                          ' ' + str(quantity) + ' лотов')
                    print(
                        self.client.orders.post_order(
                            instrument_id=instrument_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_SELL,
                            account_id=dst_account_id,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                    )
            elif target_positions[instrument_id] < self.get_quantity_position(
                    position):
                quantity = round(
                    (self.get_quantity_position(position) -
                     target_positions[instrument_id]) / instrument.lot)
                if quantity > 0:
                    print('Продать: ' + self.no_money_to_string(instrument) +
                          ' ' + str(quantity) + ' лотов')
                    print(
                        self.client.orders.post_order(
                            instrument_id=instrument_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_SELL,
                            account_id=dst_account_id,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                    )

        for instrument_id in target_positions.keys():
            instrument = self.client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=instrument_id).instrument
            if instrument_id not in dst_positions.keys():
                position = src_positions[instrument_id]
                quantity = round(target_positions[instrument_id] /
                                 instrument.lot)
                if quantity > 0:
                    print('Купить: ' + self.no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    print(
                        self.client.orders.post_order(
                            instrument_id=instrument_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_BUY,
                            account_id=dst_account_id,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                    )
            elif target_positions[instrument_id] > self.get_quantity_position(
                    dst_positions[instrument_id]):
                position = dst_positions[instrument_id]
                quantity = round(
                    (target_positions[instrument_id] -
                     self.get_quantity_position(position)) / instrument.lot)
                if quantity > 0:
                    print('Купить: ' + self.no_money_to_string(instrument) + ' ' +
                          str(quantity) + ' лотов')
                    print(
                        self.client.orders.post_order(
                            instrument_id=instrument_id,
                            quantity=quantity,
                            direction=OrderDirection.ORDER_DIRECTION_BUY,
                            account_id=dst_account_id,
                            order_type=OrderType.ORDER_TYPE_BESTPRICE).order_id
                    )

    def mainflow(self, src, dst):

        self.sync_accounts(src, dst)

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
                self.sync_accounts(src, dst)


def main():
    parser = argparse.ArgumentParser(description="autorepeater")

    with Client(token=TOKEN, target=INVEST_GRPC_API) as client:
        autorepeater = AutoRepeater(client)
        autorepeater.print_all_portfolio()
        parser.add_argument("src", help="id счёта источника")
        parser.add_argument("dst", help="id счёта назначения")
        args = parser.parse_args()
        autorepeater.mainflow(args.src, args.dst)


if __name__ == "__main__":
    main()
