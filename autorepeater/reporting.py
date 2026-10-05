"""User-facing portfolio, order, and event output."""
from datetime import datetime
from decimal import Decimal, localcontext
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
    with localcontext() as context:
        context.prec = max(
            28, position.current_price.adjusted() + position.quantity.adjusted() + 14)
        value = (position.current_price * position.quantity).quantize(NANO_QUANT)
        return f'{position.currency} - {format_decimal(value)}'


def strategy_position_to_string(position):
    """Format a strategy-side position without relying on an SDK DTO."""
    if position.instrument_type == InstrumentType.CURRENCY:
        return _strategy_currency_to_string(position)
    if position.instrument_type in (InstrumentType.SHARE, InstrumentType.ETF):
        quantity = format_decimal(position.quantity)
        return (position.uid + ' - ' + quantity + ' - ' +
                _strategy_currency_to_string(position))
    return position.diagnostic_text


def print_strategy_position(position):
    """Report a position from the SDK-independent strategy data model."""
    logger.info('%s', strategy_position_to_string(position))


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


def print_missing_destination(mode):
    """A portfolio-only local launch or an empty destination does not execute trades."""
    logger.info('Skipping trading: destination account is not set; mode=%s', mode)


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


def print_skipped_strategy_event(event):
    """Report the adapter-provided diagnostic representation of an event."""
    logger.log(IMPORTANT, event.diagnostic_text)
    logger.info('Skipping synchronization: event did not trigger rebalance; '
                'account=%s has_position=%s securities=%d money=%d',
                event.account_id, event.has_position, len(event.securities), len(event.money))


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

def _decimal_map(values):
    return '{' + ', '.join(f'{key}: {format_decimal(value)}'
                           for key, value in values.items()) + '}'


def print_rebalance_plan(plan, debug):
    """Report loaded targets, occurrence permissions and actual monetary bounds."""
    from autorepeater.purchase_plan import nodes  # pylint: disable=import-outside-toplevel
    print_target(plan.strategy.target, plan.strategy.budget)
    for node in nodes(plan.strategy):
        decision = node.decision
        logger.info('Policy path=%s mode=%s reason=%s metric=%s limit=%s '
                    'redistribution=%s budget=%s cash_floor=%s',
                    node.path, decision.mode.value, decision.reason,
                    None if decision.metric is None else format_decimal(decision.metric),
                    None if decision.limit is None else format_decimal(decision.limit),
                    decision.redistribution_allowed, format_decimal(node.budget),
                    format_decimal(node.cash_floor))
    logger.info('Execution money=%s scoped_money=%s limits_ready=%s active_orders=%d',
                _decimal_map(plan.snapshot.available_cash), _decimal_map(plan.money),
                plan.snapshot.limits_ready,
                len(plan.snapshot.active_orders))
    for uid, rule in sorted(plan.rules.items()):
        logger.info('Rules UID %s lot=%s currency=%s API=%s BESTPRICE=%s '
                    'buy_money=%s buy_lots=%s sell_lots=%s', uid, rule.lot, rule.currency,
                    rule.api_trade_available, rule.bestprice_order_available,
                    format_decimal(rule.buy_money_amount), rule.buy_max_lots, rule.sell_max_lots)
    for intent in (*plan.sells, *plan.buys):
        logger.info('Intent UID %s side=%s lots=%s pieces=%s',
                    intent.uid, intent.side, intent.lots, _decimal_map(intent.pieces))
    logger.info('Execution: debug=%s sells=%d buys=%d', debug, len(plan.sells), len(plan.buys))
    if not plan.sells and not plan.buys:
        logger.info('SKIP: no executable orders')
