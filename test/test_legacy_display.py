"""Exact legacy broker-read order and compatibility acceptance."""
import inspect
import logging
from decimal import Decimal, InvalidOperation
from unittest.mock import Mock, call, create_autospec, patch

import pytest
from grpc import StatusCode
from t_tech.invest import schemas as sdk
from t_tech.invest import RequestError
from t_tech.invest.services import (
    InstrumentsService, OperationsService, UsersService, OrdersService,
)

from autorepeater import legacy_display, reporting
from autorepeater import runner as runner_module
from autorepeater.constants import IMPORTANT
from autorepeater.logging_config import logger
from autorepeater.money import format_decimal
from autorepeater.strategy_contract import PreparedStrategy, Strategy


@pytest.fixture(name='client')
def fixture_client():
    """Strict SDK services with genuine responses, never a connected client."""
    services = {'instruments': InstrumentsService, 'operations': OperationsService,
                'users': UsersService, 'orders': OrdersService}
    client = Mock(spec_set=list(services))
    for name, service in services.items():
        setattr(client, name, create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True))
    client.users.get_accounts.return_value = sdk.GetAccountsResponse(accounts=[
        sdk.Account(id='selected', name='Selected'), sdk.Account(id='other', name='Other')])
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse()
    client.instruments.find_instrument.return_value = sdk.FindInstrumentResponse(instruments=[
        sdk.InstrumentShort(name='Company', ticker='TICK')])
    return client


def position(kind='currency', currency='RUB', units=1, nano=0):
    """SDK data deliberately keeps legacy-invalid values unvalidated."""
    return sdk.PortfolioPosition(instrument_uid='uid', instrument_type=kind,
                                 current_price=sdk.MoneyValue(currency, units, nano),
                                 quantity=sdk.Quotation(2, 0))


def test_browse_order_and_partial_output(client, caplog):
    """A later lookup failure keeps the first position but no premature total."""
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(
        positions=[position(), position('share'), position('etf')])
    client.instruments.find_instrument.side_effect = [sdk.FindInstrumentResponse(instruments=[
        sdk.InstrumentShort(name='Company', ticker='TICK')]),
        sdk.FindInstrumentResponse(instruments=[])]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'), \
            pytest.raises(reporting.GetInstrumentException):
        legacy_display.print_all_portfolio(client)
    assert caplog.messages == ['Selected (selected)', '------------', 'RUB - 2.0',
                               'Company(TICK) - 2.0 - RUB - 2.0']
    assert client.mock_calls == [call.users.get_accounts(),
                                 call.operations.get_portfolio(account_id='selected'),
                                 call.instruments.find_instrument(query='uid'),
                                 call.instruments.find_instrument(query='uid')]


@pytest.mark.parametrize('currency', [None, '', 'strange', 'rUb'])
@pytest.mark.parametrize('kind', ['currency', 'share', 'etf', 'bond', 'unknown'])
def test_legacy_currency_acceptance(client, currency, kind):
    """No currency guard is added, including the raw-only unsupported wrapper."""
    raw = position(kind, currency)
    if currency is None and kind in ('currency', 'share', 'etf'):
        with pytest.raises(TypeError):
            reporting.postiton_to_string(client, raw)
    else:
        text = reporting.postiton_to_string(client, raw)
        assert text == (str(raw) if kind in ('bond', 'unknown') else
                        ('' if kind == 'currency' else 'Company(TICK) - 2.0 - ') +
                        currency + ' - 2.0')
    assert client.mock_calls == ([call.instruments.find_instrument(query='uid')]
                                 if kind in ('share', 'etf') else [])


@pytest.mark.parametrize('kind', ['bond', 'unknown'])
def test_raw_compatibility_never_reads_numeric_fields(client, kind):
    """Unsupported compatibility input can have completely absent numerical data."""
    raw = sdk.PortfolioPosition(instrument_type=kind, current_price=None, quantity=None)
    assert reporting.postiton_to_string(client, raw) == str(raw)
    assert client.mock_calls == []


@pytest.mark.parametrize('kind', ['bond', 'unknown'])
@pytest.mark.parametrize('broken_values', [
    ('price_missing', AttributeError),
    ('price_invalid', InvalidOperation),
])
def test_fallback_preserves_price_error_before_quantity(client, caplog, kind, broken_values):
    """The old fallback total read price first, even when both inputs were malformed."""
    broken_part, expected_error = broken_values
    raw = position(kind)
    if broken_part == 'price_missing':
        raw.current_price = None
        raw.quantity.units = 'bad'
    else:
        raw.current_price.units = 'bad'
        raw.quantity = None
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[raw])
    with caplog.at_level(logging.INFO, logger='tinkoffBot'), pytest.raises(expected_error):
        legacy_display.print_portfolio_by_account(
            client, sdk.Account(id='selected', name='Selected'))
    assert caplog.messages == ['Selected (selected)', '------------']
    assert client.mock_calls == [call.operations.get_portfolio(account_id='selected')]


@pytest.mark.parametrize('stage', ['accounts', 'portfolio', 'lookup'])
def test_request_failure_is_once_with_full_diagnostic(client, caplog, stage):
    """Former direct reads acquire diagnostics without acquiring quota retries."""
    error = RequestError(StatusCode.RESOURCE_EXHAUSTED, 'complete server detail', ())
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(
        positions=[position('share')])
    method = {'accounts': client.users.get_accounts, 'portfolio': client.operations.get_portfolio,
              'lookup': client.instruments.find_instrument}[stage]
    method.side_effect = error
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'), \
            pytest.raises(RequestError) as caught:
        legacy_display.print_all_portfolio(client)
    assert caught.value is error
    assert method.call_count == 1
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].sdk_details == 'complete server detail'
    assert records[0].exc_info[1] is error
    assert not any('total:' in message for message in caplog.messages)


def _baseline_browse(client):
    """Fixed pre-C16 oracle, including conversion/evaluation and output order."""
    def decimal(value):
        return Decimal(value.units) + Decimal(value.nano) / Decimal('1000000000')

    def value(raw):
        return (decimal(raw.current_price) * decimal(raw.quantity)).quantize(Decimal('1e-9'))

    def money(raw):
        currency = raw.current_price.currency
        amount = value(raw)
        currency += ' - ' + format_decimal(amount)
        return currency

    for account in client.users.get_accounts().accounts:
        logger.log(IMPORTANT, '%s (%s)', account.name, account.id)
        logger.log(IMPORTANT, '------------')
        portfolio = client.operations.get_portfolio(account_id=account.id)
        total = Decimal('0')
        for raw in portfolio.positions:
            if raw.instrument_type == 'currency':
                logger.log(IMPORTANT, money(raw))
            elif raw.instrument_type in ('share', 'etf'):
                matches = client.instruments.find_instrument(query=raw.instrument_uid).instruments
                if len(matches) != 1:
                    raise reporting.GetInstrumentException('error get instrument')
                quantity = format_decimal(decimal(raw.quantity))
                label = matches[0].name + '(' + matches[0].ticker + ')'
                logger.log(IMPORTANT, label + ' - ' + quantity + ' - ' + money(raw))
            else:
                logger.debug('%s', raw)
                # Raw output moved to DEBUG, but old total errors still take price first.
                price = decimal(raw.current_price)
                quantity = decimal(raw.quantity)
                currency = raw.current_price.currency
                logger.info('Position: uid=%s type=%s quantity=%s currency=%s price=%s',
                            raw.instrument_uid, raw.instrument_type,
                            format_decimal(quantity), currency, format_decimal(price))
            total += value(raw)
        logger.log(IMPORTANT, 'total: %s', str(total))
        logger.log(IMPORTANT, '============')


def _run_observed_browse(client, raw, browse, caplog):
    """Use actual Runner.run ordering with the old/new browse and a foreign account."""
    client.operations.get_portfolio.side_effect = lambda *, account_id: sdk.PortfolioResponse(
        positions=[] if account_id == 'selected' else [raw])
    prepared = PreparedStrategy(lambda _: create_autospec(Strategy, instance=True, spec_set=True),
                                None, 'synthetic')
    with patch.object(runner_module, 'Client', autospec=True) as sdk_client, \
            patch.object(runner_module, 'AutoRepeater', autospec=True, spec_set=True) as engine, \
            patch.object(runner_module, 'configure_local_logging'), \
            patch.object(runner_module, 'print_all_portfolio', browse):
        sdk_client.return_value.__enter__.return_value = client
        runner = runner_module.Runner('synthetic-credential', prepared, 'selected')
        caplog.clear()
        client.reset_mock()
        caught = None
        try:
            runner.run()
        except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
            caught = (type(error), str(error))
        return (caught, list(caplog.messages), list(client.mock_calls),
                list(engine.return_value.mainflow.call_args_list))


@pytest.mark.parametrize('kind', ['currency', 'share', 'etf', 'bond', 'unknown'])
@pytest.mark.parametrize('currency', [None, '', 'unusual currency', 'rUb'])
@pytest.mark.parametrize('field, value', [
    ('units', 0), ('units', True), ('units', '1.25'), ('units', None),
    ('units', 'bad'), ('units', 'NaN'), ('units', 'Infinity'),
    ('nano', 1_000_000_000), ('nano', -1), ('nano', None),
    ('quantity.units', True), ('quantity.units', '1.25'), ('quantity.units', None),
    ('quantity.units', 'bad'), ('quantity.units', 'NaN'), ('quantity.units', 'Infinity'),
    ('quantity.nano', 1_000_000_000), ('quantity.nano', -1), ('quantity.nano', None),
    ('quantity', None), ('current_price', None),
])
def test_cli_foreign_account_parity(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        client, caplog, kind, currency, field, value):
    """Same legacy input, output, SDK reads, error and mainflow admission as before."""
    raw = position(kind, currency)
    if field in ('units', 'nano'):
        setattr(raw.current_price, field, value)
    elif field.startswith('quantity.'):
        setattr(raw.quantity, field.split('.')[1], value)
    else:
        setattr(raw, field, value)
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        baseline = _run_observed_browse(client, raw, _baseline_browse, caplog)
        actual = _run_observed_browse(client, raw, legacy_display.print_all_portfolio, caplog)
    assert actual == baseline
    assert actual[2][:3] == [call.users.get_accounts(),
                             call.operations.get_portfolio(account_id='selected'),
                             call.operations.get_portfolio(account_id='other')]
    client.orders.post_order.assert_not_called()
    client.orders.cancel_order.assert_not_called()


@pytest.mark.parametrize('kind', ['share', 'etf'])
def test_metadata_failure_precedes_broken_price(client, kind):
    """Formatting a malformed name still fails before loading numeric price parts."""
    client.instruments.find_instrument.return_value = sdk.FindInstrumentResponse(
        instruments=[sdk.InstrumentShort(name=None, ticker='TICK')])
    raw = position(kind)
    raw.current_price = None
    with pytest.raises(TypeError):
        reporting.postiton_to_string(client, raw)
    assert client.mock_calls == [call.instruments.find_instrument(query='uid')]


@pytest.mark.parametrize('accounts', [[], [sdk.Account(id='empty', name='Empty')]])
def test_empty_accounts_and_portfolios(client, accounts, caplog):
    """Empty displays retain their original output and need no metadata reads."""
    client.users.get_accounts.return_value = sdk.GetAccountsResponse(accounts=accounts)
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[])
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        legacy_display.print_all_portfolio(client)
    assert caplog.messages == ([] if not accounts else [
        'Empty (empty)', '------------', 'total: 0', '============'])
    assert client.mock_calls == ([call.users.get_accounts()] if not accounts else [
        call.users.get_accounts(), call.operations.get_portfolio(account_id='empty')])


def test_total_rounds_each_position_before_sum(client, caplog):
    """Sub-nano rounding belongs to each position rather than the final sum."""
    client.users.get_accounts.return_value = sdk.GetAccountsResponse(accounts=[
        sdk.Account(id='selected', name='Selected')])
    raw = position(units=0, nano=1)
    raw.quantity = sdk.Quotation(0, 600_000_000)
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[raw, raw])
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        legacy_display.print_all_portfolio(client)
    assert caplog.messages == ['Selected (selected)', '------------',
                               'RUB - 0.000000001', 'RUB - 0.000000001',
                               'total: 2E-9', '============']


@pytest.mark.parametrize('kind', ['currency', 'share', 'etf', 'bond', 'unknown'])
def test_total_rereads_values_after_position_output(client, caplog, kind):
    """Total uses raw values read after output rather than the displayed DTO."""
    raw = position(kind)
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(positions=[raw])
    print_position = reporting.print_display_position

    def print_and_update(display):
        print_position(display)
        raw.current_price = sdk.MoneyValue('RUB', 3, 0)
        raw.quantity = sdk.Quotation(4, 0)

    with caplog.at_level(logging.INFO, logger='tinkoffBot'), \
            patch.object(reporting, 'print_display_position', autospec=True,
                         side_effect=print_and_update):
        legacy_display.print_portfolio_by_account(
            client, sdk.Account(id='selected', name='Selected'))
    expected = ('RUB - 2.0' if kind == 'currency' else
                'Company(TICK) - 2.0 - RUB - 2.0' if kind in ('share', 'etf') else
                f'Position: uid=uid type={kind} quantity=2.0 currency=RUB price=1.0')
    assert caplog.messages == ['Selected (selected)', '------------', expected,
                               'total: 12.000000000', '============']
    assert client.mock_calls == [call.operations.get_portfolio(account_id='selected')] + (
        [call.instruments.find_instrument(query='uid')] if kind in ('share', 'etf') else [])


def test_late_portfolio_failure_keeps_completed_account_and_next_header(client, caplog):
    """A later SDK error adds full diagnostics after the same partial display."""
    error = RequestError(StatusCode.UNAVAILABLE, 'late multiline\nserver detail', ())
    response = sdk.PortfolioResponse(positions=[position()])
    client.operations.get_portfolio.side_effect = [response, error]
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'), \
            pytest.raises(RequestError) as caught:
        legacy_display.print_all_portfolio(client)
    assert caught.value is error
    assert [record.getMessage() for record in caplog.records if record.levelno == IMPORTANT] == [
        'Selected (selected)', '------------', 'RUB - 2.0', 'total: 2.000000000',
        '============', 'Other (other)', '------------']
    assert client.mock_calls == [call.users.get_accounts(),
                                 call.operations.get_portfolio(account_id='selected'),
                                 call.operations.get_portfolio(account_id='other')]
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].sdk_details == 'late multiline\nserver detail'
    assert records[0].exc_info[1] is error


def test_success_debug_contains_complete_received_responses(client, caplog):
    """All three former direct reads log the received SDK DTO before conversion."""
    client.users.get_accounts.return_value = sdk.GetAccountsResponse(accounts=[
        sdk.Account(id='selected', name='Selected')])
    client.operations.get_portfolio.return_value = sdk.PortfolioResponse(
        positions=[position('share')])
    expected = [client.users.get_accounts.return_value,
                client.operations.get_portfolio.return_value,
                client.instruments.find_instrument.return_value]
    with caplog.at_level(logging.DEBUG, logger='tinkoffBot'):
        legacy_display.print_all_portfolio(client)
    records = [record for record in caplog.records
               if record.getMessage().startswith('SDK response:')]
    assert [record.getMessage() for record in records] == [
        f'SDK response: method=MagicMock response={response!r}' for response in expected]
    assert all(record.levelno == logging.DEBUG for record in records)
