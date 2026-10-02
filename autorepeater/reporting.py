"""User-facing portfolio, order, and event output."""
from datetime import datetime
from decimal import Decimal
import json

from autorepeater.constants import IMPORTANT
from autorepeater.logging_config import logger
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_string
from autorepeater.money import format_decimal
from autorepeater.money import get_quantity_position
from autorepeater.money import no_money_to_string
from autorepeater.portfolio import get_portfolio
from autorepeater.strategy_data import InstrumentType


NANO_QUANT = Decimal('0.000000001')


class GetInstrumentException(Exception):
    """Instrument not found uniquely by instrument_id."""


def print_config_warning(message):
    """Report an unusable foreign config without blocking exact-name selection."""
    logger.warning('%s', message)


def print_index_config_warning(message):
    """Report an unusable foreign index document without blocking selection."""
    print_config_warning(message)


def print_unavailable_index_candidate(instrument):
    """Explain exclusion using full metadata, without additional API reads."""
    logger.info('Skipping index candidate: %s uid=%s class_code=%s api_trade_available=False',
                instrument.ticker, instrument.uid, instrument.class_code)


def print_index_instrument_selected(instrument):
    """Make the dynamically chosen board and UID visible on every snapshot."""
    logger.info('Selected index instrument: %s uid=%s class_code=%s api_trade_available=True',
                instrument.ticker, instrument.uid, instrument.class_code)


def print_index_selection_warning(ticker, chosen, available):
    """Multiple live candidates use the API's original order, not a board preference."""
    details = ', '.join(f'{item.uid}/{item.class_code}' for item in available)
    logger.warning('Multiple API-tradable index instruments: %s; '
                   'selected first uid=%s class_code=%s; candidates: %s',
                   ticker, chosen.uid, chosen.class_code, details)


def get_instrument(client, instrument_id):
    """Find the instrument name used for display."""
    result = client.instruments.find_instrument(query=instrument_id).instruments
    if len(result) == 1:
        return result[0]
    raise GetInstrumentException('error get instrument')


def postiton_to_string(client, position):
    """Convert a position to its existing human-readable representation."""
    if position.instrument_type == 'currency':
        return currency_to_string(position)
    if position.instrument_type in ['share', 'etf']:
        instrument = get_instrument(client, position.instrument_uid)
        quantity = format_decimal(get_quantity_position(position))
        return (no_money_to_string(instrument) + ' - ' +
                quantity + ' - ' + currency_to_string(position))
    return str(position)


def print_account_header(side):
    """Identify the account being synchronized before it is loaded."""
    logger.log(IMPORTANT, '%s account', side)


def print_position(client, position):
    """Report an already loaded position."""
    logger.log(IMPORTANT, postiton_to_string(client, position))


def _strategy_currency_to_string(position):
    value = (position.current_price * position.quantity).quantize(NANO_QUANT)
    return f'{position.currency} - {format_decimal(value)}'


def strategy_position_to_string(data, position):
    """Format a strategy-side position without relying on an SDK DTO."""
    if position.instrument_type == InstrumentType.CURRENCY:
        return _strategy_currency_to_string(position)
    if position.instrument_type in (InstrumentType.SHARE, InstrumentType.ETF):
        instruments = data.find_instruments(position.uid)
        if len(instruments) != 1:
            raise GetInstrumentException('error get instrument')
        instrument = instruments[0]
        quantity = format_decimal(position.quantity)
        return (no_money_to_string(instrument) + ' - ' + quantity + ' - ' +
                _strategy_currency_to_string(position))
    return position.diagnostic_text


def print_strategy_position(data, position):
    """Report a position from the SDK-independent strategy data model."""
    logger.log(IMPORTANT, strategy_position_to_string(data, position))


def print_total(total):
    """Report the caller's calculated total."""
    logger.log(IMPORTANT, 'total: %s', str(total))


def print_launch(algoritm, src, dst):
    """Report resolved selection, never the request or authentication token."""
    logger.info('Launch: algoritm=%s src=%s dst=%s', algoritm, src, dst)


def print_target(target, budget):
    """Describe the validated full target without inspecting strategy internals."""
    total = Decimal('0')
    for uid, quantity in target.quantities.items():
        price = target.prices[uid]
        value = quantity * price
        total += value
        logger.info('Target position: uid=%s quantity=%s price=%s estimated_value=%s',
                    uid, format_decimal(quantity), format_decimal(price), format_decimal(value))
    logger.info('Target: instruments=%d estimated_value=%s budget=%s estimated_cash=%s',
                len(target.quantities), format_decimal(total), format_decimal(budget),
                format_decimal(budget - total))


def print_nontrading_instrument(instrument, side):
    """Explain the executor's trading-status guard using already loaded metadata."""
    status = instrument.trading_status
    logger.info('Skipping %s: %s uid=%s class_code=%s lot=%s trading_status=%s',
                side, no_money_to_string(instrument), instrument.uid, instrument.class_code,
                instrument.lot, getattr(status, 'name', status))


def print_order_skip(instrument, side, current, target, lots=None):
    """Report an unchanged target or a delta that did not produce positive lots."""
    reason = ('target does not require this direction' if lots is None
              else 'rounded quantity is not positive')
    logger.info('Skipping %s: %s uid=%s current=%s target=%s lot=%s rounded_lots=%s reason=%s',
                side, no_money_to_string(instrument), instrument.uid,
                format_decimal(current), format_decimal(target), instrument.lot, lots, reason)


def print_missing_destination(mode):
    """A portfolio-only local launch or an empty destination does not execute trades."""
    logger.info('Skipping trading: destination account is not set; mode=%s', mode)


def print_execution_decision(sells, buys, volume=None, threshold_value=None):
    """None valuation denotes debug, which does not evaluate the submission threshold."""
    if volume is None:
        logger.info('Execution: debug mode, sells=%d buys=%d; no orders submitted',
                    len(sells), len(buys))
        return
    logger.info('Execution: sells=%d buys=%d volume=%s threshold_value=%s submit=%s',
                len(sells), len(buys), format_decimal(volume), format_decimal(threshold_value),
                volume > threshold_value)
    if not sells and not buys:
        logger.info('Skipping trading: no executable orders')
    elif volume <= threshold_value:
        logger.info('Skipping trading: order volume does not exceed threshold')


def print_portfolio_by_account(client, account):
    """Print detailed information about an account, including its cash."""
    logger.log(IMPORTANT, '%s (%s)', account.name, account.id)
    logger.log(IMPORTANT, '------------')
    portfolio = get_portfolio(client, account.id)
    total = Decimal('0')
    for position in portfolio.positions:
        print_position(client, position)
        total += currency_to_decimal(position)
    print_total(total)
    logger.log(IMPORTANT, '============')


def print_all_portfolio(client):
    """Print detailed information about all accounts."""
    response = client.users.get_accounts()
    for account in response.accounts:
        print_portfolio_by_account(client, account)


def print_sell(instrument, quantity):
    """Report a calculated sale without submitting it."""
    logger.log(IMPORTANT, 'Продать: %s %d лотов', no_money_to_string(instrument), quantity)


def print_buy(instrument, quantity):
    """Report a calculated purchase without submitting it."""
    logger.log(IMPORTANT, 'Купить: %s %d лотов', no_money_to_string(instrument), quantity)


def print_order(order_params):
    """Report order parameters before submission."""
    logger.log(IMPORTANT, order_params)


def print_order_result(order_id):
    """Report the identifier returned by order submission."""
    logger.log(IMPORTANT, order_id)


def print_order_execution(response):
    """An accepted order identifier alone does not establish that lots were filled."""
    status = response.execution_report_status
    logger.info('Order result: id=%s status=%s lots_requested=%s lots_executed=%s',
                response.order_id, getattr(status, 'name', status),
                response.lots_requested, response.lots_executed)


def print_skipped_strategy_event(event):
    """Report the adapter-provided diagnostic representation of an event."""
    logger.log(IMPORTANT, event.diagnostic_text)
    logger.info('Skipping synchronization: event did not trigger rebalance; '
                'account=%s has_position=%s securities=%d money=%d',
                event.account_id, event.has_position, len(event.securities), len(event.money))


def print_empty_target(dst_account_id, reason=None):
    """Explain why a synchronization leaves destination holdings untouched."""
    logger.info('Skipping synchronization for destination %s: %s',
               dst_account_id, reason or 'empty target')


def _calibration_json_value(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'unsupported calibration value: {type(value).__name__}')


def format_index_calibration(report):
    """Preserve decimal precision and quotation timestamps in a reusable report."""
    return json.dumps(report, default=_calibration_json_value, indent=2, allow_nan=False)


def print_index_calibration(payload):
    """Display the same public data that can be saved for offline reproduction."""
    logger.log(IMPORTANT, '%s', payload)
