"""Explicit-only sandbox monetary evidence; never collected by ordinary test_*."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from t_tech.invest import (
    GetOperationsByCursorRequest, GetOrderPriceRequest, MoneyValue,
    OrderDirection, OrderType,
)

from scripts.sandbox_e2e import namespace, run_bounded
from scripts.sandbox_lifecycle import SandboxFailure, list_exception_facts, safe_call
from scripts.sandbox_support import FILL, PENDING, SandboxSession

INSTRUMENTS = {
    'CNYRUB_TOM': '4587ab1d-a9c9-4910-a0d6-86c7b9c42510',
    'RUSAL': 'ffc0fc8b-fe3b-4729-9eea-593bb1f899b0',
    'SBER': 'e6123145-9665-43e0-8413-cd61b8aa9b13',
}
MONEY_FIELDS = ('initial_order_price', 'executed_order_price', 'total_order_amount',
                'initial_commission', 'executed_commission', 'service_commission',
                'initial_security_price', 'aci_value', 'average_position_price')


def number(value):
    """Exact signed units/nano; malformed inputs are not evidence."""
    if any(isinstance(part, bool) or not isinstance(part, int)
           for part in (value.units, value.nano)) or not -1000000000 < value.nano < 1000000000:
        raise SandboxFailure('invalid monetary number')
    return Decimal(value.units) + Decimal(value.nano) / Decimal(1000000000)


def money(value):
    """Only explicit monetary fields, including an unspecified currency if supplied."""
    if not hasattr(value, 'currency'):
        return None
    currency = value.currency
    if not isinstance(currency, str) or re.fullmatch('[a-z]{3}|', currency) is None:
        raise SandboxFailure('invalid monetary currency')
    return {'currency': currency, 'units': value.units, 'nano': value.nano,
            'amount': format(number(value), 'f')}


def identity(value):
    """Identifiers only, never SDK messages or descriptions."""
    if not isinstance(value, str):
        return None
    if re.fullmatch('[A-Za-z0-9_.:/-]{0,160}', value) is None:
        raise SandboxFailure('invalid evidence identifier')
    return value


def enum_name(value):
    """Missing SDK fields remain missing, not guessed defaults."""
    name = getattr(value, 'name', None)
    return name if isinstance(name, str) and re.fullmatch('[A-Z0-9_]+', name) else None


def timestamp(value):
    """Dates only; no arbitrary conversion to strings."""
    return value.isoformat() if isinstance(value, datetime) else None


def trades_facts(trades):
    """Legacy and cursor trade keys differ; normalize names without inferring links."""
    if not isinstance(trades, (tuple, list)):
        return []
    return [{'trade_id': identity(getattr(trade, 'trade_id', getattr(trade, 'num', None))),
             'quantity': trade.quantity if isinstance(trade.quantity, int) else None,
             'price': money(getattr(trade, 'price', None)),
             'date': timestamp(getattr(trade, 'date_time', getattr(
                 trade, 'date', getattr(trade, 'execution_time', None))))}
            for trade in trades]


def operation_facts(operation):
    """Allowlisted operation fields; free-form description and account IDs excluded."""
    trades = getattr(operation, 'trades', None)
    if not isinstance(trades, (tuple, list)):
        trades = getattr(getattr(operation, 'trades_info', None), 'trades', None)
    result = {
        'id': identity(operation.id),
        'parent_operation_id': identity(operation.parent_operation_id),
        'instrument_uid': identity(getattr(operation, 'instrument_uid', None)),
        'type': enum_name(getattr(operation, 'operation_type', getattr(operation, 'type', None))),
        'state': enum_name(operation.state), 'date': timestamp(operation.date),
        'trades': trades_facts(trades),
    }
    for field in ('payment', 'price', 'commission', 'accrued_int'):
        result[field] = money(getattr(operation, field, None))
    for field in ('quantity', 'quantity_rest', 'quantity_done'):
        value = getattr(operation, field, None)
        result[field] = value if isinstance(value, int) and not isinstance(value, bool) else None
    children = getattr(operation, 'child_operations', None)
    result['children'] = ([{'id': identity(getattr(child, 'id', None)),
                            'payment': money(getattr(child, 'payment', None)),
                            'type': enum_name(getattr(child, 'operation_type', None))}
                           for child in children] if isinstance(children, (tuple, list)) else [])
    return result


def order_facts(response):
    """PostOrder and GetOrderState are independently retained as broker facts."""
    stages = getattr(response, 'stages', None)
    result = {'order_id': identity(response.order_id),
              'instrument_uid': identity(response.instrument_uid),
              'direction': enum_name(response.direction),
              'status': enum_name(response.execution_report_status),
              'lots_requested': response.lots_requested, 'lots_executed': response.lots_executed,
              'stages': trades_facts(stages)}
    result.update({field: money(getattr(response, field, None)) for field in MONEY_FIELDS})
    return result


def operation_links(order, operations):
    """Only exact identity links; no matching by amount, UID or proximity in time."""
    trade_ids = {item['trade_id'] for item in order['stages'] if item.get('trade_id')}
    result = []
    for operation in operations:
        bases = []
        if operation['id'] == order['order_id']:
            bases.append('operation_id_equals_broker_order_id')
        if operation['parent_operation_id'] == order['order_id']:
            bases.append('parent_equals_broker_order_id')
        if trade_ids & {item['trade_id'] for item in operation['trades'] if item.get('trade_id')}:
            bases.append('trade_id_intersection')
        if bases:
            result.append({'operation_id': operation['id'], 'bases': bases})
    return result


def cursor_operations(client, account_id, start):
    """Bounded complete pagination with commissions and trades included."""
    cursor, seen, result = '', set(), []
    for _ in range(5):
        response = safe_call(
            client.operations.get_operations_by_cursor,
            request=GetOperationsByCursorRequest(
                account_id=account_id, from_=start,
                to=datetime.now(timezone.utc), cursor=cursor, limit=100,
                without_commissions=False, without_trades=False, without_overnights=False))
        result.extend(operation_facts(item) for item in response.items)
        if response.has_next is False:
            return result
        next_cursor = response.next_cursor
        if response.has_next is not True or not next_cursor or next_cursor in seen:
            raise SandboxFailure('invalid cursor pagination')
        seen.add(next_cursor)
        cursor = next_cursor
    raise SandboxFailure('incomplete cursor pagination')


def observe(reader, report, label, **kwargs):
    """Read failures are recorded; authorization failures stop the experiment."""
    try:
        return reader(**kwargs)
    except SandboxFailure as error:
        facts = list_exception_facts(error)
        if any(item.get('grpc_status') in ('UNAUTHENTICATED', 'PERMISSION_DENIED')
               for item in facts):
            raise
        report.setdefault('read_gaps', []).append({'method': label, 'error': facts})
        return None


def capture(session, account_id, start, phase, report):
    """Cash deltas are controls only; they are not assigned to a strategy or order."""
    positions = safe_call(session.client.operations.get_positions, account_id=account_id)
    state = {'phase': phase, 'observed_at': datetime.now(timezone.utc).isoformat(),
             'cash': [money(item) for item in positions.money],
             'blocked': [money(item) for item in positions.blocked],
             'limits_loading': positions.limits_loading_in_progress}
    report['observations'].append(state)
    legacy = observe(safe_call, report, 'GetOperations',
                     method=session.client.operations.get_operations, account_id=account_id,
                     from_=start, to=datetime.now(timezone.utc))
    state['legacy'] = (None if legacy is None
                       else [operation_facts(item) for item in legacy.operations])
    state['cursor'] = observe(cursor_operations, report, 'GetOperationsByCursor',
                              client=session.client, account_id=account_id, start=start)
    for order in report['orders']:
        fact = order.get('state') or order.get('post')
        if fact:
            state.setdefault('links', []).append({
                'broker_order_id': fact['order_id'],
                'legacy': operation_links(fact, state['legacy'] or []),
                'cursor': operation_links(fact, state['cursor'] or [])})


def submit(session, account_id, instrument, side, report, *, lots):
    """One mutation; bounded status reads, never a retry of an uncertain order."""
    # pylint: disable=too-many-arguments
    direction = getattr(OrderDirection, 'ORDER_DIRECTION_' + side)
    stage = {'ticker': instrument.ticker, 'uid': instrument.uid, 'side': side, 'lots': lots,
             'request_id': str(session.order_id_factory())}
    report['orders'].append(stage)
    book = safe_call(session.client.market_data.get_order_book,
                     instrument_id=instrument.uid, depth=1)
    levels = book.asks if side == 'BUY' else book.bids
    if not levels or number(levels[0].price) <= 0:
        raise SandboxFailure('no positive quote for monetary probe')
    stage['estimate_input_quote'] = format(number(levels[0].price), 'f')
    estimate = observe(
        safe_call, report, 'GetOrderPrice', method=session.client.orders.get_order_price,
        request=GetOrderPriceRequest(account_id=account_id, instrument_id=instrument.uid,
                                     quantity=lots, direction=direction, price=levels[0].price))
    if estimate is not None:
        stage['estimate'] = {name: money(getattr(estimate, name, None)) for name in (
            'total_order_amount', 'initial_order_amount', 'executed_commission',
            'executed_commission_rub', 'service_commission', 'deal_commission')}
        stage['estimate']['aci_value'] = money(
            getattr(getattr(estimate, 'extra_bond', None), 'aci_value', None))
    response = safe_call(session.client.orders.post_order, account_id=account_id,
                         instrument_id=instrument.uid, direction=direction, quantity=lots,
                         order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=stage['request_id'])
    stage['post'] = order_facts(response)
    broker_id = response.order_id
    deadline = session.monotonic() + 30
    while True:
        session.check_order(response, instrument.uid, lots, direction, broker_id)
        if response.execution_report_status == FILL:
            break
        if response.execution_report_status not in PENDING or session.monotonic() >= deadline:
            raise SandboxFailure('cash probe order not fully confirmed')
        session.sleep(2)
        response = safe_call(session.client.orders.get_order_state,
                             account_id=account_id, order_id=broker_id)
    state = safe_call(session.client.orders.get_order_state,
                      account_id=account_id, order_id=broker_id)
    session.check_order(state, instrument.uid, lots, direction, broker_id)
    if state.execution_report_status != FILL:
        raise SandboxFailure('cash probe state no longer confirms fill')
    stage['state'] = order_facts(state)
    print(f'Cash facts: {instrument.ticker} {side} lots={lots}; FILL confirmed', flush=True)


def run_probe(session, report):
    """A fresh account isolates four monetary directions and a RUB control sale."""
    if session.lifecycle.created:
        raise SandboxFailure('previous cash probe cleanup incomplete')
    session.data.begin_snapshot()
    instruments = {}
    for name, uid in INSTRUMENTS.items():
        info = safe_call(session.data.get_instrument, uid=uid)
        status = safe_call(session.client.market_data.get_trading_status, instrument_id=uid)
        report.setdefault('preflight', []).append({
            'name': name, 'uid': uid, 'ticker': info.ticker, 'currency': info.currency,
            'lot': info.lot, 'status': enum_name(status.trading_status),
            'api': status.api_trade_available_flag,
            'bestprice': status.bestprice_order_available_flag})
        expected_currency = 'cny' if name == 'RUSAL' else 'rub'
        if (info.uid != uid or status.instrument_uid != uid
                or info.lot != (1000 if name == 'CNYRUB_TOM' else 1)
                or info.currency != expected_currency):
            raise SandboxFailure('cash probe instrument metadata changed')
        if (info.api_trade_available is not True or status.api_trade_available_flag is not True
                or status.bestprice_order_available_flag is not True):
            raise SandboxFailure('cash probe trading unavailable')
        instruments[name] = info
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    account_id = session.lifecycle.create('cashfacts', 'dst')
    session.lifecycle.verify_name(account_id)
    safe_call(session.client.sandbox.sandbox_pay_in, account_id=account_id,
              amount=MoneyValue(currency='rub', units=1000000, nano=0))
    capture(session, account_id, start, 'funded', report)
    sequence = [('CNYRUB_TOM', 'BUY', 10), ('RUSAL', 'BUY', 2), ('RUSAL', 'SELL', 1),
                ('CNYRUB_TOM', 'SELL', 2), ('SBER', 'BUY', 1), ('SBER', 'SELL', 1)]
    for index, (name, side, lots) in enumerate(sequence, 1):
        submit(session, account_id, instruments[name], side, report, lots=lots)
        for delay in (0, 2, 6):
            session.sleep(delay)
            capture(session, account_id, start, f'{index}_{name}_{side}_plus{delay}', report)
    session.sleep(30)
    capture(session, account_id, start, 'final_after_30s', report)
    report['status'] = 'observed'


def test_sandbox_cash_facts(sandbox: SandboxSession):
    """Observation completion is not automatic proof of a net-cash contract."""
    report = {'tested_sha': os.environ.get('PROBE_SHA', 'local'), 'status': 'incomplete',
              'started_at': datetime.now(timezone.utc).isoformat(), 'orders': [],
              'observations': [], 'cleanup_confirmed': False,
              'scope': 'sandbox facts; not production acceptance or an approved cash formula'}
    try:
        run_probe(sandbox, report)
    except BaseException as error:
        report['error'] = list_exception_facts(error)
        raise
    finally:
        try:
            sandbox.lifecycle.cleanup()
            report['cleanup_confirmed'] = True
        finally:
            root = Path(os.environ.get('E2E_ARTIFACT_DIR', '/tmp/cash-facts'))
            root.mkdir(parents=True, exist_ok=True)
            (root / 'cash-facts.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(f'Cash facts: {report["status"]}; '
                  f'cleanup={report["cleanup_confirmed"]}', flush=True)


def main():
    """Reuse serialization and supervised process cleanup; sandbox credentials only."""
    if not os.environ.get('SANDBOX_TOKEN'):
        print('SANDBOX_TOKEN is required', file=sys.stderr)
        return 2
    keys = ('E2E_NAMESPACE', 'E2E_RUN_ID', 'E2E_ARTIFACT_DIR')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        selected, run_id = namespace(), uuid4().hex
        with Path('/tmp/autorepeater-sandbox-e2e.lock').open('a', encoding='ascii') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SandboxFailure('overlapping sandbox run forbidden') from None
            os.environ.update(E2E_NAMESPACE=selected, E2E_RUN_ID=run_id)
            root = Path(os.environ.get('E2E_ARTIFACT_DIR', '/tmp/cash-facts-' + run_id))
            root.mkdir(parents=True, exist_ok=True)
            os.environ['E2E_ARTIFACT_DIR'] = str(root)
            return run_bounded([
                sys.executable, '-m', 'pytest', 'test/sandbox_cash_live.py',
                '-p', 'e2e.conftest', '--noconftest', '-p', 'no:cacheprovider',
                '-q', '-s', '--tb=no', '--show-capture=no',
                f'--junitxml={root / "junit.xml"}'], 900, selected, run_id)
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
