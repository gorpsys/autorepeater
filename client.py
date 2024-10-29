import os

from tinkoff.invest import Client
from tinkoff.invest.constants import INVEST_GRPC_API_SANDBOX
from tinkoff.invest.constants import INVEST_GRPC_API

TOKEN = os.environ["INVEST_TOKEN"]
SRC_ACCOUNT = '2141399550'
DST_ACCOUNT = '2193248994'

class AutoRepeater:

    def __init__(self, client):
            self.client = client
            
    def moneyToString(self, money):
        result = money.currency
        result += ' - '+str(money.units+money.nano/1000000000.)
        return result

    def blockedToString(self, blocked):
        result = 'blocked ' + self.moneyToString(blocked)
        return result

    def shareToString(self, share):
        result = share.name+'('+share.ticker+')'
        return result

    def currencyToFloat(self, position):
        result = (position.current_price.units+position.current_price.nano/1000000000.)*(position.quantity.units+position.quantity.nano/1000000000.)
        return result

    def currencyToString(self, position):
        result = position.current_price.currency
        result += ' - ' + str(self.currencyToFloat(position))
        return result

    def postitonToString(self, position):
        if position.instrument_type == 'currency':
            return self.currencyToString(position)
        elif position.instrument_type == 'share' or position.instrument_type == 'etf':
            instrument = self.get_instrument(position.instrument_uid)
            return self.shareToString(instrument)+' - '+ str(position.quantity.units+position.quantity.nano/1000000000.) +' - '+self.currencyToString(position)
        else:
            return str(position)

    def get_instrument(self, id):
        result = self.client.instruments.find_instrument(query=id).instruments
        if len(result)==1:
            return result[0]
        raise Exception('error get instrument')
    
    def mainflow(self):
        response = self.client.users.get_accounts()
        for account in response.accounts:
            print(account.name +' (' +str(account.id)+')')
            print('------------')
            portfolio = self.client.operations.get_portfolio(account_id=account.id)
            total = 0.0
            for position in portfolio.positions:
                print(self.postitonToString(position))
                total += self.currencyToFloat(position)
            print('total: '+str(total))
            print('============')

        for response in self.client.operations_stream.positions_stream(accounts=[SRC_ACCOUNT]):
#            if response.position is not None and len(response.position.securities)>0 and response.position.securities[0].blocked==0:
                print("src account")
                portfolio_src = self.client.operations.get_portfolio(account_id=SRC_ACCOUNT)
                total_src = 0.0
                src_positions = {}
                for position in portfolio_src.positions:
                    print(self.postitonToString(position))
                    if position.instrument_type != 'currency':
                        src_positions[position.instrument_uid] = position.quantity.units+position.quantity.nano/1000000000.
                    total_src += self.currencyToFloat(position)
                print('total: '+str(total_src))

                print("dst account")
                portfolio_dst = self.client.operations.get_portfolio(account_id=DST_ACCOUNT)
                total_dst = 0.0
                dst_positions = {}
                for position in portfolio_dst.positions:
                    print(self.postitonToString(position))
                    if position.instrument_type != 'currency':
                        dst_positions[position.instrument_uid] = position.quantity.units+position.quantity.nano/1000000000.
                    total_dst += self.currencyToFloat(position)
                print('total: '+str(total_dst))

                ratio = total_dst/total_src
                target_positions = {}

                for instrument_id in src_positions.keys():
                    target_positions[instrument_id] = ratio*src_positions[instrument_id]

                for instrument_id in dst_positions.keys():
                    instrument = self.get_instrument(instrument_id)
                    if instrument_id not in target_positions.keys():
                        print('Продать: ' + self.shareToString(instrument) + str(dst_positions[instrument_id]))
                    elif target_positions[instrument_id]<dst_positions[instrument_id]:
                        print('Продать: ' + self.shareToString(instrument) + str(dst_positions[instrument_id]-target_positions[instrument_id]))

                for instrument_id in target_positions.keys():
                    instrument = self.get_instrument(instrument_id)
                    if instrument_id not in dst_positions.keys():
                        print('Купить: ' + self.shareToString(instrument) + str(target_positions[instrument_id]))
                    elif target_positions[instrument_id]>dst_positions[instrument_id]:
                        print('Купить: ' + self.shareToString(instrument) + str(target_positions[instrument_id]-dst_positions[instrument_id]))


def main():
    with Client(token=TOKEN, target=INVEST_GRPC_API) as client:
        autorepeater = AutoRepeater(client)
        autorepeater.mainflow()

if __name__ == "__main__":
    main()
