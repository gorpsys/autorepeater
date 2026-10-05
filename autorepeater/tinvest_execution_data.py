"""Read SDK state using the unit/availability evidence in docs/execution-adapter.md."""
from decimal import Decimal

from t_tech.invest import GetMaxLotsRequest, OrderExecutionReportStatus, RequestError

from autorepeater.execution_data import (
    ActiveOrder, ExecutionDataError, ExecutionSnapshot, TradeRules,
)
from autorepeater.strategy_data import DataAccessError, InstrumentType
from autorepeater.tinvest_strategy_data import _decimal_value

NANO = Decimal('0.000000001')


def _text(value, field):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError(f'{field} must be a nonempty string without whitespace')
    return value


def _bool(value, field):
    if not isinstance(value, bool):
        raise ValueError(f'{field} must be bool')
    return value


def _count(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{field} must be a nonnegative integer')
    return value


def _decimal(value, field, nonnegative=False):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f'{field} must be a finite Decimal')
    if nonnegative and value < 0:
        raise ValueError(f'{field} must be nonnegative')
    return value


def _quotation(value, field):
    return _decimal(_decimal_value(value, field), field, nonnegative=True)


def _read(method, **kwargs):
    try:
        return method(**kwargs)
    except (RequestError, DataAccessError) as error:
        raise ExecutionDataError('execution data read failed') from error


def _money(values, field):
    result = {}
    for index, value in enumerate(values):
        context = f'{field}[{index}]'
        currency = _text(value.currency, f'{context}.currency')
        amount = _quotation(value, context)
        result[currency] = result.get(currency, Decimal(0)) + amount
    return result


def _portfolio_state(portfolio):
    budget = Decimal(0)
    quantities, marks, identities = {}, {}, {}
    blocked = False
    for position in portfolio.positions:
        uid = _text(position.uid, 'portfolio UID')
        context = f'portfolio {uid}'
        _text(position.currency, f'{context}.currency')
        price = _decimal(position.current_price, f'{context}.current_price')
        quantity = _decimal(position.quantity, f'{context}.quantity')
        blocked_flag = _bool(position.blocked, f'{context}.blocked')
        blocked_count = _decimal(position.blocked_lots, f'{context}.blocked_lots', nonnegative=True)
        blocked = blocked or blocked_flag or blocked_count != 0
        budget += (price * quantity).quantize(NANO)
        identity = (price, position.currency, position.instrument_type)
        if uid in identities and identities[uid] != identity:
            raise ValueError(f'portfolio {uid}: incompatible duplicate marks/currency/type')
        identities[uid] = identity
        if position.currency != 'rub':
            raise ValueError(f'portfolio {uid}: valuation currency must be rub without conversion')
        if position.instrument_type != InstrumentType.CURRENCY:
            quantities[uid] = quantities.get(uid, Decimal(0)) + quantity
            marks[uid] = price
    return budget, quantities, marks, blocked


def _positions_blocked(positions):
    blocked = any(amount != 0 for amount in _money(positions.blocked, 'blocked').values())
    for group in ('securities', 'futures', 'options'):
        for item in getattr(positions, group):
            field = f'{group} {item.instrument_uid}'
            count = _count(item.blocked, f'{field}.blocked')
            exchange = (_bool(item.exchange_blocked, f'{field}.exchange_blocked')
                        if group == 'securities' else False)
            blocked = blocked or count != 0 or exchange
    return blocked


def _active_orders(orders):
    active = []
    statuses = {
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW: 'NEW',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL: 'PARTIALLYFILL',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL: 'FILL',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: 'REJECTED',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: 'CANCELLED',
    }
    for order in orders:
        field = f'order {order.order_id}'
        if (not isinstance(order.execution_report_status, OrderExecutionReportStatus)
                or order.execution_report_status not in statuses):
            raise ValueError(f'{field}.execution_report_status is invalid')
        requested = _count(order.lots_requested, f'{field}.lots_requested')
        executed = _count(order.lots_executed, f'{field}.lots_executed')
        if executed > requested:
            raise ValueError(f'{field}.lots_executed exceeds lots_requested')
        status = statuses[order.execution_report_status]
        if status in ('NEW', 'PARTIALLYFILL'):
            active.append(ActiveOrder(
                _text(order.order_id, 'order_id'), _text(order.instrument_uid, f'{field}.UID'),
                status, requested, executed))
    return tuple(active)


class TInvestExecutionData:
    """Translate fresh reads without strategy decisions or monetary settlement guesses."""

    def __init__(self, client, data):
        self._client = client
        self._data = data

    def get_destination(self, account_id):
        """Keep full valuation; advertise no availability in any blocked state."""
        _text(account_id, 'account_id')
        portfolio = _read(self._data.get_portfolio, account_id=account_id)
        budget, quantities, marks, portfolio_blocked = _portfolio_state(portfolio)
        positions = _read(self._client.operations.get_positions, account_id=account_id)
        orders = _read(self._client.orders.get_orders, account_id=account_id)
        cash = _money(positions.money, 'money')
        loading = _bool(positions.limits_loading_in_progress, 'limits_loading_in_progress')
        positions_blocked = _positions_blocked(positions)
        active = _active_orders(orders.orders)
        ready = not (loading or portfolio_blocked or positions_blocked or active)
        return ExecutionSnapshot(
            portfolio, budget, quantities, marks,
            {currency: amount if ready else Decimal(0) for currency, amount in cash.items()},
            {uid: quantity if ready else Decimal(0) for uid, quantity in quantities.items()},
            active, ready)

    def get_trade_rules(self, account_id, uids):
        """Own money and position caps only; BESTPRICE is an explicit permission."""
        _text(account_id, 'account_id')
        result = {}
        for uid in dict.fromkeys(uids):
            _text(uid, 'instrument UID')
            info = _read(self._data.get_instrument, uid=uid)
            if info.uid != uid:
                raise ValueError(f'instrument {uid}: metadata UID mismatch')
            lot = _count(info.lot, f'instrument {uid}.lot')
            if lot == 0:
                raise ValueError(f'instrument {uid}.lot must be positive')
            currency = _text(info.currency, f'instrument {uid}.currency')
            metadata_api = _bool(info.api_trade_available,
                                 f'instrument {uid}.api_trade_available')
            status = _read(self._client.market_data.get_trading_status, instrument_id=uid)
            if status.instrument_uid != uid:
                raise ValueError(f'instrument {uid}: trading status UID mismatch')
            api = _bool(status.api_trade_available_flag,
                        f'instrument {uid}.api_trade_available_flag')
            bestprice = _bool(status.bestprice_order_available_flag,
                              f'instrument {uid}.bestprice_order_available_flag')
            caps = _read(self._client.orders.get_max_lots,
                         request=GetMaxLotsRequest(account_id=account_id, instrument_id=uid))
            if _text(caps.currency, f'instrument {uid}.currency') != currency:
                raise ValueError(f'instrument {uid}: currency mismatch')
            if (not hasattr(caps.buy_limits, 'buy_money_amount')
                    or not hasattr(caps.sell_limits, 'sell_max_lots')):
                raise ValueError(f'instrument {uid}: own limits are missing')
            result[uid] = TradeRules(
                lot, currency, metadata_api and api, bestprice,
                _quotation(caps.buy_limits.buy_money_amount, f'instrument {uid}.buy_money_amount'),
                _count(caps.buy_limits.buy_max_lots, f'instrument {uid}.buy_max_lots'),
                _count(caps.sell_limits.sell_max_lots, f'instrument {uid}.sell_max_lots'))
        return result
