"""Explicit-only sandbox experiment; excluded from normal test_* collection."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import grpc
import pytest
from t_tech.invest import MoneyValue, OrderDirection, OrderType, RequestError
from t_tech.invest.grpc import operations_pb2

from autorepeater.strategy_data import InstrumentType
from scripts.sandbox_e2e import namespace, run_bounded
from scripts.sandbox_lifecycle import SandboxFailure, list_exception_facts, safe_call
from scripts.sandbox_support import FILL, PENDING, SandboxSession

USD_TICKERS = ('AAPL', 'MSFT')
REQUEST_CURRENCIES = ('DEFAULT', 'RUB', 'USD', 'EUR')


def quantity(value):
    """Read units/nano without exposing the surrounding SDK object."""
    units, nano = value.units, value.nano
    if any(isinstance(part, bool) or not isinstance(part, int) for part in (units, nano)):
        raise SandboxFailure('invalid portfolio number')
    if not -1000000000 < nano < 1000000000:
        raise SandboxFailure('invalid portfolio nano')
    return Decimal(units) + Decimal(nano) / Decimal(1000000000)


def snapshot_facts(response, phase, requested):
    """Allowlisted observations retain duplicate rows; never dump a raw response."""
    positions = []
    currency_positions = []
    quantities = {}
    for position in response.positions:
        currency = position.current_price.currency
        if not isinstance(currency, str) or re.fullmatch('[a-z]{3}', currency) is None:
            raise SandboxFailure('invalid portfolio currency')
        amount = quantity(position.quantity)
        price = quantity(position.current_price)
        if position.instrument_type == 'currency':
            currency_positions.append({'quantity': format(amount, 'f'),
                                       'price': format(price, 'f'), 'price_currency': currency})
            continue
        uid = position.instrument_uid
        if not isinstance(uid, str) or re.fullmatch('[A-Za-z0-9_-]{1,80}', uid) is None:
            raise SandboxFailure('invalid portfolio instrument UID')
        positions.append({'uid': uid, 'quantity': format(amount, 'f'),
                          'price': format(price, 'f'), 'price_currency': currency})
        quantities[uid] = quantities.get(uid, Decimal(0)) + amount
    counts = Counter(item['uid'] for item in positions)
    totals = {}
    for field in ('total_amount_portfolio', 'total_amount_shares', 'total_amount_currencies'):
        value = getattr(response, field, None)
        if value is not None and isinstance(value.currency, str) and value.currency:
            if re.fullmatch('[a-z]{3}', value.currency) is None:
                raise SandboxFailure('invalid portfolio total currency')
            totals[field] = {'currency': value.currency, 'amount': format(quantity(value), 'f')}
    return {'phase': phase, 'request_currency': requested, 'positions': positions,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'currency_positions': currency_positions, 'totals': totals,
            'quantity_by_uid': {uid: format(value, 'f') for uid, value in quantities.items()},
            'duplicate_uids': sorted(uid for uid, count in counts.items() if count > 1),
            'price_currencies': sorted({item['price_currency'] for item in positions})}


def price_behavior(records, native_currencies):
    """Classify observed currencies, without presuming either API interpretation."""
    facts = [(record['request_currency'], item['price_currency'], native_currencies[item['uid']])
             for record in records for item in record['positions']
             if item['uid'] in native_currencies and record['request_currency'] != 'DEFAULT']
    if not facts or {mode for mode, _, _ in facts} != {'RUB', 'USD', 'EUR'}:
        return 'inconclusive'
    if all(currency == native for _, currency, native in facts):
        return 'instrument_currency'
    if all(currency == mode.lower() for mode, currency, _ in facts):
        return 'request_currency'
    return 'mixed_or_other'


def resolve_instrument(data, ticker, currency, report):
    """Exact share/ETF ticker, requested currency and real API permission only."""
    for match in safe_call(data.find_instruments, query=ticker):
        if match.ticker != ticker or match.instrument_type not in (
                InstrumentType.SHARE, InstrumentType.ETF):
            continue
        instrument = safe_call(data.get_instrument, uid=match.uid)
        report['candidates'].append({
            'ticker': ticker, 'uid': instrument.uid, 'currency': instrument.currency,
            'lot': instrument.lot, 'api_trade_available': instrument.api_trade_available})
        if (instrument.currency == currency and instrument.api_trade_available is True
                and isinstance(instrument.lot, int) and not isinstance(instrument.lot, bool)
                and instrument.lot > 0):
            return instrument
    raise SandboxFailure(f'no API-available {currency} instrument for {ticker}')


def read_portfolio(client, account_id, requested):
    """SDK 1.51 wrapper omits currency; call the same sandbox-channel RPC explicitly."""
    if requested == 'DEFAULT':
        return safe_call(client.operations.get_portfolio, account_id=account_id)
    request = operations_pb2.PortfolioRequest(account_id=account_id)
    request.currency = operations_pb2.PortfolioRequest.CurrencyRequest.Value(requested)

    def get_portfolio_currency():
        try:
            return client.operations.stub.GetPortfolio(
                request, metadata=client.operations.metadata, timeout=10)
        except grpc.RpcError as error:
            if not isinstance(error, grpc.Call):
                raise SandboxFailure('portfolio RPC failed') from None
            raise RequestError(code=getattr(error, 'code')(), details=getattr(error, 'details')(),
                               metadata=None) from None

    return safe_call(get_portfolio_currency)


def buy_one_lot(session, account_id, instrument, report):
    """A mutation is sent once; only reads are polled after NEW/PARTIALLYFILL."""
    request_id = str(session.order_id_factory())
    report['orders'].append({'request_id': request_id, 'uid': instrument.uid, 'lots': 1})
    response = safe_call(
        session.client.orders.post_order, account_id=account_id,
        instrument_id=instrument.uid, quantity=1, direction=OrderDirection.ORDER_DIRECTION_BUY,
        order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=request_id)
    broker_id = response.order_id
    deadline = session.monotonic() + 30
    while True:
        session.check_order(response, instrument.uid, 1, OrderDirection.ORDER_DIRECTION_BUY,
                            broker_id)
        if response.execution_report_status == FILL:
            report['orders'][-1].update({'broker_order_id': broker_id, 'status': 'FILL'})
            return
        if response.execution_report_status not in PENDING or session.monotonic() >= deadline:
            raise SandboxFailure('USD probe setup order not fully filled')
        session.sleep(2)
        response = safe_call(session.client.orders.get_order_state,
                             account_id=account_id, order_id=broker_id)


def capture_phase(session, account_id, expected, phase, report):
    """Record even unsettled snapshots before checking the expected physical holdings."""
    deadline = session.monotonic() + 30
    while True:
        facts = snapshot_facts(read_portfolio(session.client, account_id, 'DEFAULT'),
                               phase, 'DEFAULT')
        report['observations'].append(facts)
        actual = facts['quantity_by_uid']
        if all(Decimal(actual.get(uid, '0')) == amount for uid, amount in expected.items()):
            break
        if session.monotonic() >= deadline:
            raise SandboxFailure('USD probe positions did not settle')
        session.sleep(2)
    for requested in REQUEST_CURRENCIES[1:]:
        record = snapshot_facts(read_portfolio(session.client, account_id, requested),
                                phase, requested)
        report['observations'].append(record)
        if any(Decimal(record['quantity_by_uid'].get(uid, '0')) != amount
               for uid, amount in expected.items()):
            raise SandboxFailure('portfolio currency request changed physical holdings')
    print(f'Portfolio probe: {report["ticker"]} {phase}; currencies recorded', flush=True)


def run_probe(session, ticker, report):
    """One disposable account: USD, then mixed holdings, then a repeated USD purchase."""
    if session.lifecycle.created:
        raise SandboxFailure('previous probe cleanup incomplete')
    session.data.begin_snapshot()
    dollar = resolve_instrument(session.data, ticker, 'usd', report)
    ruble = resolve_instrument(session.data, 'SBER', 'rub', report)
    native = {dollar.uid: 'usd', ruble.uid: 'rub'}
    report['instrument_currencies'] = native
    account_id = session.lifecycle.create('currency' + ticker.lower(), 'dst')
    session.lifecycle.verify_name(account_id)
    for currency, units in (('usd', 100000), ('rub', 1000000)):
        safe_call(session.client.sandbox.sandbox_pay_in, account_id=account_id,
                  amount=MoneyValue(currency=currency, units=units, nano=0))
    holdings = {}
    for phase, instrument in (('usd_first', dollar), ('mixed', ruble), ('usd_second', dollar)):
        buy_one_lot(session, account_id, instrument, report)
        holdings[instrument.uid] = holdings.get(instrument.uid, Decimal(0)) + instrument.lot
        capture_phase(session, account_id, holdings, phase, report)
    report['price_behavior'] = price_behavior(report['observations'], native)
    report['status'] = 'observed'


@pytest.mark.parametrize('ticker', USD_TICKERS)
def test_sandbox_portfolio_currency(ticker, sandbox: SandboxSession):
    """A different observed currency is data, not an assumed-contract assertion failure."""
    report = {'ticker': ticker, 'status': 'incomplete', 'candidates': [], 'orders': [],
              'observations': [], 'cleanup_confirmed': False,
              'tested_sha': os.environ.get('PROBE_SHA', 'local'),
              'scope': 'sandbox observations; not a production API guarantee'}
    try:
        run_probe(sandbox, ticker, report)
    except BaseException as error:
        report['error'] = list_exception_facts(error)
        raise
    finally:
        try:
            sandbox.lifecycle.cleanup()
            report['cleanup_confirmed'] = True
        finally:
            root = Path(os.environ.get('E2E_ARTIFACT_DIR', '/tmp/portfolio-probe'))
            root.mkdir(parents=True, exist_ok=True)
            (root / f'{ticker}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(f'Portfolio probe {ticker}: {report["status"]}; '
                  f'cleanup={report["cleanup_confirmed"]}', flush=True)


def main():
    """Reuse the shared process lock, bounded supervisor and sandbox-only fixture."""
    if not os.environ.get('SANDBOX_TOKEN'):
        print('SANDBOX_TOKEN is required', file=sys.stderr)
        return 2
    keys = ('E2E_NAMESPACE', 'E2E_RUN_ID', 'E2E_ARTIFACT_DIR')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        selected = namespace()
        run_id = uuid4().hex
        with Path('/tmp/autorepeater-sandbox-e2e.lock').open('a', encoding='ascii') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SandboxFailure('overlapping sandbox run forbidden') from None
            os.environ['E2E_NAMESPACE'] = selected
            os.environ['E2E_RUN_ID'] = run_id
            root = Path(os.environ.get('E2E_ARTIFACT_DIR', '/tmp/portfolio-probe-' + run_id))
            root.mkdir(parents=True, exist_ok=True)
            os.environ['E2E_ARTIFACT_DIR'] = str(root)
            return run_bounded([
                sys.executable, '-m', 'pytest', 'test/sandbox_portfolio_live.py',
                '-p', 'e2e.conftest', '--noconftest', '-p', 'no:cacheprovider',
                '-q', '-s', '--tb=no', '--show-capture=no',
                f'--junitxml={root / "junit.xml"}'], 600, selected, run_id)
    except (ValueError, SandboxFailure) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == '__main__':
    sys.exit(main())
