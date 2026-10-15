"""User-facing portfolio, order, and event output."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, localcontext
import json
from typing import TYPE_CHECKING, Protocol, TypeVar

from autorepeater.cash_facts import order_cash_bound
from autorepeater.constants import IMPORTANT
from autorepeater.logging_config import logger
from autorepeater.money import currency_to_decimal
from autorepeater.money import currency_to_string
from autorepeater.money import format_decimal
from autorepeater.money import get_quantity_position
from autorepeater.money import no_money_to_string
from autorepeater.portfolio import DisplayPosition, PortfolioClient, TargetPortfolio, get_portfolio
from autorepeater.strategy_data import InstrumentInfo, InstrumentType, PortfolioEntry, PositionEvent
from autorepeater.strategy_plan import exact_product

if TYPE_CHECKING:
    from autorepeater.execution import ExecutionReceipt
    from autorepeater.fx import FxSnapshot
    from autorepeater.order_plan import OrderPlan

KeyT = TypeVar('KeyT')


class DisplayInstrument(Protocol):  # pylint: disable=too-few-public-methods
    """Name and ticker needed for the legacy instrument display."""
    name: str
    ticker: str


class InstrumentMatches(Protocol):  # pylint: disable=too-few-public-methods
    """Ordered matches from the legacy display lookup."""
    instruments: Sequence[DisplayInstrument]


class InstrumentFinder(Protocol):  # pylint: disable=too-few-public-methods
    """Only the instrument lookup used for explicit account display."""

    def find_instrument(self, *, query: str) -> InstrumentMatches:
        """Find display metadata."""


class DisplayAccount(Protocol):  # pylint: disable=too-few-public-methods
    """Account identity used by explicit account display."""
    name: str
    id: str


class DisplayAccounts(Protocol):  # pylint: disable=too-few-public-methods
    """Accounts returned for explicit display."""
    accounts: Sequence[DisplayAccount]


class AccountReader(Protocol):  # pylint: disable=too-few-public-methods
    """Only the account-list read used by reporting."""

    def get_accounts(self) -> DisplayAccounts:
        """Read available account identities."""


class ReportingClient(PortfolioClient, Protocol):  # pylint: disable=too-few-public-methods
    """Legacy display reads; strategy reporting still uses only loaded neutral DTOs."""
    instruments: InstrumentFinder
    users: AccountReader


NANO_QUANT = Decimal('0.000000001')


class GetInstrumentException(Exception):
    """Instrument not found uniquely by instrument_id."""


def print_config_warning(message: str) -> None:
    """Report an unusable foreign config without blocking exact-name selection."""
    logger.warning('%s', message)


def print_index_config_warning(message: str) -> None:
    """Report an unusable foreign index document without blocking selection."""
    print_config_warning(message)


def print_unavailable_index_candidate(instrument: InstrumentInfo) -> None:
    """Explain exclusion using full metadata, without additional API reads."""
    logger.info('Skipping index candidate: %s uid=%s class_code=%s api_trade_available=False',
                instrument.ticker, instrument.uid, instrument.class_code)


def print_index_instrument_selected(instrument: InstrumentInfo) -> None:
    """Make the dynamically chosen board and UID visible on every snapshot."""
    logger.info('Selected index instrument: %s uid=%s class_code=%s api_trade_available=True',
                instrument.ticker, instrument.uid, instrument.class_code)


def print_index_selection_warning(ticker: str, chosen: InstrumentInfo,
                                  available: Sequence[InstrumentInfo]) -> None:
    """Multiple live candidates use the API's original order, not a board preference."""
    details = ', '.join(f'{item.uid}/{item.class_code}' for item in available)
    logger.warning('Multiple API-tradable index instruments: %s; '
                   'selected first uid=%s class_code=%s; candidates: %s',
                   ticker, chosen.uid, chosen.class_code, details)


def get_instrument(client: ReportingClient, instrument_id: str) -> DisplayInstrument:
    """Find the instrument name used for display."""
    result = client.instruments.find_instrument(query=instrument_id).instruments
    if len(result) == 1:
        return result[0]
    raise GetInstrumentException('error get instrument')


def postiton_to_string(client: ReportingClient, position: DisplayPosition) -> str:
    """Convert a position to its existing human-readable representation."""
    if position.instrument_type == 'currency':
        return currency_to_string(position)
    if position.instrument_type in ['share', 'etf']:
        instrument = get_instrument(client, position.instrument_uid)
        quantity = format_decimal(get_quantity_position(position))
        return (no_money_to_string(instrument) + ' - ' +
                quantity + ' - ' + currency_to_string(position))
    return str(position)


def print_account_header(side: str) -> None:
    """Identify the account being synchronized before it is loaded."""
    logger.log(IMPORTANT, '%s account', side)


def print_position(client: ReportingClient, position: DisplayPosition) -> None:
    """Report an already loaded position."""
    logger.log(IMPORTANT, postiton_to_string(client, position))


def _strategy_currency_to_string(position: PortfolioEntry) -> str:
    with localcontext() as context:
        context.prec = max(
            28, position.current_price.adjusted() + position.quantity.adjusted() + 14)
        value = (position.current_price * position.quantity).quantize(NANO_QUANT)
        return f'{position.currency} - {format_decimal(value)}'


def strategy_position_to_string(position: PortfolioEntry) -> str:
    """Format a strategy-side position without relying on an SDK DTO."""
    if (position.native_price is not None and position.rub_price is not None
            and position.settlement_currency is not None):
        return (f'{position.uid} quantity={format_decimal(position.quantity)} '
                f'native_price={format_decimal(position.native_price)} '
                f'currency={position.settlement_currency} '
                f'rub_price={format_decimal(position.rub_price)} '
                f'broker_price={format_decimal(position.current_price)} '
                f'broker_currency={position.currency}')
    if position.instrument_type == InstrumentType.CURRENCY:
        return _strategy_currency_to_string(position)
    if position.instrument_type in (InstrumentType.SHARE, InstrumentType.ETF):
        quantity = format_decimal(position.quantity)
        return (position.uid + ' - ' + quantity + ' - ' +
                _strategy_currency_to_string(position))
    return position.diagnostic_text


def print_strategy_position(position: PortfolioEntry) -> None:
    """Report a position from the SDK-independent strategy data model."""
    logger.info('%s', strategy_position_to_string(position))


def print_total(total: Decimal) -> None:
    """Report the caller's calculated total."""
    logger.log(IMPORTANT, 'total: %s', str(total))


def print_launch(algoritm: str, src: str, dst: str | None) -> None:
    """Report resolved selection, never the request or authentication token."""
    logger.info('Launch: algoritm=%s src=%s dst=%s', algoritm, src, dst)


def print_target(target: TargetPortfolio, budget: Decimal) -> None:
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


def print_missing_destination(mode: str) -> None:
    """A portfolio-only local launch or an empty destination does not execute trades."""
    logger.info('Skipping trading: destination account is not set; mode=%s', mode)


def print_portfolio_by_account(client: ReportingClient, account: DisplayAccount) -> None:
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


def print_all_portfolio(client: ReportingClient) -> None:
    """Print detailed information about all accounts."""
    response = client.users.get_accounts()
    for account in response.accounts:
        print_portfolio_by_account(client, account)


def print_skipped_strategy_event(event: PositionEvent) -> None:
    """Report the adapter-provided diagnostic representation of an event."""
    logger.log(IMPORTANT, event.diagnostic_text)
    logger.info('Skipping synchronization: event did not trigger rebalance; '
                'account=%s has_position=%s securities=%d money=%d',
                event.account_id, event.has_position, len(event.securities), len(event.money))


def _calibration_json_value(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'unsupported calibration value: {type(value).__name__}')


def format_index_calibration(report: Mapping[str, object]) -> str:
    """Preserve decimal precision and quotation timestamps in a reusable report."""
    return json.dumps(report, default=_calibration_json_value, indent=2, allow_nan=False)


def print_index_calibration(payload: str) -> None:
    """Display the same public data that can be saved for offline reproduction."""
    logger.log(IMPORTANT, '%s', payload)


def _decimal_map(values: Mapping[KeyT, Decimal]) -> str:
    return '{' + ', '.join(f'{key}: {format_decimal(value)}'
                           for key, value in values.items()) + '}'


def print_order_money(receipt: ExecutionReceipt) -> None:
    """Called only after receipt validation; incomplete money never becomes exact cash."""
    bound = (order_cash_bound(receipt, receipt.amounts)
             if receipt.amounts is not None else None)
    if bound is None:
        logger.info('Order money: uid=%s order_id=%s cash_bound=unavailable confirmed_cash=%s',
                    receipt.uid, receipt.order_id, _decimal_map(receipt.cash))
        return
    logger.info('Order money: uid=%s order_id=%s cash_bound=%s currency=%s amount=%s '
                'executed_commission=%s commission_currency=%s confirmed_cash=%s',
                receipt.uid, receipt.order_id,
                'debit_upper' if receipt.side == 'BUY' else 'credit_lower',
                bound.amount.currency, format_decimal(bound.amount.amount),
                format_decimal(receipt.amounts.executed_commission.amount),
                receipt.amounts.executed_commission.currency, _decimal_map(receipt.cash))


def print_fx_snapshot(fx: FxSnapshot, *, stage: str = 'planning') -> None:
    """Report observed quote units and time without performing any market reads."""
    for currency, quote in sorted(fx.quotes.items()):
        logger.info('FX valuation pair=%s currency=%s rub_per_unit=%s '
                    'bid=%s ask=%s nominal=%s lot=%d market_time=%s received_time=%s stage=%s',
                    quote.pair.uid, currency, format_decimal(fx.rate(currency)),
                    format_decimal(quote.bid), format_decimal(quote.ask),
                    format_decimal(quote.pair.nominal), quote.pair.lot_size,
                    quote.market_time.isoformat(), quote.received_time.isoformat(), stage)


def _print_funding(plan: OrderPlan) -> None:
    funding = plan.funding
    for uid, price in sorted(funding.native_prices.items()):
        logger.info('Valuation uid=%s native_price=%s currency=%s rub_price=%s',
                    uid, format_decimal(price.amount), price.currency,
                    format_decimal(plan.marks[uid]))
    print_fx_snapshot(plan.snapshot.fx_snapshot)
    for path in sorted(funding.spending_limits):
        logger.info('Funding path=%s available_native=%s reserve_native=%s '
                    'security_debit_estimate=%s estimated_security_fees=%s '
                    'debit_includes_estimated_fees=True '
                    'projected_residual_including_lot_surplus=%s security_limit_rub=%s',
                    path, _decimal_map(funding.cash.balances.get(path, {})),
                    _decimal_map(funding.reserve_shares.get(path, {})),
                    _decimal_map(funding.security_debits.balances.get(path, {})),
                    _decimal_map(funding.estimated_security_fees.balances.get(path, {})),
                    _decimal_map(funding.projected_cash.balances.get(path, {})),
                    format_decimal(funding.spending_limits[path]))
    logger.info('Funding protected_native=%s', _decimal_map(funding.cash.protected))
    _print_funding_orders(plan)


def _owner_money(values: Mapping[object, Mapping[str, Decimal]]) -> str:
    return '{' + ', '.join(f'{path}: {_decimal_map(money)}'
                           for path, money in values.items()) + '}'


def _print_funding_orders(plan: OrderPlan) -> None:
    funding = plan.funding
    for intent in funding.exchanges:
        currency = next(code for code, quote in plan.snapshot.fx_snapshot.quotes.items()
                        if quote.pair.uid == intent.uid)
        route = f'rub->{currency}' if intent.side == 'BUY' else f'{currency}->rub'
        logger.info('FX route=%s pair=%s side=%s lots=%d lot=%d dependent_uids=%s '
                    'estimated_debits=%s estimated_credits=%s estimated_fees=%s owners=%s',
                    route, intent.uid, intent.side, intent.lots, intent.lot_size,
                    intent.dependent_uids, _owner_money(intent.debits),
                    _owner_money(intent.credits), _owner_money(intent.estimated_fees),
                    _decimal_map(intent.base_contributions))
    selected = {}
    for purchase in funding.purchases:
        intent = purchase.intent
        selected[intent.uid] = intent.lots
        logger.info('Funding BUY uid=%s lots=%d executable_now=%s native_estimate=%s '
                    'currency=%s fx_dependencies=%s owners=%s',
                    intent.uid, intent.lots, purchase.executable_now,
                    format_decimal(purchase.native_amount.amount), purchase.native_amount.currency,
                    purchase.fx_dependencies, _decimal_map(intent.pieces))
    for demand in funding.demands:
        remaining = demand.lots - selected.get(demand.uid, 0)
        price = funding.native_prices[demand.uid]
        logger.info('Funding demand uid=%s lots=%d native_estimate=%s currency=%s owners=%s',
                    demand.uid, demand.lots, format_decimal(exact_product(
                        price.amount, Decimal(demand.lots * demand.lot_size))),
                    price.currency, _decimal_map(demand.pieces))
        if remaining:
            logger.info('Defer funding uid=%s unfilled_lots=%d '
                        'reason=native cash/FX lot/own caps/budget', demand.uid, remaining)


def print_rebalance_plan(plan: OrderPlan, debug: bool) -> None:
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
    if plan.funding is not None:
        _print_funding(plan)
        logger.info('Execution funding: funded_buys=%d exchanges=%d',
                    len(plan.funding.purchases), len(plan.funding.exchanges))
    logger.info('Execution: debug=%s sells=%d buys=%d', debug, len(plan.sells), len(plan.buys))
    if not plan.sells and not plan.buys and (plan.funding is None or not plan.funding.purchases):
        logger.info('SKIP: no executable orders')
