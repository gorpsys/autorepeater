"""Offline regressions for the disposable USD portfolio experiment."""
from decimal import Decimal as D
import inspect
from types import SimpleNamespace
from unittest.mock import create_autospec, patch
from test import sandbox_portfolio_live as probe

import pytest
from t_tech.invest import MoneyValue, PortfolioPosition, Quotation
from t_tech.invest.services import SandboxService, UsersService

from autorepeater.strategy_data import InstrumentInfo, InstrumentMatch, InstrumentType, StrategyData
from scripts.sandbox_lifecycle import SandboxFailure, SandboxLifecycle
from scripts.sandbox_support import SandboxSession


def position(uid='usd', currency='usd', quantity=1):
    """Only SDK portfolio facts, never credentials or complete response dumps."""
    return PortfolioPosition(
        instrument_uid=uid, instrument_type='share',
        current_price=MoneyValue(currency=currency, units=100, nano=123),
        quantity=Quotation(units=quantity, nano=0))


def test_snapshot_keeps_currencies_and_duplicate_uid_facts():
    """A list is retained instead of last-write-wins indexing by UID."""
    response = SimpleNamespace(
        positions=[position(), position(quantity=2), position('rub', 'rub')],
        total_amount_portfolio=MoneyValue(currency='rub', units=30300, nano=0))
    result = probe.snapshot_facts(response, 'mixed', 'RUB')
    assert result['duplicate_uids'] == ['usd']
    assert result['quantity_by_uid'] == {'usd': '3', 'rub': '1'}
    assert result['price_currencies'] == ['rub', 'usd']
    assert result['positions'][0]['price'] == '100.000000123'
    assert 'account_id' not in result


@pytest.mark.parametrize('bad', ['', None, 'token-looking-invalid-currency'])
def test_snapshot_rejects_bad_currency_without_echoing_it(bad):
    """Provider values cannot become error messages."""
    with pytest.raises(SandboxFailure, match='invalid portfolio currency') as error:
        probe.snapshot_facts(SimpleNamespace(positions=[position(currency=bad)]), 'mixed', 'RUB')
    assert 'token-looking' not in str(error.value)


def test_summary_distinguishes_native_prices_from_requested_totals():
    """Requested valuation currency need not be the security price currency."""
    records = [probe.snapshot_facts(SimpleNamespace(positions=[position()]), 'usd_first', mode)
               for mode in ('DEFAULT', 'RUB', 'USD', 'EUR')]
    assert probe.price_behavior(records, {'usd': 'usd'}) == 'instrument_currency'
    assert probe.price_behavior([], {'usd': 'usd'}) == 'inconclusive'


def test_summary_detects_converted_prices():
    """The experiment also accepts genuinely converted per-security prices."""
    records = [
        probe.snapshot_facts(SimpleNamespace(positions=[position(currency=currency)]),
                             'usd_first', mode)
        for mode, currency in [('RUB', 'rub'), ('USD', 'usd'), ('EUR', 'eur')]]
    assert probe.price_behavior(records, {'usd': 'usd'}) == 'request_currency'


def test_usd_selection_never_falls_back_to_rub_or_partial_ticker():
    """Ticker suffixes and RUB variants cannot make a USD experiment pass."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = [
        InstrumentMatch('partial', 'AAPLX', '', InstrumentType.SHARE, ''),
        InstrumentMatch('rub', 'AAPL', '', InstrumentType.SHARE, ''),
        InstrumentMatch('usd', 'AAPL', '', InstrumentType.SHARE, ''),
    ]
    rub = InstrumentInfo('rub', 'AAPL', '', InstrumentType.SHARE, '', 1, 'rub', True)
    usd = InstrumentInfo('usd', 'AAPL', '', InstrumentType.SHARE, '', 1, 'usd', True)
    data.get_instrument.side_effect = [rub, usd]
    report = {'candidates': []}
    assert probe.resolve_instrument(data, 'AAPL', 'usd', report) == usd
    assert data.get_instrument.call_count == 2
    assert [item['currency'] for item in report['candidates']] == ['rub', 'usd']


def test_missing_usd_instrument_is_explicit_failure():
    """Unavailable USD instruments do not turn into a skipped successful experiment."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = []
    with pytest.raises(SandboxFailure, match='no API-available usd instrument for AAPL'):
        probe.resolve_instrument(data, 'AAPL', 'usd', {'candidates': []})


def test_live_entrypoint_refuses_missing_sandbox_token(monkeypatch, capsys):
    """No production credential fallback is accepted."""
    monkeypatch.delenv('SANDBOX_TOKEN', raising=False)
    assert probe.main() == 2
    assert 'SANDBOX_TOKEN is required' in capsys.readouterr().err


def test_decimal_conversion_does_not_round_nano():
    """Small fractional facts survive serialization."""
    assert probe.quantity(Quotation(units=7, nano=123)) == D('7.000000123')


@pytest.mark.parametrize('mode,number', [('RUB', 0), ('USD', 1), ('EUR', 2)])
def test_explicit_currency_reaches_rpc_with_finite_timeout(mode, number):
    """The pinned wrapper's missing parameter is bypassed only on its existing channel."""
    def rpc(request, *, metadata, timeout):
        return request, metadata, timeout

    method = create_autospec(rpc, spec_set=True)
    method.side_effect = rpc
    client = SimpleNamespace(operations=SimpleNamespace(
        stub=SimpleNamespace(GetPortfolio=method), metadata=(('authorization', 'offline'),)))
    request, metadata, timeout = probe.read_portfolio(client, 'disposable', mode)
    assert request.account_id == 'disposable' and request.currency == number
    assert metadata == (('authorization', 'offline'),) and timeout == 10
    assert method.call_count == 1


def test_default_currency_uses_unmodified_sdk_wrapper():
    """DEFAULT is a separate observation, not an alias for explicitly requested RUB."""
    def get_portfolio(*, account_id):
        return account_id

    method = create_autospec(get_portfolio, spec_set=True)
    method.return_value = 'snapshot'
    client = SimpleNamespace(operations=SimpleNamespace(get_portfolio=method))
    assert probe.read_portfolio(client, 'disposable', 'DEFAULT') == 'snapshot'
    method.assert_called_once_with(account_id='disposable')


def test_cash_rows_do_not_require_a_security_uid():
    """Currency holdings are excluded from ACCOUNT, so record them separately."""
    cash = PortfolioPosition(instrument_type='currency', instrument_uid='',
                             current_price=MoneyValue(currency='rub', units=1, nano=0),
                             quantity=Quotation(units=12, nano=0))
    result = probe.snapshot_facts(SimpleNamespace(positions=[cash]), 'mixed', 'RUB')
    assert not result['positions'] and not result['duplicate_uids']
    assert result['currency_positions'] == [{'quantity': '12', 'price': '1',
                                            'price_currency': 'rub'}]


@pytest.mark.parametrize('bad', [SimpleNamespace(units=1, nano=1000000000),
                                 SimpleNamespace(units=True, nano=0)])
def test_bad_quantities_are_not_coerced(bad):
    """Invalid typed facts stop observation instead of being normalized."""
    with pytest.raises(SandboxFailure):
        probe.quantity(bad)


def test_failure_still_writes_sanitized_evidence_and_closes_accounts(tmp_path, monkeypatch):
    """The actual live test finalizer runs for failed setup too."""
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    lifecycle = create_autospec(SandboxLifecycle, instance=True, spec_set=True)
    session = SimpleNamespace(lifecycle=lifecycle)
    with patch.object(probe, 'run_probe', side_effect=SandboxFailure('synthetic secret')):
        with pytest.raises(SandboxFailure):
            probe.test_sandbox_portfolio_currency('AAPL', session)
    text = (tmp_path / 'AAPL.json').read_text(encoding='utf-8')
    assert 'synthetic secret' not in text and '"cleanup_confirmed": true' in text
    lifecycle.cleanup.assert_called_once_with()


def test_cleanup_failure_cannot_be_reported_as_success(tmp_path, monkeypatch):
    """Failure artifacts distinguish completed observation from incomplete cleanup."""
    monkeypatch.setenv('E2E_ARTIFACT_DIR', str(tmp_path))
    lifecycle = create_autospec(SandboxLifecycle, instance=True, spec_set=True)
    lifecycle.cleanup.side_effect = SandboxFailure('cleanup failed')
    with patch.object(probe, 'run_probe'):
        with pytest.raises(SandboxFailure, match='cleanup failed'):
            probe.test_sandbox_portfolio_currency('MSFT', SimpleNamespace(lifecycle=lifecycle))
    assert '"cleanup_confirmed": false' in (tmp_path / 'MSFT.json').read_text(encoding='utf-8')


def test_probe_uses_currency_funding_and_repeated_usd_purchase():
    """The real experiment sequence creates only owned accounts and sandbox cash."""
    sandbox = create_autospec(inspect.unwrap(SandboxService), instance=True, spec_set=True)
    users = create_autospec(inspect.unwrap(UsersService), instance=True, spec_set=True)
    lifecycle = create_autospec(SandboxLifecycle(sandbox, users, 'local', 'a' * 32),
                                instance=True, spec_set=True)
    lifecycle.created = []
    lifecycle.create.return_value = 'disposable'
    session = SandboxSession(SimpleNamespace(sandbox=sandbox), lifecycle)
    dollar = InstrumentInfo('usd', 'AAPL', '', InstrumentType.SHARE, '', 1, 'usd', True)
    ruble = InstrumentInfo('rub', 'SBER', '', InstrumentType.SHARE, '', 10, 'rub', True)
    report = {'ticker': 'AAPL', 'observations': []}
    with patch.object(probe, 'resolve_bond', return_value=dollar), patch.object(
            probe, 'resolve_instrument', return_value=ruble), patch.object(
            probe, 'trading_facts', return_value={}), patch.object(
            probe, 'buy_one_lot') as buy, patch.object(probe, 'capture_phase') as capture:
        probe.run_probe(session, 'AAPL', report)
    assert sandbox.sandbox_pay_in.call_count == 2
    assert [call.kwargs['amount'].currency for call in sandbox.sandbox_pay_in.call_args_list] == [
        'usd', 'rub']
    assert [call.args[2] for call in buy.call_args_list] == [dollar, ruble, dollar]
    assert [call.args[3] for call in capture.call_args_list] == ['usd_first', 'mixed', 'usd_second']
    assert report['status'] == 'observed'
    lifecycle.verify_name.assert_called_once_with('disposable')


def test_unsupported_usd_funding_is_recorded_without_retry(capsys):
    """Only the definite provider rejection permits a RUB-only experiment."""
    sandbox = create_autospec(inspect.unwrap(SandboxService), instance=True, spec_set=True)
    sandbox.sandbox_pay_in.side_effect = SandboxFailure('rejected', api_facts=[{
        'type': 'RequestError', 'grpc_status': 'INVALID_ARGUMENT', 'api_code': '30258'}])
    report = {}
    session = SimpleNamespace(client=SimpleNamespace(sandbox=sandbox))
    probe.fund_account(session, 'disposable', 'usd', 100000, report)
    assert report['funding'][0]['outcome'] == 'rejected_currency_unavailable'
    assert 'WARNING' in capsys.readouterr().out
    assert sandbox.sandbox_pay_in.call_count == 1


def test_bond_discovery_distinguishes_nominal_from_settlement():
    """A USD nominal does not make a RUB-settled bond a USD-priced instrument."""
    from t_tech.invest import Bond  # pylint: disable=import-outside-toplevel
    from t_tech.invest.services import InstrumentsService  # pylint: disable=import-outside-toplevel
    service = create_autospec(inspect.unwrap(InstrumentsService), instance=True, spec_set=True)
    service.bonds.return_value = SimpleNamespace(instruments=[
        Bond(uid='bond', ticker='RU000BOND', name='Replacement bond', class_code='TQCB',
             nominal=MoneyValue(currency='usd', units=1000, nano=0), currency='rub', lot=1,
             api_trade_available_flag=True, buy_available_flag=True),
        Bond(uid='rub', nominal=MoneyValue(currency='rub', units=1000, nano=0))])
    report = {'candidates': []}
    status = {'api_trade_available_flag': True, 'bestprice_order_available_flag': True}
    with patch.object(probe, 'trading_facts', return_value=status):
        result = probe.resolve_bond(SimpleNamespace(instruments=service), 'BOND1', report)
    assert result.uid == 'bond' and result.currency == 'rub'
    assert report['candidates'][0]['nominal_currency'] == 'usd'
    assert report['candidates'][0]['currency'] == 'rub'
    service.bonds.assert_called_once_with()


def test_bond_discovery_refuses_closed_candidates():
    """Read-only discovery fails before opening or funding accounts."""
    from t_tech.invest import Bond  # pylint: disable=import-outside-toplevel
    client = SimpleNamespace(instruments=SimpleNamespace(bonds=lambda: SimpleNamespace(
        instruments=[Bond(uid='bond', ticker='BOND', name='Bond', class_code='TQCB',
                          nominal=MoneyValue(currency='usd', units=1000, nano=0),
                          api_trade_available_flag=True, buy_available_flag=True, lot=1)])))
    with patch.object(probe, 'trading_facts', return_value={
            'api_trade_available_flag': True, 'bestprice_order_available_flag': False}):
        with pytest.raises(SandboxFailure, match='no currently available USD-nominal bond'):
            probe.resolve_bond(client, 'BOND1', {'candidates': []})


@pytest.mark.parametrize('slot,expected', [
    ('BOND1', 'RU000A1056U0'), ('BOND2', 'RU000A1059P4')])
def test_replacement_bonds_take_precedence_over_other_usd_nominal_bonds(slot, expected):
    """Both replacement issues are checked before the bounded generic catalog scan."""
    from t_tech.invest import Bond  # pylint: disable=import-outside-toplevel
    from t_tech.invest.services import InstrumentsService  # pylint: disable=import-outside-toplevel
    service = create_autospec(inspect.unwrap(InstrumentsService), instance=True, spec_set=True)
    service.bonds.return_value = SimpleNamespace(instruments=[
        Bond(uid=ticker, ticker=ticker, name='Bond', class_code='TQCB',
             nominal=MoneyValue(currency='usd', units=1000, nano=0), currency='rub', lot=1,
             api_trade_available_flag=True, buy_available_flag=True)
        for ticker in ('AAA', 'RU000A1059P4', 'RU000A1056U0')])
    with patch.object(probe, 'trading_facts', return_value={
            'api_trade_available_flag': True, 'bestprice_order_available_flag': True}):
        report = {'candidates': []}
        result = probe.resolve_bond(SimpleNamespace(instruments=service), slot, report)
    assert result.ticker == expected
    assert [item['ticker'] for item in report['candidates'][:2]] == list(probe.PREFERRED_BONDS)


def test_normal_trading_bond_can_use_limit_instead_of_bestprice():
    """Replacement bonds need not support BESTPRICE to support a limit-order probe."""
    from t_tech.invest import Bond  # pylint: disable=import-outside-toplevel
    from t_tech.invest.services import InstrumentsService  # pylint: disable=import-outside-toplevel
    service = create_autospec(inspect.unwrap(InstrumentsService), instance=True, spec_set=True)
    service.bonds.return_value = SimpleNamespace(instruments=[
        Bond(uid='bond', ticker='RU000A1056U0', name='Bond', class_code='TQCB', lot=1,
             nominal=MoneyValue(currency='usd', units=1000, nano=0), currency='rub',
             api_trade_available_flag=True, buy_available_flag=True)])
    report = {'candidates': []}
    with patch.object(probe, 'trading_facts', return_value={
            'status': 'SECURITY_TRADING_STATUS_NORMAL_TRADING',
            'api_trade_available_flag': True, 'bestprice_order_available_flag': False}):
        probe.resolve_bond(SimpleNamespace(instruments=service), 'BOND1', report)
    assert report['bond_order_types'] == {'bond': 'LIMIT'}


@pytest.mark.parametrize('has_ask', [True, False])
def test_limit_purchase_uses_ask_and_never_submits_without_one(has_ask):
    """Use the broker's price directly, with at most one submission."""
    from t_tech.invest import (  # pylint: disable=import-outside-toplevel
        OrderDirection, OrderType, PostOrderResponse, Order, PriceType)
    from t_tech.invest.services import MarketDataService, OrdersService  # pylint: disable=import-outside-toplevel
    market = create_autospec(inspect.unwrap(MarketDataService), instance=True, spec_set=True)
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    price = Quotation(units=99, nano=500000000)
    market.get_order_book.return_value = SimpleNamespace(
        asks=[Order(price=price, quantity=1)] if has_ask else [])
    orders.post_order.return_value = PostOrderResponse(
        order_id='broker', execution_report_status=probe.FILL)
    session = create_autospec(SandboxSession(
        SimpleNamespace(market_data=market, orders=orders), SimpleNamespace()),
        instance=True, spec_set=True)
    session.client = SimpleNamespace(market_data=market, orders=orders)
    session.order_id_factory = lambda: 'request'
    session.monotonic = lambda: 0
    instrument = InstrumentInfo('bond', 'BOND', '', InstrumentType.OTHER, 'TQCB', 1, 'rub', True)
    report = {'bond_order_types': {'bond': 'LIMIT'}, 'orders': []}
    if not has_ask:
        with pytest.raises(SandboxFailure, match='bond order book has no positive ask'):
            probe.buy_one_lot(session, 'disposable', instrument, report)
        orders.post_order.assert_not_called()
        return
    probe.buy_one_lot(session, 'disposable', instrument, report)
    orders.post_order.assert_called_once_with(
        account_id='disposable', instrument_id='bond', quantity=1,
        direction=OrderDirection.ORDER_DIRECTION_BUY, order_type=OrderType.ORDER_TYPE_LIMIT,
        order_id='request', price=price, price_type=PriceType.PRICE_TYPE_POINT)


def test_uncertain_funding_is_not_retried_or_ignored():
    """A timeout may have mutated the account and cannot permit blind retries."""
    sandbox = create_autospec(inspect.unwrap(SandboxService), instance=True, spec_set=True)
    sandbox.sandbox_pay_in.side_effect = SandboxFailure('timeout', api_facts=[{
        'type': 'RequestError', 'grpc_status': 'DEADLINE_EXCEEDED'}])
    with pytest.raises(SandboxFailure):
        probe.fund_account(SimpleNamespace(client=SimpleNamespace(sandbox=sandbox)),
                           'disposable', 'usd', 100000, {})
    assert sandbox.sandbox_pay_in.call_count == 1


def test_mixed_selection_requires_foreign_settlement_not_foreign_nominal():
    """RUB-settled replacement bonds cannot satisfy the mixed-price experiment."""
    from t_tech.invest import Bond  # pylint: disable=import-outside-toplevel
    from t_tech.invest.services import InstrumentsService  # pylint: disable=import-outside-toplevel
    service = create_autospec(inspect.unwrap(InstrumentsService), instance=True, spec_set=True)
    service.bonds.return_value = SimpleNamespace(instruments=[
        Bond(uid=currency, ticker=currency, name='Bond', class_code='TQCB', lot=1,
             nominal=MoneyValue(currency=nominal, units=1000, nano=0), currency=currency,
             api_trade_available_flag=True, buy_available_flag=True)
        for currency, nominal in [('rub', 'usd'), ('cny', 'cny')]])
    report = {'candidates': []}
    with patch.object(probe, 'trading_facts', return_value={
            'status': 'SECURITY_TRADING_STATUS_NORMAL_TRADING',
            'api_trade_available_flag': True, 'bestprice_order_available_flag': False}):
        result = probe.resolve_bond(SimpleNamespace(instruments=service), 'MIXED', report)
    assert result.currency == 'cny'
    assert [item['currency'] for item in report['candidates']] == ['cny']
