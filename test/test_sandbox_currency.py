"""Independent native cash, bond and currency-lot financial assertions fail closed."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from test.test_sandbox_support import fixture_session

import pytest

from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit
from scripts import sandbox_multicurrency as subject
from scripts.sandbox_lifecycle import SandboxFailure

__all__ = ['fixture_session']


def observation():
    """Cash and securities are separate, including base currency lot remnants."""
    return subject.NativeState({'bond': D(1)}, {'rub': D(1000), 'cny': D(70)},
                               {'rub': D(0), 'cny': D(0)})


def test_bond_midpoint_converts_percent_and_adds_aci_once():
    """Execution price is irrelevant to midpoint reporting and ACI is not a fee."""
    quote = SecurityQuote('cny', D(98), D(100), SecurityQuoteUnit.PERCENT_NOMINAL,
                          MoneyAmount('cny', D(1000)), MoneyAmount('cny', D(7)))
    assert subject.native_midpoint(quote) == D(997)
    assert subject.native_midpoint(SecurityQuote(
        'rub', D(100), D(102), SecurityQuoteUnit.MONEY_PER_PIECE)) == D(101)


@pytest.mark.parametrize('fault', ['inverted', 'currency', 'nominal', 'aci'])
def test_bad_bond_units_never_silently_become_rubles(fault):
    """The oracle independently rejects dimensionally inconsistent estimates."""
    quote = SecurityQuote('cny', D(98), D(100), SecurityQuoteUnit.PERCENT_NOMINAL,
                          MoneyAmount('cny', D(1000)), MoneyAmount('cny', D(7)))
    changes = {'inverted': {'bid': D(101)}, 'currency': {'nominal': MoneyAmount('rub', D(1000))},
               'nominal': {'nominal': MoneyAmount('cny', D(0))},
               'aci': {'accrued_interest': None}}
    with pytest.raises(SandboxFailure):
        subject.native_midpoint(replace(quote, **changes[fault]))


def test_fx_money_and_bond_positions_reconcile_independently():
    """An FX BUY credits units, its RUB fee does not masquerade as securities."""
    before = observation()
    trades = [subject.TradeFact('fx', 'BUY', 2, 1000, 'rub', D(24000), D(12), 'cny'),
              subject.TradeFact('bond', 'BUY', 1, 1, 'cny', D(997), D(1), None)]
    after = subject.NativeState({'bond': D(2)}, {'rub': D(-23012), 'cny': D(1072)}, {})
    # Give the disposable account sufficient original RUB; preserve the exact deltas.
    before = replace(before, money={**before.money, 'rub': D(30000)})
    after = replace(after, money={**after.money, 'rub': D(5988)})
    subject.reconcile(before, after, trades)
    assert subject.fees_by_currency(trades) == {'rub': D(12), 'cny': D(1)}
    assert subject.cash_value(after.money, {'rub': D(1), 'cny': D(12)}) == D(18852)


@pytest.mark.parametrize('fault', ['cash', 'blocked', 'security', 'fx-as-security', 'fee', 'aci'])
def test_corrupted_currency_conservation_is_a_failure(fault):
    """Each monetary dimension has a real failure, never a broadened RUB tolerance."""
    before = observation()
    trade = subject.TradeFact('bond', 'SELL', 1, 1, 'cny', D(997), D(1), None)
    after = subject.NativeState({}, {'rub': D(1000), 'cny': D(1066)}, {})
    if fault == 'cash':
        after = replace(after, money={'rub': D(1000), 'cny': D(1065)})
    if fault == 'blocked':
        after = replace(after, blocked={'cny': D(1)})
    if fault == 'security':
        after = replace(after, quantities={'bond': D(1)})
    if fault == 'fx-as-security':
        after = replace(after, quantities={'fx': D(1000)})
    if fault == 'fee':
        trade = replace(trade, commission=D(0))
    if fault == 'aci':
        trade = replace(trade, total=D(1004))
    with pytest.raises(SandboxFailure):
        subject.reconcile(before, after, [trade])


def test_sell_currency_only_changes_native_money():
    """Reverse exchange spends base units and credits RUB net of the reported fee."""
    before = replace(observation(), money={'rub': D(0), 'cny': D(1070)})
    after = replace(observation(), money={'rub': D(11990), 'cny': D(70)})
    subject.reconcile(before, after, [
        subject.TradeFact('fx', 'SELL', 1, 1000, 'rub', D(12000), D(10), 'cny')])


@pytest.mark.parametrize('side,original,expected', [
    ('BUY', {'rub': D(30000)}, {'rub': D(17990), 'cny': D(2)}),
    ('SELL', {'cny': D(2000)}, {'rub': D(11990), 'cny': D(1996)}),
])
@pytest.mark.parametrize('residual', [D('.000001'), D('-.000001')])
def test_diagnostic_money_deltas_preserve_exact_native_residual(side, original, expected, residual):
    """Diagnostic BUY/SELL arithmetic includes FX units, ACI totals and one fee per fill."""
    trades = [subject.TradeFact('fx', side, 1, 1000, 'rub', D(12000), D(10), 'cny'),
              subject.TradeFact('bond', side, 1, 1, 'cny', D(997), D(1), None)]
    before = subject.NativeState({}, original, {})
    actual = {**expected, 'cny': expected['cny'] + residual}
    after = subject.NativeState({}, actual, {})
    facts = subject.money_deltas(before, after, trades)
    assert facts == {code: {
        'before': original.get(code, D(0)), 'expected': expected[code], 'actual': actual[code],
        'expected_delta': expected[code] - original.get(code, D(0)),
        'actual_delta': actual[code] - original.get(code, D(0)),
        'residual': actual[code] - expected[code],
    } for code in ('rub', 'cny')}
    assert before.money == original and after.money == actual


@pytest.mark.parametrize('available,expected', [(D(0), 2), (D(500), 1), (D(1050), 0)])
def test_existing_currency_subtracted_before_rounding(available, expected):
    """Reserve is one leaf allocation, not a second fee per order or FX lot."""
    assert subject.required_fx_lots(D(950), D(100), available, 1000) == expected


def test_commission_consumes_reserve_without_requiring_restore():
    """Sufficient purchase cash may leave reserve below target and require no exchange."""
    assert subject.required_fx_lots(D(900), D(100), D(950), 1000,
                                    commission=D(5)) == 0


def test_setup_fx_waits_for_money_without_adding_security(session, monkeypatch):
    """Currency setup shares the checked FILL lifecycle but waits for native units."""
    from t_tech.invest import (  # pylint: disable=import-outside-toplevel
        OrderDirection, OrderExecutionReportStatus, OrderState, PostOrderResponse,
    )
    facts = {'order_id': 'broker', 'instrument_uid': 'fx',
             'direction': OrderDirection.ORDER_DIRECTION_BUY,
             'execution_report_status': OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
             'lots_requested': 2, 'lots_executed': 2}
    session.client.orders.post_order.return_value = PostOrderResponse(**facts)
    session.client.orders.get_order_state.return_value = OrderState(**facts)
    before = SimpleNamespace(quantities={'bond': D(1)}, available_cash={'cny': D(70)})
    monkeypatch.setattr(session, 'state', lambda _: before)
    waited = []
    monkeypatch.setattr(session, 'wait_positions', lambda *args, **kw: waited.append((args, kw)))
    session.buy_setup('dst', SimpleNamespace(uid='fx', lot=1000), 2, base_currency='cny')
    assert waited == [(('dst', {'bond': D(1)}), {'minimum_cash': {'cny': D(2070)}})]
    session.client.orders.post_order.assert_called_once()


def test_currency_settlement_waits_for_actual_available_units(session, monkeypatch):
    """Physical securities equality alone cannot confirm an FX credit."""
    states = iter([
        SimpleNamespace(limits_ready=True, active_orders=(), quantities={},
                        available_cash={'cny': D(0)}),
        SimpleNamespace(limits_ready=True, active_orders=(), quantities={},
                        available_cash={'cny': D(1000)})])
    monkeypatch.setattr(session, 'state', lambda _: next(states))
    monkeypatch.setattr(session, 'monotonic', lambda: 0)
    slept = []
    monkeypatch.setattr(session, 'sleep', slept.append)
    assert session.wait_positions('dst', {}, minimum_cash={'cny': D(1000)}).available_cash == {
        'cny': D(1000)}
    assert slept == [2]


def test_two_purchases_may_spend_reserve_without_second_floor():
    """The second 450 purchase is valid at 545 cash despite the original 100 reserve."""
    initial = subject.NativeState({}, {'rub': D(1000)}, {})
    first = subject.NativeState({'one': D(1)}, {'rub': D(545)}, {})
    final = subject.NativeState({'one': D(1), 'two': D(1)}, {'rub': D(90)}, {})
    subject.reconcile(initial, first, [
        subject.TradeFact('one', 'BUY', 1, 1, 'rub', D(450), D(5), None)])
    subject.reconcile(first, final, [
        subject.TradeFact('two', 'BUY', 1, 1, 'rub', D(450), D(5), None)])
    assert final.money['rub'] < D(100)
    assert subject.required_fx_lots(D(0), D(100), D(90), 1000) == 0


@pytest.mark.parametrize('fault', ['side', 'lots', 'bool', 'total', 'currency', 'commission'])
def test_invalid_trade_facts_fail_closed(fault):
    """Money facts are not repaired or rounded into an apparently valid trade."""
    trade = subject.TradeFact('fx', 'BUY', 1, 1000, 'rub', D(12000), D(10), 'cny')
    changes = {'side': {'side': 'other'}, 'lots': {'lots': 0}, 'bool': {'lots': True},
               'total': {'total': D('NaN')}, 'currency': {'base_currency': 'rub'},
               'commission': {'commission': D(-1)}}
    with pytest.raises(SandboxFailure):
        subject.reconcile(observation(), observation(), [replace(trade, **changes[fault])])


@pytest.mark.parametrize('fault', ['missing', 'rate', 'quote-units', 'funding'])
def test_invalid_valuation_and_funding_oracles_reject(fault):
    """Missing rates, quotation units and lot sizes cannot be guessed."""
    with pytest.raises(SandboxFailure):
        if fault == 'missing':
            subject.cash_value({'cny': D(1)}, {'rub': D(1)})
        elif fault == 'rate':
            subject.cash_value({'cny': D(1)}, {'rub': D(1), 'cny': D(0)})
        elif fault == 'quote-units':
            subject.native_midpoint(SecurityQuote('rub', D(1), D(1), 'unknown'))
        else:
            subject.required_fx_lots(D(1), D(1), D(0), 0)
