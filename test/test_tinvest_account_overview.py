"""An independent, bounded account browse never supplies trading facts."""
from decimal import Decimal
import logging
import inspect
from types import SimpleNamespace
from unittest.mock import call, create_autospec, patch

import pytest
from grpc import StatusCode
from t_tech.invest import (
    Account, FindInstrumentResponse, GetAccountsResponse, InstrumentShort,
    MoneyValue, PortfolioPosition, PortfolioResponse, Quotation, RequestError,
)
from t_tech.invest.services import InstrumentsService, OperationsService, Services, UsersService

from autorepeater import reporting, runner as runner_module
from autorepeater import tinvest_account_overview as overview
from autorepeater.cash_facts import MoneyAmount
from autorepeater.display_data import GetInstrumentException
from autorepeater.execution import OrderExecutor
from autorepeater.execution_data import AccountReadiness, ExecutionData
from autorepeater.repeater import AutoRepeater
from autorepeater.strategy_contract import Strategy
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.tinvest_strategy_data import TInvestStrategyData


@pytest.fixture(name='client')
def fixture_client():
    """Use concrete service signatures, including forbidden unconfigured methods."""
    services = object.__new__(inspect.unwrap(Services))
    services.users = None
    services.operations = None
    services.instruments = None
    result = create_autospec(services, instance=True, spec_set=True)
    result.users = create_autospec(inspect.unwrap(UsersService), instance=True, spec_set=True)
    result.operations = create_autospec(
        inspect.unwrap(OperationsService), instance=True, spec_set=True)
    result.instruments = create_autospec(
        inspect.unwrap(InstrumentsService), instance=True, spec_set=True)
    result.users.get_accounts.return_value = GetAccountsResponse(
        accounts=[Account(id='a', name='First'), Account(id='b', name='Second')])
    result.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=[
        InstrumentShort(uid='uid', name='Company', ticker='TICK',
                        instrument_type='share', class_code='TQBR')])
    return result


def position(kind='share', uid='uid'):
    """Position quote is deliberately unrelated to the broker total."""
    return PortfolioPosition(instrument_uid=uid, instrument_type=kind,
                             current_price=MoneyValue('rUb', 99, 0), quantity=Quotation(2, 1))


def response(*positions, total=None):
    """Explicit None exercises absent total rather than SDK unset defaults."""
    return PortfolioResponse(positions=list(positions), total_amount_portfolio=total)


def messages(caplog):
    """Separate raw DEBUG diagnostics from useful output."""
    return [record.getMessage() for record in caplog.records if record.levelno > logging.DEBUG]


def test_snapshot_metadata_cache_and_currency_totals(client, caplog):
    """Only own snapshots, loaded metadata and broker MoneyValue reach rendering."""
    raw = position()
    client.operations.get_portfolio.side_effect = [
        response(raw, position('currency', 'cash'), total=MoneyValue('USD', 7, 0)),
        response(raw, position('bond'), total=MoneyValue('RUB', 10, 1))]
    with patch.object(reporting, 'print_display_portfolio',
                      wraps=reporting.print_display_portfolio) as render, \
            caplog.at_level(logging.DEBUG, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert client.mock_calls == [call.users.get_accounts(),
                                 call.operations.get_portfolio(account_id='a'),
                                 call.instruments.find_instrument(query='uid'),
                                 call.operations.get_portfolio(account_id='b')]
    snapshot, metadata, total = render.call_args_list[0].args
    assert isinstance(snapshot, PortfolioSnapshot)
    assert metadata['uid'].name == 'Company'
    assert total == MoneyAmount('usd', Decimal(7))
    raw.quantity.units = 100
    assert snapshot.positions[0].quantity == Decimal('2.000000001')
    output = messages(caplog)
    assert 'Company(TICK) - 2.000000001 - rUb - 198.000000099' in output
    assert 'broker portfolio total: 7.0 usd' in output
    assert 'broker portfolio totals: 7.0 usd' in output
    assert 'broker portfolio totals: 10.000000001 rub' in output
    bond = next(line for line in output if 'type=bond' in line)
    assert 'quantity=2.000000001' in bond and 'broker quote=99.0' in bond and '198' not in bond


@pytest.mark.parametrize('total', [None, MoneyValue('', 0, 0), MoneyValue('USD', True, 0),
                                   MoneyValue('USD', 1, -1), MoneyValue('USD', 0, 10**9)])
def test_invalid_total_keeps_positions_and_marks_sums_incomplete(client, caplog, total):
    """Missing or damaged broker money never becomes an inferred numerical total."""
    client.operations.get_portfolio.side_effect = [
        response(position(), total=total), response(total=MoneyValue('USD', 4, 0))]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    output = messages(caplog)
    assert any('Company(TICK)' in line for line in output)
    assert 'broker portfolio total unavailable' in output
    assert 'incomplete subtotal: 4.0 usd; unavailable accounts=1' in output
    assert not any('broker portfolio totals:' in line for line in output)


def test_true_zero_and_exact_same_currency_sum(client, caplog):
    """Explicit zero is money, and tiny residuals survive large grouped sums."""
    client.operations.get_portfolio.side_effect = [
        response(total=MoneyValue('USD', 0, 0)),
        response(total=MoneyValue('usd', 999999999999999999, 1))]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert 'broker portfolio total: 0.0 usd' in messages(caplog)
    assert 'broker portfolio totals: 999999999999999999.000000001 usd' in messages(caplog)


@pytest.mark.parametrize('failure', [
    RequestError(StatusCode.RESOURCE_EXHAUSTED, 'quota original details ' * 1000, ()),
    ValueError('bad quantity')])
def test_account_failure_is_bounded_atomic_and_continues(client, caplog, failure):
    """Expected failure consumes one attempt and cannot erase later proven totals."""
    client.operations.get_portfolio.side_effect = [failure, response(total=MoneyValue('RUB', 8, 0))]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert client.operations.get_portfolio.call_count == 2
    client.instruments.find_instrument.assert_not_called()
    assert 'portfolio unavailable: account=a' in messages(caplog)
    assert 'incomplete subtotal: 8.0 rub; unavailable accounts=1' in messages(caplog)
    if isinstance(failure, RequestError):
        record = next(record for record in caplog.records if record.exc_info)
        assert record.exc_info[1] is failure and record.sdk_details == failure.details


def test_bad_position_discards_account_before_metadata_or_rendering(client, caplog):
    """A malformed later position cannot leave a misleading partial account."""
    bad = position()
    bad.quantity = SimpleNamespace(units=True, nano=0)
    client.operations.get_portfolio.side_effect = [response(position(), bad), response()]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert 'portfolio unavailable: account=a' in messages(caplog)
    assert not any('Company' in line for line in messages(caplog))
    client.instruments.find_instrument.assert_not_called()
    assert 'broker portfolio totals unavailable; unavailable accounts=2' in messages(caplog)


@pytest.mark.parametrize('matches', [
    [], [InstrumentShort(uid='other')], [InstrumentShort(uid='uid')] * 2,
    [InstrumentShort(uid='uid', name=None, ticker='T')],
    [InstrumentShort(uid='uid', name='N', ticker=3)]])
def test_metadata_rejection_is_cached_without_losing_totals(client, caplog, matches):
    """Wrong, duplicate or damaged metadata keeps numeric positions with UID fallback."""
    client.operations.get_portfolio.side_effect = [
        response(position(), total=MoneyValue('RUB', 1, 0))] * 2
    client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=matches)
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    client.instruments.find_instrument.assert_called_once_with(query='uid')
    assert messages(caplog).count('metadata unavailable: uid=uid') == 2
    assert 'uid - 2.000000001 - rUb - 198.000000099' in messages(caplog)
    assert 'broker portfolio totals: 2.0 rub' in messages(caplog)


def test_metadata_request_error_is_full_and_cached(client, caplog):
    """An unavailable name is diagnosed once and never invalidates broker money."""
    failure = RequestError(StatusCode.UNAVAILABLE, 'metadata full details ' * 1000, ())
    client.instruments.find_instrument.side_effect = failure
    client.operations.get_portfolio.side_effect = [
        response(position(), total=MoneyValue('EUR', 1, 0))] * 2
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    client.instruments.find_instrument.assert_called_once_with(query='uid')
    assert [record.sdk_details for record in caplog.records if record.exc_info] == [failure.details]
    assert 'broker portfolio totals: 2.0 eur' in messages(caplog)


@pytest.mark.parametrize('empty', [False, True])
def test_accounts_failure_or_empty_never_fabricates_money(client, caplog, empty):
    """Unknown accounts and an empty account list are distinct nonmonetary outcomes."""
    if empty:
        client.users.get_accounts.return_value = GetAccountsResponse(accounts=[])
    else:
        client.users.get_accounts.side_effect = RequestError(
            StatusCode.UNAVAILABLE, 'full accounts', ())
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    client.operations.get_portfolio.assert_not_called()
    output = messages(caplog)
    assert ('Overview: no accounts' if empty else 'overview unavailable') in output
    assert not any('0.0' in line or 'subtotal' in line for line in output)


def test_programming_and_render_errors_escape(client):
    """The read boundary never hides programming errors or wraps rendering."""
    client.users.get_accounts.side_effect = TypeError('programming')
    with pytest.raises(TypeError, match='programming'):
        overview.print_all_portfolio(client)
    client.users.get_accounts.side_effect = None
    client.operations.get_portfolio.return_value = response()
    with patch.object(reporting, 'print_display_portfolio', side_effect=ValueError('render bug')):
        with pytest.raises(ValueError, match='render bug'):
            overview.print_all_portfolio(client)


def test_compatibility_exact_uid_numbers_currency_error_and_raw_fallback(client):
    """The public typo, exception identity and useful strings survive without legacy rules."""
    assert reporting.GetInstrumentException is GetInstrumentException
    signature = inspect.signature(reporting.postiton_to_string)
    assert tuple(signature.parameters) == ('client', 'position')
    client.instruments.find_instrument.return_value.instruments.insert(
        0, InstrumentShort(uid='wrong'))
    assert reporting.postiton_to_string(client, position()) == \
        'Company(TICK) - 2.000000001 - rUb - 198.000000099'
    client.instruments.find_instrument.return_value = FindInstrumentResponse(instruments=[])
    with pytest.raises(GetInstrumentException):
        reporting.postiton_to_string(client, position())
    failure = RequestError(StatusCode.UNAVAILABLE, 'original', ())
    client.instruments.find_instrument.side_effect = failure
    with pytest.raises(RequestError) as raised:
        reporting.postiton_to_string(client, position())
    assert raised.value is failure
    bad = position('currency')
    bad.quantity = SimpleNamespace(units=True, nano=0)
    with pytest.raises(ValueError, match='quantity.units is invalid'):
        reporting.postiton_to_string(client, bad)
    bad.quantity = Quotation(1, 0)
    bad.current_price.currency = ''
    with pytest.raises(ValueError, match='invalid monetary currency'):
        reporting.postiton_to_string(client, bad)
    fallback = PortfolioPosition(instrument_type='bond')
    client.reset_mock()
    assert reporting.postiton_to_string(client, fallback) == str(fallback)
    assert not client.mock_calls


@pytest.mark.parametrize('failed_account', ['foreign', 'selected'])
def test_runner_overview_failure_then_fresh_selected_failure(client, failed_account, caplog):
    """The browse does not gate mainflow, whose independent failure still propagates."""
    from test.test_order_plan import destination  # pylint: disable=import-outside-toplevel

    client.users.get_accounts.return_value.accounts[0].id = failed_account
    failure = RequestError(StatusCode.UNAVAILABLE, 'fresh selected account', ())
    client.operations.get_portfolio.side_effect = [ValueError('foreign'), response(), failure]
    runner = object.__new__(runner_module.Runner)
    runner.dst = 'selected'
    strategy = create_autospec(Strategy, instance=True, spec_set=True)
    strategy.load_snapshot.side_effect = lambda data: data.get_portfolio('selected')
    strategy.event_accounts.return_value = ('selected',)
    execution = create_autospec(ExecutionData, instance=True, spec_set=True)
    execution.get_readiness.return_value = AccountReadiness(False, False, False, 0)
    execution.get_destination.return_value = destination()
    executor = create_autospec(OrderExecutor, instance=True, spec_set=True)
    data = TInvestStrategyData(client)
    engine = AutoRepeater(strategy, data, execution, executor)
    with patch.object(runner, '_create_client') as factory, \
            patch.object(runner, '_create_repeater', return_value=engine), \
            patch.object(data, 'position_events',
                         side_effect=RuntimeError('end test after pass')), \
            caplog.at_level(logging.INFO, logger='tinkoffBot'):
        factory.return_value.__enter__.return_value = client
        with pytest.raises(RuntimeError, match='end test after pass'):
            runner.run()
    assert client.operations.get_portfolio.call_args_list == [
        call(account_id=failed_account), call(account_id='b'), call(account_id='selected')]
    assert any('Current pass stopped for destination selected' in line for line in caplog.messages)
    strategy.allocation_profile.assert_not_called()
    execution.get_trade_rules.assert_not_called()
    assert not executor.mock_calls


class ReadOnceValue:  # pylint: disable=too-few-public-methods
    """A source scalar that fails if rendering tries another units/nano conversion."""

    currency = 'RUB'

    def __init__(self):
        self.reads = []

    @property
    def units(self):
        """Each component must be detached only once."""
        assert 'units' not in self.reads
        self.reads.append('units')
        return 2

    @property
    def nano(self):
        """Each component must be detached only once."""
        assert 'nano' not in self.reads
        self.reads.append('nano')
        return 1


@pytest.mark.parametrize('level', [logging.INFO, logging.DEBUG])
def test_raw_detachment_once_complete_load_and_lazy_success_repr(client, caplog, level):
    """The formatter sees stable Decimals after reads and metadata, never live raw fields."""
    raw = SimpleNamespace(instrument_uid='uid', instrument_type='share',
                          current_price=ReadOnceValue(), quantity=ReadOnceValue(),
                          blocked=None, blocked_lots=None)
    loaded = response(raw, total=MoneyValue('RUB', 7, 0))
    accounts = client.users.get_accounts.return_value
    accounts.accounts = accounts.accounts[:1]
    client.operations.get_portfolio.return_value = loaded
    rendered = []

    def render(snapshot, metadata, total):
        assert client.mock_calls == [call.users.get_accounts(),
                                     call.operations.get_portfolio(account_id='a'),
                                     call.instruments.find_instrument(query='uid')]
        assert raw.current_price.reads == raw.quantity.reads == ['units', 'nano']
        assert metadata['uid'].ticker == 'TICK' and total.amount == Decimal(7)
        rendered.append(reporting.display_position_to_string(snapshot.positions[0], metadata))

    with patch.object(reporting, 'print_display_portfolio', side_effect=render), \
            patch.object(overview, 'portfolio_entry', wraps=overview.portfolio_entry) as convert, \
            patch.object(overview, 'instrument_matches',
                         wraps=overview.instrument_matches) as matches, \
            caplog.at_level(level, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert len(rendered) == convert.call_count == matches.call_count == 1
    successes = [item for item in caplog.messages if item.startswith('SDK response:')]
    assert len(successes) == (3 if level == logging.DEBUG else 0)
    if successes:
        assert f'response={loaded!r}' in successes[1]


def test_all_failed_portfolios_never_print_zero_or_money(client, caplog):
    """No successful total means no numerical subtotal of any currency."""
    client.operations.get_portfolio.side_effect = RequestError(
        StatusCode.UNAVAILABLE, 'all failed', ())
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    assert client.operations.get_portfolio.call_count == 2
    assert 'broker portfolio totals unavailable; unavailable accounts=2' in messages(caplog)
    assert not any('0.0' in item for item in messages(caplog))


def test_missing_account_marks_every_known_currency_incomplete(client, caplog):
    """A single missing total qualifies every subtotal, without mixing currencies."""
    client.users.get_accounts.return_value.accounts.append(Account(id='c', name='Third'))
    client.operations.get_portfolio.side_effect = [
        response(total=MoneyValue('RUB', 3, 0)),
        response(total=MoneyValue('USD', 4, 0)), response()]
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    output = messages(caplog)
    assert 'incomplete subtotal: 3.0 rub; unavailable accounts=1' in output
    assert 'incomplete subtotal: 4.0 usd; unavailable accounts=1' in output
    assert not any('broker portfolio totals:' in item for item in output)


def test_metadata_value_error_and_empty_uid_are_explicit_without_retries(client, caplog):
    """Expected search conversion failure is cached; an empty UID is never queried."""
    client.instruments.find_instrument.side_effect = ValueError('metadata conversion')
    client.operations.get_portfolio.side_effect = [
        response(position(), position('other', ''), total=MoneyValue('USD', 5, 0))] * 2
    with caplog.at_level(logging.INFO, logger='tinkoffBot'):
        overview.print_all_portfolio(client)
    client.instruments.find_instrument.assert_called_once_with(query='uid')
    assert messages(caplog).count('metadata unavailable: uid=') == 2
    assert 'broker portfolio totals: 10.0 usd' in messages(caplog)
