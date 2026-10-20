"""Run the new live harness offline against strict SDK services and native cash facts."""
from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal as D
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec, patch
from test.test_fx_runtime import money, quotation

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest import services

from autorepeater.runner import Runner
from scripts.sandbox_lifecycle import SandboxLifecycle
from scripts.sandbox_support import OrderJournal, SandboxSession
from scripts.sandbox_support import write_failure_dump
from scripts.sandbox_lifecycle import SandboxFailure
from e2e import test_sandbox_multicurrency as scenarios


class OfflineBroker:
    """Finite disposable accounts; effect methods are reached only through SDK autospec."""

    def __init__(self):
        self.client = SimpleNamespace(**{name: create_autospec(
            inspect.unwrap(service), instance=True, spec_set=True) for name, service in (
                ('instruments', services.InstrumentsService), ('orders', services.OrdersService),
                ('operations', services.OperationsService),
                ('market_data', services.MarketDataService),
                ('sandbox', services.SandboxService), ('users', services.UsersService))})
        self.accounts = {}
        self.orders = {}
        self.metadata = {
            'R': sdk.Instrument(uid='R', ticker='SBER', name='Sber', instrument_type='share',
                                class_code='T', currency='rub', lot=10,
                                api_trade_available_flag=True),
            'C': sdk.Instrument(uid='C', ticker='RU000A105104', name='Rusal',
                                instrument_type='bond',
                                class_code='T', currency='cny', lot=1,
                                api_trade_available_flag=True),
            'F': sdk.Instrument(uid='F', ticker='CNYRUB_TOM', name='CNY',
                                instrument_type='currency',
                                class_code='T', currency='rub', lot=1000,
                                api_trade_available_flag=True)}
        self._wire_reads()
        self.client.sandbox.open_sandbox_account.side_effect = self.open_account
        self.client.sandbox.sandbox_pay_in.side_effect = self.pay_in
        self.client.orders.post_order.side_effect = self.submit
        self.client.orders.get_order_state.side_effect = lambda **kw: self.orders[kw['order_id']]

    def _wire_reads(self):
        client = self.client
        client.instruments.find_instrument.side_effect = lambda **kw: sdk.FindInstrumentResponse(
            instruments=[sdk.InstrumentShort(uid=item.uid, ticker=item.ticker,
                                             name=item.name, instrument_type=item.instrument_type,
                                             class_code=item.class_code)
                         for item in self.metadata.values() if item.ticker == kw['query']])
        client.instruments.get_instrument_by.side_effect = lambda **kw: sdk.InstrumentResponse(
            instrument=self.metadata[kw['id']])
        client.instruments.shares.return_value = sdk.SharesResponse(instruments=[sdk.Share(
            uid='R', ticker='SBER', name='Sber', class_code='T', currency='rub', lot=10,
            api_trade_available_flag=True)])
        client.instruments.bond_by.return_value = sdk.BondResponse(instrument=sdk.Bond(
            uid='C', isin='RU000A105104', currency='cny', nominal=money('cny', 1000),
            aci_value=money('cny', 7)))
        client.instruments.currencies.return_value = sdk.CurrenciesResponse(instruments=[
            sdk.Currency(uid='F', iso_currency_name='CNY', currency='rub', lot=1000,
                         nominal=money('cny', 1), api_trade_available_flag=True,
                         buy_available_flag=True, sell_available_flag=True)])
        client.instruments.currency_by.side_effect = lambda **kw: sdk.CurrencyResponse(
            instrument=sdk.Currency(uid=kw['id'], iso_currency_name=kw['id'][5:].upper()))
        client.market_data.get_order_book.side_effect = self.book
        client.market_data.get_trading_status.side_effect = (
            lambda **kw: sdk.GetTradingStatusResponse(
                instrument_uid=kw['instrument_id'], api_trade_available_flag=True,
                bestprice_order_available_flag=True))
        client.market_data.get_last_prices.side_effect = lambda **kw: sdk.GetLastPricesResponse(
            last_prices=[sdk.LastPrice(instrument_uid=uid, price=quotation(
                300 if uid == 'R' else 99), time=datetime.now(timezone.utc))
                         for uid in kw['instrument_id']])
        client.operations.get_portfolio.side_effect = self.portfolio
        client.operations.get_positions.side_effect = self.positions
        client.orders.get_max_lots.side_effect = self.caps
        client.orders.get_orders.return_value = sdk.GetOrdersResponse(orders=[])
        client.operations.get_operations.return_value = sdk.OperationsResponse(operations=[])
        client.users.get_accounts.side_effect = lambda: sdk.GetAccountsResponse(accounts=[
            sdk.Account(id=key, name=value['name']) for key, value in self.accounts.items()])

    def open_account(self, *, name):
        """Keep IDs and exact run ownership round-trippable through the real lifecycle."""
        uid = str(len(self.accounts) + 1)
        self.accounts[uid] = {'name': name, 'cash': {'rub': D(0), 'cny': D(0)}, 'held': {}}
        return sdk.OpenSandboxAccountResponse(account_id=uid)

    def pay_in(self, *, account_id, amount):
        """Only this explicit fixture effect can add money from outside the account."""
        self.accounts[account_id]['cash'][amount.currency] += D(amount.units) + D(amount.nano)/10**9
        return sdk.SandboxPayInResponse(balance=amount)

    @staticmethod
    def book(*, instrument_id, depth):
        """Different percent, RUB per share and RUB per CNY dimensions."""
        assert depth == 1
        price = {'R': 300, 'C': 99, 'F': 12}[instrument_id]
        return sdk.GetOrderBookResponse(
            instrument_uid=instrument_id,
            orderbook_ts=datetime.now(timezone.utc),
            bids=[sdk.Order(price=quotation(price), quantity=1000000)],
            asks=[sdk.Order(price=quotation(price), quantity=1000000)])

    def portfolio(self, *, account_id):
        """Intentionally report a different broker CNY RUB mark; runtime must use books."""
        account = self.accounts[account_id]
        positions = [sdk.PortfolioPosition(
            instrument_uid='cash-' + code,
            instrument_type='currency', quantity=quotation(amount),
            current_price=money('rub', 1 if code == 'rub' else 10),
            blocked=False, blocked_lots=quotation(0)) for code, amount in account['cash'].items()
                     if amount]
        for uid, count in account['held'].items():
            if count:
                positions.append(sdk.PortfolioPosition(
                    instrument_uid=uid,
                    instrument_type=self.metadata[uid].instrument_type, quantity=quotation(count),
                    current_price=money('rub', 300 if uid == 'R' else 11000),
                    blocked=False, blocked_lots=quotation(0)))
        return sdk.PortfolioResponse(positions=positions)

    def positions(self, *, account_id):
        """GetPositions has physical pieces and native money, never FX securities."""
        account = self.accounts[account_id]
        return sdk.PositionsResponse(
            money=[money(code, amount) for code, amount in account['cash'].items()],
            blocked=[], securities=[
                sdk.PositionsSecurities(instrument_uid=uid, balance=int(count), blocked=0,
                                        exchange_blocked=False)
                for uid, count in account['held'].items() if count], futures=[], options=[],
            limits_loading_in_progress=False)

    def caps(self, *, request):
        """Strict native own limits; zero foreign cash produces zero initial bond cap."""
        account = self.accounts[request.account_id]
        item = self.metadata[request.instrument_id]
        cost = {'R': D(300), 'C': D(997), 'F': D(12)}[item.uid] * item.lot
        return sdk.GetMaxLotsResponse(currency=item.currency, buy_limits=sdk.BuyLimitsView(
            buy_money_amount=quotation(account['cash'][item.currency]),
            buy_max_lots=int(account['cash'][item.currency] / cost)),
            sell_limits=sdk.SellLimitsView(sell_max_lots=(
                int(account['cash']['cny'] / 1000) if item.uid == 'F'
                else int(account['held'].get(item.uid, D(0)) / item.lot))))

    def submit(self, **kwargs):
        """One checked BESTPRICE effect, separate known sandbox fee and single bond ACI."""
        assert 'price' not in kwargs
        assert kwargs['order_type'] == sdk.OrderType.ORDER_TYPE_BESTPRICE
        account = self.accounts[kwargs['account_id']]
        item, lots = self.metadata[kwargs['instrument_id']], kwargs['quantity']
        side = 1 if kwargs['direction'] == sdk.OrderDirection.ORDER_DIRECTION_BUY else -1
        total = D(lots * item.lot) * {'R': D(300), 'C': D(997), 'F': D(12)}[item.uid]
        fee = total * D('.001')
        account['cash'][item.currency] -= side * total + fee
        if item.uid == 'F':
            account['cash']['cny'] += side * D(lots * item.lot)
        else:
            account['held'][item.uid] = account['held'].get(item.uid, D(0)) + side * lots * item.lot
        assert all(amount >= 0 for amount in account['cash'].values())
        broker_id = 'broker' + str(len(self.orders))
        fields = {'order_id': broker_id, 'instrument_uid': item.uid,
                  'direction': kwargs['direction'], 'lots_requested': lots, 'lots_executed': lots,
                  'execution_report_status': (
                      sdk.OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL),
                  'total_order_amount': money(item.currency, total),
                  'executed_commission': money(item.currency, fee)}
        self.orders[broker_id] = sdk.OrderState(**fields)
        return sdk.PostOrderResponse(**fields)


@pytest.fixture(name='offline_live')
def fixture_offline_live(monkeypatch):
    """Only client creation is substituted; real Runner, planner, executor and harness run."""
    broker = OfflineBroker()
    lifecycle = SandboxLifecycle(broker.client.sandbox, broker.client.users, 'local', 'a' * 32)
    session = SandboxSession(broker.client, lifecycle)
    journal = OrderJournal()
    logger = logging.getLogger('tinkoffBot')
    logger.addHandler(journal)
    # Synthetic value only; the actual environment/token files are never inspected.
    monkeypatch.setenv('SANDBOX_TOKEN', 'offline-synthetic')
    try:
        with patch.object(Runner, '_create_client', autospec=True, spec_set=True) as connect:
            connect.return_value.__enter__.return_value = broker.client
            yield (session, journal), broker
    finally:
        logger.removeHandler(journal)


@pytest.mark.parametrize('algorithm,source', [
    ('INDEX', 'CROSS'), ('COMPOSITE', 'MIX'), ('ACCOUNT', 'MIXED')])
def test_new_mixed_scenarios_offline(offline_live, tmp_path, monkeypatch, algorithm, source):
    """Run the very same acceptance scenario, not a rewritten weaker offline contract."""
    live, broker = offline_live
    scenarios.test_rub_cny_normal_application(live, tmp_path, monkeypatch, algorithm, source)
    broker.client.orders.get_order_price.assert_not_called()
    broker.client.operations.get_operations.assert_not_called()


@pytest.mark.parametrize('scenario', [
    scenarios.test_shared_cny_owners_and_unallocated_cash,
    scenarios.test_reverse_exchange_only_for_rub_purchase,
    scenarios.test_isolated_sale_does_not_fund_sibling,
])
def test_new_composite_reverse_scenarios_offline(offline_live, tmp_path, monkeypatch, scenario):
    """Ownership and reverse FX have the same financial assertions offline and live."""
    live, _ = offline_live
    scenario(live, tmp_path, monkeypatch)


@pytest.mark.parametrize('kind', ['sufficient', 'partial'])
def test_new_existing_currency_scenarios_offline(offline_live, tmp_path, monkeypatch, kind):
    """Existing currency is not reset or replenished to make the assertion pass."""
    live, _ = offline_live
    scenarios.test_existing_cny_before_lot_rounding(live, tmp_path, monkeypatch, kind)


def _wire_commission_probe(broker, model):
    """Raw 7dp fees with 6dp history/positions distinguish numeric balance models."""
    from decimal import ROUND_HALF_UP  # pylint: disable=import-outside-toplevel
    history = {key: [] for key in broker.accounts}
    exact_balances = {}
    original = broker.client.orders.post_order.side_effect

    def submit(**kwargs):
        account, lots = kwargs['account_id'], kwargs['quantity']
        before = broker.accounts[account]['cash']['cny']
        response = original(**kwargs)
        if kwargs['instrument_id'] == 'F':
            pieces = lots * broker.metadata['F'].lot
            for kind, payment, quantity in (
                    (sdk.OperationType.OPERATION_TYPE_BUY, -12 * pieces, pieces),
                    (sdk.OperationType.OPERATION_TYPE_BROKER_FEE, D('-.012') * pieces, 0)):
                history.setdefault(account, []).append(sdk.Operation(
                    id=response.order_id + '-' + kind.name, instrument_uid='F',
                    operation_type=kind, state=sdk.OperationState.OPERATION_STATE_EXECUTED,
                    quantity=quantity, payment=money('rub', payment)))
            return response
        fee = D('.123456' if model == 'equal-fees' else '.1234565') * lots
        history_fee = fee.quantize(D('.000001'), rounding=ROUND_HALF_UP)
        total = D(response.total_order_amount.units) + D(response.total_order_amount.nano)/10**9
        exact_balances[account] = exact_balances.get(account, before) - total - (
            history_fee if model == 'cash-rounded' else fee)
        broker.accounts[account]['cash']['cny'] = exact_balances[account].quantize(
            D('.000001'), rounding=ROUND_HALF_UP)
        response = replace(response, executed_commission=money('cny', fee))
        broker.orders[response.order_id] = replace(
            broker.orders[response.order_id], executed_commission=response.executed_commission,
            stages=[sdk.OrderStage(price=money('cny', 997),
                                   quantity=lots * broker.metadata['C'].lot,
                                   trade_id=response.order_id + '-fill')])
        for kind, payment, quantity in (
                (sdk.OperationType.OPERATION_TYPE_BUY, -total, lots * broker.metadata['C'].lot),
                (sdk.OperationType.OPERATION_TYPE_BROKER_FEE, -history_fee, 0)):
            history.setdefault(account, []).append(sdk.Operation(
                id=response.order_id + '-' + kind.name, instrument_uid='C',
                operation_type=kind, state=sdk.OperationState.OPERATION_STATE_EXECUTED,
                quantity=quantity, quantity_rest=0, date=datetime.now(timezone.utc),
                payment=money('cny', payment), price=money('cny', 997), trades=[]))
        return response

    broker.client.orders.post_order.side_effect = submit
    broker.client.operations.get_operations.side_effect = lambda **kw: sdk.OperationsResponse(
        operations=[] if model == 'no-history' else history.get(kw['account_id'], []))
    broker.client.sandbox.close_sandbox_account.side_effect = (
        lambda **kw: broker.accounts.pop(kw['account_id']))


@pytest.mark.parametrize('model,verdict', [
    ('response-rounded', 'inconclusive'),
    ('cash-rounded', 'inconclusive'),
    ('equal-fees', 'inconclusive'), ('no-history', 'inconclusive'),
])
def test_controlled_cny_probe_offline(offline_live, monkeypatch, model, verdict):
    """Run the additional live case with exact account, order, cash and cleanup effects."""
    live, broker = offline_live
    _wire_commission_probe(broker, model)
    elapsed = [0]
    monkeypatch.setattr(live[0], 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(live[0], 'sleep', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    scenarios.test_cny_commission_one_order_vs_split(live)
    summary = [item for item in live[0].records if item['event'] == 'cny_commission_probe']
    assert len(summary) == 1 and summary[0]['comparison']['verdict'] == verdict
    if model != 'no-history':
        assert any(item['model'] == (
            'per-operation-fee' if model == 'cash-rounded' else 'display-final-balance')
            for item in summary[0]['comparison']['consistent_models'])
    else:
        assert not summary[0]['comparison']['consistent_models']
    steps = [item for item in live[0].records if item['event'] == 'cny_probe_step']
    assert [item['lots'] for item in steps] == [2, 1, 1]
    orders = broker.client.orders.post_order.call_args_list
    assert [(call.kwargs['instrument_id'], call.kwargs['quantity'])
            for call in orders] == [('F', 4), ('F', 4), ('C', 2), ('C', 1), ('C', 1)]
    assert orders[0].kwargs['account_id'] == orders[2].kwargs['account_id']
    assert orders[1].kwargs['account_id'] == orders[3].kwargs['account_id']
    assert orders[3].kwargs['account_id'] == orders[4].kwargs['account_id']
    payins = broker.client.sandbox.sandbox_pay_in.call_args_list
    assert len(payins) == 2
    assert [(call.kwargs['amount'].currency, call.kwargs['amount'].units,
             call.kwargs['amount'].nano) for call in payins] == [('rub', 1000000, 0)] * 2
    assert not live[0].lifecycle.created and not broker.accounts
    assert broker.client.sandbox.close_sandbox_account.call_count == 2
    broker.client.orders.get_order_price.assert_not_called()
    assert all(item['before']['money']['cny'] > item['after']['money']['cny'] >= 0
               for item in steps)
    assert all(item['own_money'] >= item['principal'] + item['reported_fee'] for item in steps)
    assert len({entry['order_id'] for entry in steps}) == 3
    for event in live[0].records:
        if event['event'] == 'currency_order_money':
            for field in ('total_order_amount', 'executed_commission'):
                raw = getattr(broker.orders[event['order_id']], field)
                assert event[field]['units'] == {'type': 'int', 'value': str(raw.units)}
                assert event[field]['nano'] == {'type': 'int', 'value': str(raw.nano)}
    if model != 'no-history':
        histories = [item for item in live[0].records if item['event'] == 'cny_probe_history']
        assert len(histories) == 3 and all(item['complete'] for item in histories)
        assert all(sum(op['type'] == 'OPERATION_TYPE_BROKER_FEE'
                       for op in item['operations']) == 1 for item in histories)


@pytest.mark.parametrize('bond_lot,fx_lot,expected_lots', [(1, 1000, 4), (3, 100, 120)])
def test_probe_fx_preparation_settles_before_equal_baselines(
        offline_live, monkeypatch, bond_lot, fx_lot, expected_lots):
    """Different FX prices and a late RUB debit cannot leak into bond measurements."""
    # pylint: disable=too-many-locals
    live, broker = offline_live
    session = live[0]
    broker.metadata['C'] = replace(broker.metadata['C'], lot=bond_lot)
    broker.metadata['F'] = replace(broker.metadata['F'], lot=fx_lot)
    currencies = broker.client.instruments.currencies.return_value
    currencies.instruments[0] = replace(currencies.instruments[0], lot=fx_lot)
    _wire_commission_probe(broker, 'cash-rounded')
    submit = broker.client.orders.post_order.side_effect
    pending, trace = {}, []
    elapsed = [0]

    def delayed_submit(**kwargs):
        account = kwargs['account_id']
        trace.append(('post', kwargs['instrument_id'], account, kwargs['quantity']))
        if kwargs['instrument_id'] == 'C':
            assert not pending
            baselines = [item for item in session.records if item['event'] == 'cny_probe_baseline']
            assert len(baselines) == 2
        before_rub = broker.accounts[account]['cash']['rub']
        response = submit(**kwargs)
        if kwargs['instrument_id'] == 'F':
            extra = D(account)  # Different actual RUB principals at each account's FX fill.
            response = replace(response, total_order_amount=money(
                'rub', D(response.total_order_amount.units) + extra))
            broker.orders[response.order_id] = replace(
                broker.orders[response.order_id], total_order_amount=response.total_order_amount)
            pending[account] = broker.accounts[account]['cash']['rub'] - extra
            broker.accounts[account]['cash']['rub'] = before_rub
        return response

    def settle(delay):
        trace.append(('settle', tuple(pending)))
        elapsed[0] += delay
        for account, rub in pending.items():
            broker.accounts[account]['cash']['rub'] = rub
        pending.clear()

    broker.client.orders.post_order.side_effect = delayed_submit
    monkeypatch.setattr(session, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(session, 'sleep', settle)
    scenarios.test_cny_commission_one_order_vs_split(live)
    orders = broker.client.orders.post_order.call_args_list
    assert [(call.kwargs['instrument_id'], call.kwargs['quantity']) for call in orders] == [
        ('F', expected_lots), ('F', expected_lots), ('C', 2), ('C', 1), ('C', 1)]
    assert all(call.kwargs['direction'] == sdk.OrderDirection.ORDER_DIRECTION_BUY
               and call.kwargs['order_type'] == sdk.OrderType.ORDER_TYPE_BESTPRICE
               and set(call.kwargs) == {
                   'account_id', 'instrument_id', 'quantity', 'direction', 'order_type', 'order_id'}
               for call in orders)
    assert trace[:4] == [('post', 'F', '1', expected_lots), ('settle', ('1',)),
                         ('post', 'F', '2', expected_lots), ('settle', ('2',))]
    fx_states = [call.kwargs for call in broker.client.orders.get_order_state.call_args_list
                 if broker.orders[call.kwargs['order_id']].instrument_uid == 'F']
    assert fx_states == [
        {'account_id': '1', 'order_id': 'broker0'},
        {'account_id': '1', 'order_id': 'broker0'},
        {'account_id': '2', 'order_id': 'broker1'},
        {'account_id': '2', 'order_id': 'broker1'}]
    baselines = [item['state'] for item in session.records if item['event'] == 'cny_probe_baseline']
    assert [item['money']['cny'] for item in baselines] == [D(expected_lots * fx_lot)] * 2
    assert baselines[0]['money']['rub'] != baselines[1]['money']['rub']
    steps = [item for item in session.records if item['event'] == 'cny_probe_step']
    for item in steps:
        baseline = baselines[int(item['account_id']) - 1]
        assert (item['before']['money']['rub'] == item['after']['money']['rub']
                == baseline['money']['rub'])
        assert item['history_principal'] == D(997 * item['lots'] * bond_lot)
        assert item['history_fee'] == (D('.246913') if item['lots'] == 2 else D('.123457'))
    assert not broker.accounts


@pytest.mark.parametrize('bond_fee_currency,bond_fee_uid', [
    ('cny', 'C'), ('rub', 'C'), ('rub', '')])
def test_late_fx_history_is_excluded_by_identity_without_hiding_bond_fee(
        offline_live, bond_fee_currency, bond_fee_uid):
    """Late FX BUY and linked UID-less RUB fee are setup; a bond fee keeps its guards."""
    # pylint: disable=protected-access
    live, broker = offline_live
    buy, fee = sdk.OperationType.OPERATION_TYPE_BUY, sdk.OperationType.OPERATION_TYPE_BROKER_FEE
    executed, pending = (sdk.OperationState.OPERATION_STATE_EXECUTED,
                         sdk.OperationState.OPERATION_STATE_PROGRESS)
    operations = [sdk.Operation(
        id=identity, instrument_uid=uid, parent_operation_id=parent,
        operation_type=kind, state=state, quantity=quantity, quantity_rest=0,
        payment=money(code, payment))
        for identity, uid, parent, kind, state, quantity, code, payment in (
            ('fx-buy', 'F', '', buy, executed, 4000, 'rub', -48000),
            ('fx-fee', '', 'fx-buy', fee, pending, 0, 'rub', -48),
            ('bond-buy', 'C', '', buy, executed, 1, 'cny', -997),
            ('bond-fee', bond_fee_uid, 'bond-buy', fee, pending, 0,
             bond_fee_currency, D('-.123457')))]
    broker.client.operations.get_operations.return_value = sdk.OperationsResponse(
        operations=operations)
    args = (live[0], 'dst', set(), broker.metadata['C'], D(1))
    seen, principal, commission = scenarios._probe_history(*args, setup_uid='F')
    assert seen == {'bond-buy'} and principal == 997 and commission is None
    assert live[0].records[-1]['pending_ids'] == ['bond-fee']
    for index in (1, 3):
        operations[index] = replace(operations[index], state=executed)
    if bond_fee_currency == 'rub':
        with pytest.raises(SandboxFailure, match='invalid commission probe operation payment'):
            scenarios._probe_history(*args, setup_uid='F')
    else:
        seen, principal, commission = scenarios._probe_history(*args, setup_uid='F')
        assert (seen == {'bond-buy', 'bond-fee'} and principal == 997
                and commission == D('.123457'))
        assert {item['id'] for item in live[0].records[-1]['operations']} == {
            'bond-buy', 'bond-fee'}


@pytest.mark.parametrize('fault', [
    'pay-in', 'fx-rejected', 'fx-partial', 'own-money', 'own-lots', 'fx-budget', 'settlement'])
def test_probe_partial_setup_failure_closes_both_owned_accounts(offline_live, monkeypatch, fault):
    """An uncertain second preparation never starts bond orders or leaves an owned account."""
    live, broker = offline_live
    _wire_commission_probe(broker, 'cash-rounded')
    elapsed = [0]
    monkeypatch.setattr(live[0], 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(live[0], 'sleep', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    pay_in = broker.client.sandbox.sandbox_pay_in.side_effect
    submit = broker.client.orders.post_order.side_effect
    caps = broker.client.orders.get_max_lots.side_effect

    def failing_pay_in(**kwargs):
        assert kwargs['amount'].currency == 'rub'
        if kwargs['account_id'] == '2' and fault == 'pay-in':
            raise SandboxFailure('test second pay-in failure')
        return pay_in(**kwargs)

    def limits(**kwargs):
        result = caps(**kwargs)
        if kwargs['request'].account_id == '2':
            result = replace(result, buy_limits=replace(
                result.buy_limits, buy_money_amount=(quotation(48000) if fault == 'own-money'
                                                     else result.buy_limits.buy_money_amount),
                buy_max_lots=(0 if fault == 'own-lots' else result.buy_limits.buy_max_lots)))
        return result

    broker.client.sandbox.sandbox_pay_in.side_effect = failing_pay_in
    broker.client.orders.post_order.side_effect = lambda **kw: _probe_setup_submit_failure(
        broker, submit, fault, kw)
    broker.client.orders.get_max_lots.side_effect = limits
    with pytest.raises(SandboxFailure):
        scenarios.test_cny_commission_one_order_vs_split(live)
    assert all(call.kwargs['instrument_id'] == 'F'
               for call in broker.client.orders.post_order.call_args_list)
    if fault in ('own-money', 'own-lots', 'pay-in'):
        assert broker.client.orders.post_order.call_count == 1
    assert broker.client.sandbox.close_sandbox_account.call_count == 2
    assert not broker.accounts and not live[0].lifecycle.created
    assert live[0].records[-1]['event'] == 'cny_probe_cleanup'


def _probe_setup_submit_failure(broker, submit, fault, kwargs):
    """Inject only the second account's FX failure, retaining the first settled setup."""
    if kwargs['account_id'] != '2':
        return submit(**kwargs)
    if fault == 'fx-rejected':
        raise SandboxFailure('test second FX failure')
    response = submit(**kwargs)
    if fault == 'fx-partial':
        return replace(response, lots_executed=1)
    if fault == 'fx-budget':
        broker.orders[response.order_id] = replace(
            broker.orders[response.order_id], executed_commission=money('rub', 1000001))
    if fault == 'settlement':
        broker.accounts['2']['cash']['rub'] += D('.000000002')
    return response


@pytest.mark.parametrize('fault', ['unequal-cny', 'bond-rub-debit'])
def test_probe_rejects_changed_control_balances(offline_live, fault):
    """A mismatched initial CNY amount or even a nano RUB debit invalidates the probe."""
    live, broker = offline_live
    _wire_commission_probe(broker, 'cash-rounded')
    original_state = scenarios.native_state
    original_submit = broker.client.orders.post_order.side_effect

    def state(session, account):
        result = original_state(session, account)
        if (fault == 'unequal-cny' and account == '1' and len(broker.orders) == 2):
            return replace(result, money={**result.money, 'cny': result.money['cny'] + 1})
        return result

    def submit(**kwargs):
        result = original_submit(**kwargs)
        if fault == 'bond-rub-debit' and kwargs['instrument_id'] == 'C':
            broker.accounts[kwargs['account_id']]['cash']['rub'] -= D('.000000001')
        return result

    broker.client.orders.post_order.side_effect = submit
    with patch.object(scenarios, 'native_state', autospec=True, spec_set=True, side_effect=state):
        with pytest.raises(SandboxFailure, match=(
                'unequal initial CNY' if fault == 'unequal-cny' else 'cash changed outside')):
            scenarios.test_cny_commission_one_order_vs_split(live)
    if fault == 'unequal-cny':
        assert all(call.kwargs['instrument_id'] == 'F'
                   for call in broker.client.orders.post_order.call_args_list)
    assert not broker.accounts and not live[0].lifecycle.created


def test_pending_probe_fee_is_not_consumed_or_complete(offline_live):
    """An executed fee cannot make a second pending fee disappear on the next poll."""
    # pylint: disable=protected-access
    live, broker = offline_live
    operations = [sdk.Operation(
        id=identity, instrument_uid='C', operation_type=kind, state=state,
        quantity=quantity, quantity_rest=0, payment=money('cny', payment))
        for identity, kind, state, quantity, payment in (
            ('buy', sdk.OperationType.OPERATION_TYPE_BUY,
             sdk.OperationState.OPERATION_STATE_EXECUTED, 1, -997),
            ('fee1', sdk.OperationType.OPERATION_TYPE_BROKER_FEE,
             sdk.OperationState.OPERATION_STATE_EXECUTED, 0, D('-.1')),
            ('fee2', sdk.OperationType.OPERATION_TYPE_BROKER_FEE,
             sdk.OperationState.OPERATION_STATE_PROGRESS, 0, D('-.2')))]
    broker.client.operations.get_operations.return_value = sdk.OperationsResponse(
        operations=operations)
    broker.client.operations.get_operations.side_effect = None
    bond = broker.metadata['C']
    seen, principal, fee = scenarios._probe_history(live[0], 'dst', set(), bond, D(1))
    assert seen == {'buy', 'fee1'}
    assert principal == 997 and fee is None
    fact = live[0].records[-1]
    assert fact['complete'] is False
    assert fact['observed_fee'] == D('.1')
    assert fact['pending_ids'] == ['fee2']
    operations[2] = replace(operations[2], state=sdk.OperationState.OPERATION_STATE_EXECUTED)
    next_seen, _, _ = scenarios._probe_history(live[0], 'dst', seen, bond, D(1))
    assert next_seen == {'fee2'}
    assert live[0].records[-1]['observed_fee'] == D('.2')
    assert live[0].records[-1]['complete'] is False


@pytest.mark.parametrize('state', [
    sdk.OperationState.OPERATION_STATE_PROGRESS, sdk.OperationState.OPERATION_STATE_CANCELED])
def test_incomplete_history_preserves_facts_until_fee_executes(offline_live, state):
    """Polling the same BUY must retain it, not silently complete with a pending fee."""
    # pylint: disable=protected-access
    live, broker = offline_live
    operations = [sdk.Operation(
        id='buy', instrument_uid='C', operation_type=sdk.OperationType.OPERATION_TYPE_BUY,
        state=sdk.OperationState.OPERATION_STATE_EXECUTED, quantity=1, quantity_rest=0,
        payment=money('cny', -997)), sdk.Operation(
            id='fee', instrument_uid='C',
            operation_type=sdk.OperationType.OPERATION_TYPE_BROKER_FEE,
            state=state, quantity=0, quantity_rest=0, payment=money('cny', D('-.123457')))]
    broker.client.operations.get_operations.return_value = sdk.OperationsResponse(
        operations=operations)
    for _ in range(2):
        consumed, principal, fee = scenarios._probe_history(
            live[0], 'dst', set(), broker.metadata['C'], D(1))
        assert consumed == {'buy'} and principal == 997 and fee is None
        assert live[0].records[-1]['complete'] is False
        assert len(live[0].records[-1]['operations']) == 2
    operations[1] = replace(operations[1], state=sdk.OperationState.OPERATION_STATE_EXECUTED)
    consumed, principal, fee = scenarios._probe_history(
        live[0], 'dst', set(), broker.metadata['C'], D(1))
    assert consumed == {'buy', 'fee'} and principal == 997 and fee == D('.123457')
    assert live[0].records[-1]['complete'] is True


@pytest.mark.parametrize('fault', [
    'missing', 'quantity', 'different-prices', 'invalid-quantity', 'invalid-price'])
def test_probe_checks_native_fill_prices_without_inventing_them(offline_live, fault):
    """Native per-fill provenance is required for comparison, independently of totals."""
    live, broker = offline_live
    _wire_commission_probe(broker, 'cash-rounded')
    original = broker.client.orders.get_order_state.side_effect

    def state(**kwargs):
        response = original(**kwargs)
        if response.instrument_uid != 'C':
            return response
        stages = response.stages
        if fault == 'missing':
            stages = []
        elif fault == 'quantity':
            stages = [replace(stages[0], quantity=50)]
        elif fault == 'different-prices':
            stages = [replace(stages[0], price=money('cny', 900 + response.lots_requested))]
        elif fault == 'invalid-quantity':
            stages = [replace(stages[0], quantity=True)]
        else:
            stages = [replace(stages[0], price=money('rub', 997))]
        return replace(response, stages=stages)

    broker.client.orders.get_order_state.side_effect = state
    if fault.startswith('invalid'):
        with pytest.raises(SandboxFailure, match='probe.*(fill quantity|fill price)'):
            scenarios.test_cny_commission_one_order_vs_split(live)
    else:
        scenarios.test_cny_commission_one_order_vs_split(live)
        comparison = next(item['comparison'] for item in live[0].records
                          if item['event'] == 'cny_commission_probe')
        assert comparison['controlled_inputs'] is False
        assert not comparison['consistent_models']
        assert comparison['verdict'] == 'inconclusive'
    assert not broker.accounts


@pytest.mark.parametrize('fault', [
    'identity', 'over-budget', 'duplicate-history', 'history-currency', 'no-own-lots', 'cleanup',
])
def test_controlled_probe_rejects_bad_facts_and_always_closes_owned_accounts(offline_live, fault):
    """Diagnostic success never excuses an invalid receipt, budget or cleanup."""
    live, broker = offline_live
    _wire_commission_probe(broker, 'response-rounded')
    state = broker.client.orders.get_order_state.side_effect
    history = broker.client.operations.get_operations.side_effect
    caps = broker.client.orders.get_max_lots.side_effect
    if fault == 'identity':
        broker.client.orders.get_order_state.side_effect = lambda **kw: replace(
            state(**kw), instrument_uid=(
                'wrong' if state(**kw).instrument_uid == 'C' else 'F'))
    if fault == 'over-budget':
        broker.client.orders.get_order_state.side_effect = lambda **kw: replace(
            state(**kw), executed_commission=(money('cny', 10000)
                                              if state(**kw).instrument_uid == 'C'
                                              else state(**kw).executed_commission))
    if fault == 'duplicate-history':
        broker.client.operations.get_operations.side_effect = lambda **kw: replace(
            history(**kw), operations=history(**kw).operations + history(**kw).operations[:1])
    if fault == 'history-currency':
        broker.client.operations.get_operations.side_effect = lambda **kw: replace(
            history(**kw), operations=[
                replace(item, payment=money('rub', -997)) for item in history(**kw).operations])
    if fault == 'no-own-lots':
        broker.client.orders.get_max_lots.side_effect = lambda **kw: replace(
            caps(**kw), buy_limits=replace(caps(**kw).buy_limits, buy_max_lots=(
                0 if kw['request'].instrument_id == 'C' else caps(**kw).buy_limits.buy_max_lots)))
    if fault == 'cleanup':
        broker.client.sandbox.close_sandbox_account.side_effect = None
    with pytest.raises(SandboxFailure):
        scenarios.test_cny_commission_one_order_vs_split(live)
    assert broker.client.sandbox.close_sandbox_account.call_count == 2
    assert not live[0].lifecycle.created
    if fault == 'cleanup':
        assert broker.accounts
        assert not any(item['event'] == 'cny_probe_cleanup' for item in live[0].records)
    else:
        assert not broker.accounts
        assert live[0].records[-1]['event'] == 'cny_probe_cleanup'
        assert live[0].records[-1]['confirmed_absent'] is True


@pytest.mark.parametrize('fault', ['closed', 'stale', 'wrong-isin', 'wrong-currency', 'transport'])
def test_preflight_failure_never_creates_accounts_or_orders(offline_live, fault):
    """Market closures, malformed responses and transport faults are offline-only failures."""
    live, broker = offline_live
    if fault == 'closed':
        broker.client.market_data.get_trading_status.return_value = sdk.GetTradingStatusResponse(
            instrument_uid='R', api_trade_available_flag=False,
            bestprice_order_available_flag=False)
        broker.client.market_data.get_trading_status.side_effect = None
    if fault == 'wrong-isin':
        broker.client.instruments.bond_by.return_value.instrument.isin = 'wrong'
    if fault == 'wrong-currency':
        broker.metadata['C'].currency = 'rub'
    if fault == 'stale':
        book = broker.client.market_data.get_order_book.side_effect

        def stale(**kwargs):
            result = book(**kwargs)
            result.orderbook_ts = datetime(2000, 1, 1, tzinfo=timezone.utc)
            return result
        broker.client.market_data.get_order_book.side_effect = stale
    if fault == 'transport':
        broker.client.instruments.find_instrument.side_effect = RuntimeError('offline transport')
    with pytest.raises((SandboxFailure, ValueError, RuntimeError)):
        scenarios.catalog(live[0])
    broker.client.sandbox.open_sandbox_account.assert_not_called()
    broker.client.orders.post_order.assert_not_called()


def test_setup_uses_index_preference_for_all_security_queries(offline_live):
    """A newly available CNY SBER must fail the RUB guard, not mismatch runtime selection."""
    live, broker = offline_live
    cny_share = replace(broker.metadata['R'], uid='SBER-CNY', currency='cny', class_code='CNY')
    broker.metadata = {'SBER-CNY': cny_share, **broker.metadata}
    with pytest.raises(SandboxFailure, match='invalid settlement currency'):
        scenarios.catalog(live[0])
    broker.client.sandbox.open_sandbox_account.assert_not_called()
    broker.client.orders.post_order.assert_not_called()


def test_currency_dump_keeps_native_money_and_commissions(offline_live, tmp_path, monkeypatch):
    """A successful monetary check must also survive the real finalizer's JSON boundary."""
    live, _ = offline_live
    scenarios.test_existing_cny_before_lot_rounding(live, tmp_path, monkeypatch, 'partial')
    path = tmp_path / 'native-evidence.json'
    write_failure_dump(live[0], path)
    result = json.loads(path.read_text())
    assert any(event['event'] == 'native_state' and 'cny' in event['money']
               for event in result['events'])
    assert any(event['event'] == 'currency_app_pass' and 'cny' in event['fees']
               for event in result['events'])
    assert all('error' not in account for account in result['accounts'].values())


@pytest.mark.parametrize('snapshot_fails', [False, True])
@pytest.mark.parametrize('fee_adjustment', [D('.000001'), D('.000000750')])
def test_failed_money_check_keeps_order_fields_and_poll_deltas(
        offline_live, tmp_path, monkeypatch, snapshot_fails, fee_adjustment):
    """Cash mismatch or unsupported precision retains all already-read evidence."""
    live, broker = offline_live
    elapsed = [0]

    def sleep(seconds):
        elapsed[0] += seconds

    def order_state(**kwargs):
        state = broker.orders[kwargs['order_id']]
        state = replace(state, executed_order_price=money(
            state.total_order_amount.currency, D('1005.699000001')))
        if state.instrument_uid == 'C':
            fee = state.executed_commission
            return replace(state, executed_commission=money(
                'cny', D(fee.units) + D(fee.nano) / 10**9 + fee_adjustment),
                executed_order_price=money('cny', D('1005.699000001')))
        return state

    monkeypatch.setattr(live[0], 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(live[0], 'sleep', sleep)
    broker.client.orders.get_order_state.side_effect = order_state
    with pytest.raises(SandboxFailure, match=(
            '^unsupported sandbox CNY bond precision$' if fee_adjustment == D('.000000750') else
            '^native money disagrees with order totals and single commissions$')):
        scenarios.test_existing_cny_before_lot_rounding(live, tmp_path, monkeypatch, 'partial')
    assert elapsed == [30]
    assert broker.client.orders.post_order.call_count == 3
    assert broker.client.orders.get_order_state.call_count == 3
    broker.client.orders.get_order_price.assert_not_called()
    broker.client.operations.get_operations.assert_not_called()
    if snapshot_fails:
        monkeypatch.setattr(live[0], 'diagnostic', create_autospec(
            live[0].diagnostic, spec_set=True, side_effect=RuntimeError('secret SDK payload')))
    path = tmp_path / 'failed-money.json'
    write_failure_dump(live[0], path)
    result = json.loads(path.read_text())
    assert set(result) == {'time', 'scenario', 'configs', 'quotes', 'events', 'accounts'}
    assert 'secret SDK payload' not in path.read_text()
    events = result['events']
    orders = [event for event in events if event['event'] == 'currency_order_money']
    assert len(orders) == 2
    for event in orders:
        _assert_order_money(event, broker, order_state(order_id=event['order_id']))
    assert {event['trade']['base_currency'] for event in orders} == {None, 'cny'}
    _assert_settlement_polls(events, orders, fee_adjustment, fee_adjustment == D('.000000750'))
    assert not any(event['event'] == 'currency_app_pass' for event in events)
    assert broker.client.orders.get_order_state.call_count == 3


def _assert_order_money(event, broker, state):
    """Whitelist fields survive JSON with raw units/nano and their exact normalized amount."""
    assert event['account_id'] in broker.accounts
    assert event['request_id']
    assert event['status'] == 'EXECUTION_REPORT_STATUS_FILL'
    assert event['requested'] == event['executed'] == state.lots_executed
    assert event['trade']['uid'] == state.instrument_uid
    assert event['trade']['lot_size'] == broker.metadata[state.instrument_uid].lot
    for field in ('total_order_amount', 'executed_commission', 'executed_order_price'):
        value = getattr(state, field)
        fact = event[field]
        assert fact['currency'] == {'type': 'str', 'value': value.currency}
        assert fact['units'] == {'type': 'int', 'value': str(value.units)}
        assert fact['nano'] == {'type': 'int', 'value': str(value.nano)}
    assert D(event['trade']['total']) == D(event['total_order_amount']['normalized']['value'])
    assert D(event['trade']['commission']) == D(event['executed_commission']['normalized']['value'])


def _assert_settlement_polls(events, orders, residual, unsupported):
    """The original bounded polling preserves every observation before the monetary failure."""
    polls = [event for event in events if event['event'] == 'currency_settlement_poll']
    assert [event['poll'] for event in polls] == list(range(1, 17))
    for event in polls:
        assert event['account_id'] == orders[0]['account_id']
        assert datetime.fromisoformat(event['observed_utc']).tzinfo is not None
        assert all(D(value) == 0 for value in event['blocked'].values())
        cny = {key: D(event['money']['cny'][key]) for key in (
            'before', 'expected', 'raw_expected', 'actual', 'expected_delta', 'actual_delta',
            'residual', 'raw_residual')}
        assert cny['residual'] == cny['raw_residual'] == residual
        assert cny['expected'] == cny['raw_expected']
        assert cny['actual'] - cny['expected'] == cny['residual']
        assert cny['expected_delta'] == cny['expected'] - cny['before']
        assert cny['actual_delta'] == cny['actual'] - cny['before']
        facts = event['money']['cny']
        terms = event['components']['cny']
        if unsupported:
            assert facts['settlement_status'] == terms['settlement_status'] == 'unsupported'
            assert (facts['settlement_error'] == terms['settlement_error']
                    == 'unsupported sandbox CNY bond precision')
            assert (facts['settlement_expected'] is facts['rounding_adjustment']
                    is facts['settlement_residual'] is None)
            assert terms['settlement_expected'] is None
        else:
            assert facts['settlement_status'] == terms['settlement_status'] == 'supported'
            assert facts['settlement_error'] is terms['settlement_error'] is None
            assert D(facts['settlement_expected']) == cny['expected']
            assert D(facts['rounding_adjustment']) == 0
            assert D(facts['settlement_residual']) == residual
        assert D(event['money']['rub']['residual']) == 0
    assert events.index(orders[-1]) < events.index(polls[0])


@pytest.mark.parametrize('scenario,args', [
    (scenarios.test_rub_cny_normal_application, ('INDEX', 'CROSS')),
    (scenarios.test_rub_cny_normal_application, ('COMPOSITE', 'MIX')),
    (scenarios.test_rub_cny_normal_application, ('ACCOUNT', 'MIXED')),
    (scenarios.test_shared_cny_owners_and_unallocated_cash, ()),
    (scenarios.test_existing_cny_before_lot_rounding, ('sufficient',)),
    (scenarios.test_existing_cny_before_lot_rounding, ('partial',)),
    (scenarios.test_reverse_exchange_only_for_rub_purchase, ()),
    (scenarios.test_isolated_sale_does_not_fund_sibling, ()),
])
def test_all_currency_scenarios_with_competing_rub_bond(
        offline_live, tmp_path, monkeypatch, scenario, args):
    """The original eight scenarios must select CNY even when RUB is returned first."""
    live, broker = offline_live
    rub_bond = replace(broker.metadata['C'], uid='RUB-BOND', currency='rub', class_code='TQCB')
    broker.metadata = {'RUB-BOND': rub_bond, **broker.metadata}
    scenario(live, tmp_path, monkeypatch, *args)
    configs = [value for value in live[0].configs.values() if 'instruments' in value]
    assert len(configs) == 3
    assert all(value['preferred_currency'] == 'CNY' for value in configs)
    assert not any(call.kwargs.get('instrument_id') == 'RUB-BOND'
                   for call in broker.client.orders.post_order.call_args_list)
    broker.client.orders.get_order_price.assert_not_called()
    broker.client.operations.get_operations.assert_not_called()


@pytest.mark.parametrize('scenario_case', [
    (scenarios.test_rub_cny_normal_application, ('INDEX', 'CROSS')),
    (scenarios.test_rub_cny_normal_application, ('COMPOSITE', 'MIX')),
    (scenarios.test_rub_cny_normal_application, ('ACCOUNT', 'MIXED')),
    (scenarios.test_shared_cny_owners_and_unallocated_cash, ()),
    (scenarios.test_existing_cny_before_lot_rounding, ('sufficient',)),
    (scenarios.test_existing_cny_before_lot_rounding, ('partial',)),
    (scenarios.test_reverse_exchange_only_for_rub_purchase, ()),
    (scenarios.test_isolated_sale_does_not_fund_sibling, ()),
])
@pytest.mark.parametrize('catalog_case', [
    (False, False), (False, True), (True, False), (True, True),
])
def test_all_currency_scenarios_with_competing_fx_pairs(
        offline_live, tmp_path, monkeypatch, scenario_case, catalog_case):
    """Setup state and the unmodified Runner use the same explicitly configured FX UID."""
    scenario, args = scenario_case
    reverse, available = catalog_case
    live, broker = offline_live
    pairs = broker.client.instruments.currencies.return_value.instruments
    pairs.insert(0, replace(pairs[0], uid='other-fx', api_trade_available_flag=available,
                            buy_available_flag=available, sell_available_flag=available))
    if reverse:
        pairs.reverse()
    scenario(live, tmp_path, monkeypatch, *args)
    assert not any(call.kwargs.get('instrument_id') == 'other-fx'
                   for call in broker.client.market_data.get_order_book.call_args_list)
    assert not any(call.kwargs.get('instrument_id') == 'other-fx'
                   for call in broker.client.orders.post_order.call_args_list)
    broker.client.orders.get_order_price.assert_not_called()
    broker.client.operations.get_operations.assert_not_called()
