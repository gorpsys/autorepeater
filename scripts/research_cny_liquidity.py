"""Read-only sandbox market research; credentials never enter output or artifacts."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

HOSTS = ('sandbox-invest-public-api.tbank.ru', 'sandbox-invest-public-api.tinkoff.ru')
CURRENT_ISIN = 'RU000A105104'


class NoRedirect(HTTPRedirectHandler):
    """Keep the sandbox credential on the explicitly selected verified host."""
    def redirect_request(self, *_args, **_kwargs):
        return None


class ApiError(Exception):
    """Fixed transport categories, without response bodies or credentials."""


class Api:  # pylint: disable=too-few-public-methods
    """Bounded read-only REST calls, throttled to at most one request per second."""
    def __init__(self, host):
        self.host = host
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(
            context=ssl.create_default_context()), NoRedirect())
        self.last_request = 0

    def call(self, service, method, body):
        """Read one public market-data resource with strict TLS and no redirects."""
        time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        url = f'https://{self.host}/rest/tinkoff.public.invest.api.contract.v1.{service}/{method}'
        request = Request(url, data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + os.environ['SANDBOX_TOKEN'],
            'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ApiError('response_too_large')
                return json.loads(data)
        except HTTPError as error:
            error.close()
            raise ApiError('http_' + str(error.code)) from None
        except URLError as error:
            reason = error.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ApiError('tls_certificate_' + str(reason.verify_code)) from None
            raise ApiError(type(reason).__name__) from None


def select_api():
    """Select a verified sandbox endpoint without falling back to production."""
    for host in HOSTS:
        api = Api(host)
        try:
            result = api.call('InstrumentsService', 'Bonds', {
                'instrumentStatus': 'INSTRUMENT_STATUS_BASE'})
            if not isinstance(result.get('instruments'), list):
                raise ApiError('invalid_bond_response')
            print(json.dumps({'host': host, 'result': 'connected',
                              'bond_count': len(result.get('instruments', []))}), flush=True)
            return api, result
        except (ApiError, TimeoutError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, ApiError) else type(error).__name__
            print(json.dumps({'host': host, 'result': 'failed', 'reason': reason}), flush=True)
    raise ApiError('no_verified_sandbox_endpoint')


def inventory(api, bonds):
    """Keep CNY settlement papers and CNY currency-pair controls only."""
    fields = ('uid', 'figi', 'isin', 'ticker', 'classCode', 'name', 'currency', 'lot',
              'nominal', 'minPriceIncrement', 'exchange', 'realExchange',
              'apiTradeAvailableFlag', 'buyAvailableFlag', 'sellAvailableFlag',
              'maturityDate', 'aciValue', 'floatingCouponFlag', 'amortizationFlag',
              'liquidityFlag', 'forQualInvestorFlag')
    found = []
    for method, result in [('Bonds', bonds), ('Shares', None), ('Etfs', None),
                           ('Currencies', None)]:
        if result is None:
            result = api.call('InstrumentsService', method, {
                'instrumentStatus': 'INSTRUMENT_STATUS_BASE'})
        instruments = result.get('instruments', [])
        selected = [item for item in instruments if item.get('currency', '').lower() == 'cny'
                    or (method == 'Currencies'
                        and item.get('isoCurrencyName', '').lower() == 'cny')]
        print(json.dumps({'method': method, 'total': len(instruments),
                          'cny_count': len(selected)}), flush=True)
        for item in selected:
            found.append({'kind': method, **{key: item[key] for key in fields if key in item}})
    return found


def amount(value):
    """Preserve exact units/nano in all price and nominal calculations."""
    return Decimal(str(value.get('units', '0'))) + Decimal(str(value.get('nano', 0))) / 10**9


def book_fact(api, item):
    """Observe five actual levels and server time; never substitute a local timestamp."""
    now = datetime.now(timezone.utc)
    try:
        book = api.call('MarketDataService', 'GetOrderBook', {
            'instrumentId': item['uid'], 'depth': 5})
        if book.get('instrumentUid') != item['uid']:
            raise ApiError('book_uid_mismatch')
        received = datetime.now(timezone.utc)
        timestamp = book.get('orderbookTs')
        stamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')) if timestamp else None
        bids, asks = book.get('bids', []), book.get('asks', [])
        best_bid = amount(bids[0]['price']) if bids else None
        best_ask = amount(asks[0]['price']) if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
        return {'observed_utc': received.isoformat(), 'uid': item['uid'],
                'ticker': item.get('ticker'), 'isin': item.get('isin'),
                'market_time': timestamp, 'age_seconds': (received - stamp).total_seconds()
                if stamp is not None else None,
                'epoch_time': stamp.year == 1970 if stamp is not None else None,
                'request_seconds': (received - now).total_seconds(),
                'bid_levels': len(bids), 'ask_levels': len(asks),
                'bid_quantity': sum(int(level['quantity']) for level in bids),
                'ask_quantity': sum(int(level['quantity']) for level in asks),
                'best_bid': str(best_bid) if best_bid is not None else None,
                'best_ask': str(best_ask) if best_ask is not None else None,
                'book_signature': sha256(json.dumps(
                    {'bids': bids, 'asks': asks}, sort_keys=True).encode()).hexdigest(),
                'spread_bps': str((best_ask - best_bid) / mid * 10000)
                if mid is not None and mid > 0 else None}
    except (ApiError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
        return {'observed_utc': now.isoformat(), 'uid': item['uid'],
                'ticker': item.get('ticker'),
                'error': str(error) if isinstance(error, ApiError) else type(error).__name__}


def eligible(item):
    """Match the quotation and settlement constraints used by the existing E2E harness."""
    if item['kind'] not in ('Bonds', 'Shares', 'Etfs'):
        return False
    if not all(item.get(key) is True for key in (
            'apiTradeAvailableFlag', 'buyAvailableFlag', 'sellAvailableFlag')):
        return False
    if item.get('currency', '').lower() != 'cny' or int(item.get('lot', 0)) <= 0:
        return False
    return item['kind'] != 'Bonds' or (
        item.get('nominal', {}).get('currency', '').lower() == 'cny'
        and amount(item['nominal']) > 0)


def screen(api, instruments):
    """Read each eligible book and permission state without creating an account."""
    rows = []
    for item in instruments:
        if (not eligible(item) and item.get('isin') != CURRENT_ISIN
                and item.get('ticker') != 'CNYRUB_TOM'):
            continue
        fact = book_fact(api, item)
        try:
            status = api.call('MarketDataService', 'GetTradingStatus', {
                'instrumentId': item['uid']})
            if status.get('instrumentUid') != item['uid']:
                raise ApiError('status_uid_mismatch')
            fact['status'] = {key: status.get(key) for key in (
                'instrumentUid', 'tradingStatus', 'apiTradeAvailableFlag',
                'bestpriceOrderAvailableFlag', 'marketOrderAvailableFlag',
                'limitOrderAvailableFlag')}
        except (ApiError, TimeoutError, OSError, ValueError) as error:
            fact['status_error'] = (str(error) if isinstance(error, ApiError)
                                    else type(error).__name__)
        rows.append({'instrument': item, **fact})
        print(json.dumps({'screened': item.get('ticker'),
                          'age_seconds': fact.get('age_seconds'),
                          'bid_levels': fact.get('bid_levels'),
                          'ask_levels': fact.get('ask_levels'),
                          'error': fact.get('error')}), flush=True)
    return rows


def shortlist(rows):
    """Retain current/control instruments and six promising tradable alternatives."""
    controls = [row['instrument'] for row in rows
                if row['instrument'].get('isin') == CURRENT_ISIN
                or row['instrument'].get('ticker') == 'CNYRUB_TOM']
    candidates = [row for row in rows if eligible(row['instrument'])
                  and row['instrument'] not in controls
                  and row.get('bid_levels', 0) and row.get('ask_levels', 0)
                  and row.get('status', {}).get('apiTradeAvailableFlag') is True
                  and row.get('status', {}).get('bestpriceOrderAvailableFlag') is True]
    candidates.sort(key=lambda row: (
        row.get('age_seconds') is None or not 0 <= row['age_seconds'] <= 180,
        row.get('age_seconds') if row.get('age_seconds') is not None else float('inf'),
        Decimal(row['spread_bps']) if row.get('spread_bps') is not None else Decimal('Infinity'),
        -min(row.get('bid_quantity', 0), row.get('ask_quantity', 0))))
    return controls + [row['instrument'] for row in candidates[:6]]


def sample(api, instruments, output, seconds, interval):
    """Observe actual timestamps and depth repeatedly through a complete finite window."""
    start = datetime.now(timezone.utc)
    deadline = time.monotonic() + seconds
    count = 0
    with output.open('w', encoding='utf-8') as stream:
        while time.monotonic() < deadline:
            cycle = time.monotonic()
            for item in instruments:
                stream.write(json.dumps(book_fact(api, item), ensure_ascii=False) + '\n')
                stream.flush()
                count += 1
            print(json.dumps({'sample_count': count,
                              'observed_utc': datetime.now(timezone.utc).isoformat()}), flush=True)
            pause = min(interval - (time.monotonic() - cycle), deadline - time.monotonic())
            time.sleep(max(0, pause))
    end = datetime.now(timezone.utc)
    details = sample_details(api, instruments, start, end)
    details.update(seconds=seconds, interval_seconds=interval, requests=count)
    write_json(output.with_suffix('.details.json'), details)


def sample_details(api, instruments, start, end):
    """Read observed trade counts and formal schedules for the measured window."""
    details = {'start_utc': start.isoformat(), 'end_utc': end.isoformat(),
               'trades': [], 'schedules': []}
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    for item in instruments:
        try:
            trades = api.call('MarketDataService', 'GetLastTrades', {
                'instrumentId': item['uid'], 'from': start.isoformat(), 'to': end.isoformat()})
            values = trades.get('trades', [])
            details['trades'].append({'uid': item['uid'], 'ticker': item.get('ticker'),
                                      'count': len(values),
                                      'quantity': sum(int(x['quantity']) for x in values)})
        except (ApiError, TimeoutError, OSError, ValueError, KeyError) as error:
            details['trades'].append({'uid': item['uid'],
                                      'error': str(error) if isinstance(error, ApiError)
                                      else type(error).__name__})
    for exchange in sorted({item.get('exchange', '') for item in instruments} | {'MOEX'}):
        try:
            schedules = api.call('InstrumentsService', 'TradingSchedules', {
                'exchange': exchange, 'from': day_start,
                'to': end.isoformat()})
            details['schedules'].append({'requested_exchange': exchange, **schedules})
        except (ApiError, TimeoutError, OSError, ValueError) as error:
            details['schedules'].append({
                'requested_exchange': exchange,
                'error': str(error) if isinstance(error, ApiError) else type(error).__name__})
    return details


def write_json(path, value):
    """Persist only explicitly selected public market evidence."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    """Run the requested read-only phase and preserve its public evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('inventory', 'screen', 'sample'), default='inventory')
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=900)
    parser.add_argument('--interval', type=int, default=10)
    args = parser.parse_args()
    api, bonds = select_api()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.phase == 'inventory':
        items = inventory(api, bonds)
        write_json(args.output, {
            'host': api.host, 'observed_utc': datetime.now(timezone.utc).isoformat(),
            'instruments': items})
        print(json.dumps({'inventory_written': str(args.output), 'count': len(items)}), flush=True)
    elif args.phase == 'screen':
        items = json.loads(args.inventory.read_text())['instruments']
        rows = screen(api, items)
        write_json(args.output, {'host': api.host, 'rows': rows})
        selected = shortlist(rows)
        write_json(args.output.with_suffix('.shortlist.json'),
                   {'host': api.host, 'instruments': selected})
        print(json.dumps({'shortlist': [item['ticker'] for item in selected]}), flush=True)
    else:
        items = json.loads(args.inventory.read_text())['instruments']
        sample(api, items, args.output, args.seconds, args.interval)


if __name__ == '__main__':
    try:
        main()
    except (ApiError, TimeoutError, OSError, ValueError) as error:
        print(json.dumps({'fatal': str(error) if isinstance(error, ApiError)
                          else type(error).__name__}), flush=True)
        raise SystemExit(1) from None
