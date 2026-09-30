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


class GetInstrumentException(Exception):
    """Instrument not found uniquely by instrument_id."""


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


def print_total(total):
    """Report the caller's calculated total."""
    logger.log(IMPORTANT, 'total: %s', str(total))


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


def print_skipped_event(response):
    """Report a stream event that did not request synchronization."""
    logger.log(IMPORTANT, response)


def print_empty_target(dst_account_id):
    """Explain why a synchronization leaves destination holdings untouched."""
    logger.log(IMPORTANT, 'Skipping synchronization for destination %s: empty target',
               dst_account_id)


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
