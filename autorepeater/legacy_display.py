"""Legacy SDK reads and permissive conversion for the explicit account browse."""
from decimal import Decimal

from t_tech.invest.services import Services

from autorepeater.display_data import DisplayAccount, DisplayPosition, GetInstrumentException
from autorepeater.logging_config import logger
from autorepeater.money import format_decimal
from autorepeater import reporting
from autorepeater.tinvest_diagnostics import call_once


def _legacy_decimal(value) -> Decimal:
    """Preserve the former money helper's arithmetic and acceptance exactly."""
    return Decimal(value.units) + Decimal(value.nano) / Decimal('1000000000')


def get_instrument(client: Services, instrument_id: str):
    """Keep the original single-match lookup and exception."""
    result = call_once(client.instruments.find_instrument, query=instrument_id).instruments
    if len(result) == 1:
        return result[0]
    raise GetInstrumentException('error get instrument')


def convert_position(client: Services, position) -> DisplayPosition:
    """Project only what the compatibility formatter originally needed, in order."""
    kind = position.instrument_type
    if kind not in ('currency', 'share', 'etf'):
        return DisplayPosition(None, kind, diagnostic_text=str(position))
    label = None
    uid = None
    if kind in ('share', 'etf'):
        uid = position.instrument_uid
        instrument = get_instrument(client, uid)
        # Quantity conversion and metadata formatting preceded the price reads.
        format_decimal(_legacy_decimal(position.quantity))
        label = reporting.display_instrument_label(instrument.name, instrument.ticker)
    currency = position.current_price.currency
    price = _legacy_decimal(position.current_price)
    quantity = _legacy_decimal(position.quantity)
    return DisplayPosition(uid, kind, currency, price, quantity, label)


def print_position(client: Services, position) -> None:
    """Preserve the raw DEBUG record before fallback conversion can fail."""
    if position.instrument_type in ('currency', 'share', 'etf'):
        display = convert_position(client, position)
    else:
        logger.debug('%s', position)
        uid = position.instrument_uid
        kind = position.instrument_type
        # The old fallback total read price before quantity; retain its error precedence.
        price = _legacy_decimal(position.current_price)
        quantity = _legacy_decimal(position.quantity)
        currency = position.current_price.currency
        display = DisplayPosition(uid, kind, currency, price, quantity)
    reporting.print_display_position(display)


def get_portfolio(client: Services, account_id: str):
    """Read once; legacy browsing intentionally has no quota retry loop."""
    return call_once(client.operations.get_portfolio, account_id=account_id)


def print_portfolio_by_account(client: Services, account) -> None:
    """Keep incremental output and the original per-position total calculation."""
    reporting.print_display_account_header(DisplayAccount(account.id, account.name))
    portfolio = get_portfolio(client, account.id)
    total = Decimal('0')
    for position in portfolio.positions:
        print_position(client, position)
        # Total conversion happened after printing, including for unsupported types.
        price = _legacy_decimal(position.current_price)
        quantity = _legacy_decimal(position.quantity)
        total += reporting.display_position_value(price, quantity)
    reporting.print_display_account_footer(total)


def print_all_portfolio(client: Services) -> None:
    """Browse in broker order, without materializing later accounts in advance."""
    response = call_once(client.users.get_accounts)
    for account in response.accounts:
        print_portfolio_by_account(client, account)
