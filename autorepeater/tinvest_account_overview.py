"""Bounded, read-only account overview independent of the trading snapshot."""
from t_tech.invest import PortfolioPosition, RequestError
from t_tech.invest.services import Services

from autorepeater import reporting
from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.display_data import DisplayAccount, GetInstrumentException
from autorepeater.logging_config import logger
from autorepeater.strategy_data import InstrumentMatch, InstrumentType, PortfolioSnapshot
from autorepeater.tinvest_diagnostics import call_once
from autorepeater.tinvest_strategy_data import instrument_matches, portfolio_entry


def get_instrument(client: Services, uid: str) -> InstrumentMatch:
    """Require exactly one matching UID and usable loaded display metadata."""
    response = call_once(client.instruments.find_instrument, query=uid)
    matches = [item for item in instrument_matches(response.instruments) if item.uid == uid]
    if len(matches) != 1:
        raise GetInstrumentException('error get instrument')
    match = matches[0]
    if not all(isinstance(value, str) and value for value in (match.name, match.ticker)):
        raise GetInstrumentException('instrument metadata unavailable')
    return match


def position_to_string(client: Services, position: PortfolioPosition) -> str:
    """Narrow public compatibility helper; unsupported types retain their raw fallback."""
    if position.instrument_type not in ('currency', 'share', 'etf'):
        return str(position)
    entry = portfolio_entry(position)
    currency_code(entry.currency)
    metadata = ({} if entry.instrument_type == InstrumentType.CURRENCY else
                {entry.uid: get_instrument(client, entry.uid)})
    return reporting.display_position_to_string(entry, metadata)


def _metadata(client: Services, snapshot: PortfolioSnapshot,
              cache: dict[str, InstrumentMatch | None]) -> dict[str, InstrumentMatch]:
    for entry in snapshot.positions:
        uid = entry.uid
        if entry.instrument_type == InstrumentType.CURRENCY or not uid or uid in cache:
            continue
        try:
            cache[uid] = get_instrument(client, uid)
        except (RequestError, ValueError, GetInstrumentException):
            cache[uid] = None
    return {uid: match for uid, match in cache.items() if match is not None}


def _account_portfolio(client: Services, account: DisplayAccount,
                       cache: dict[str, InstrumentMatch | None]) -> MoneyAmount | None:
    reporting.print_display_account_header(account)
    try:
        response = call_once(client.operations.get_portfolio, account_id=account.id)
        snapshot = PortfolioSnapshot(tuple(portfolio_entry(item) for item in response.positions))
        for entry in snapshot.positions:
            currency_code(entry.currency)
    except (RequestError, ValueError):
        logger.warning('portfolio unavailable: account=%s', account.id)
        return None
    metadata = _metadata(client, snapshot, cache)
    raw_total = getattr(response, 'total_amount_portfolio', None)
    try:
        total = MoneyAmount.from_units_nano(getattr(raw_total, 'currency', None),
                                            getattr(raw_total, 'units', None),
                                            getattr(raw_total, 'nano', None))
    except ValueError:
        total = None
    reporting.print_display_portfolio(snapshot, metadata, total)
    return total


def print_all_portfolio(client: Services) -> None:
    """Browse in broker order, continuing expected per-account read failures."""
    try:
        response = call_once(client.users.get_accounts)
    except RequestError:
        logger.warning('overview unavailable')
        return
    totals = []
    unavailable = 0
    cache: dict[str, InstrumentMatch | None] = {}
    for account in response.accounts:
        total = _account_portfolio(client, DisplayAccount(account.id, account.name), cache)
        if total is None:
            unavailable += 1
        else:
            totals.append(total)
    reporting.print_overview_totals(totals, unavailable)
