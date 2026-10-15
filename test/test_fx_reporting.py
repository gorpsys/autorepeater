"""Loaded native estimates and verified execution facts stay distinct in diagnostics."""
from dataclasses import replace
from decimal import Decimal as D
import logging
from test.test_fx_execution import receipt, setup_plan
from test.test_fx_funding import funding
from test.test_order_plan import leaf, rules

import pytest

from autorepeater import reporting
from autorepeater.cash_facts import FeeInclusion, MoneyAmount
from autorepeater.execution import _submit
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.strategy_data import InstrumentType, PortfolioEntry


def test_debug_funding_reports_native_marks_rates_needs_route_and_lot_residual(caplog):
    """Conditional cap0 buys must be visible even though the legacy BUY list is empty."""
    plan = funding(lot=10)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_rebalance_plan(plan, True)
    messages = '\n'.join(caplog.messages)
    for fragment in (
            'Valuation uid=C native_price=60.0 currency=cny rub_price=720.0',
            'FX valuation pair=cny-rub currency=cny rub_per_unit=12.0',
            'market_time=2026-10-15T00:00:00+00:00', 'received_time=',
            'bid=12.0 ask=12.0 nominal=1.0 lot=10',
            'Funding path=() available_native={rub: 1000.0}',
            'security_debit_estimate={cny: 60.0}', 'reserve_native={cny: 8.333333333',
            'projected_residual_including_lot_surplus={rub: 160.0, cny: 10.0}',
            'FX route=rub->cny pair=cny-rub side=BUY lots=7',
            "dependent_uids=('C',)", 'Funding BUY uid=C lots=1 executable_now=False',
            'fx_dependencies=', 'funded_buys=1 exchanges=1', 'debug=True'):
        assert fragment in messages
    assert 'SKIP: no executable orders' not in messages
    assert 'Decimal(' not in messages and 'E-' not in messages


def test_unfundable_demand_has_safe_reason_without_claimed_exchange(caplog):
    """A too-large FX lot is a deferred demand, never a completed exchange."""
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_rebalance_plan(funding(lot=1000), True)
    messages = '\n'.join(caplog.messages)
    assert ('Defer funding uid=C unfilled_lots=1 '
            'reason=native cash/FX lot/own caps/budget') in messages
    assert 'Funding demand uid=C lots=1 native_estimate=60.0 currency=cny' in messages
    assert 'FX route=' not in messages
    assert 'confirmed_cash' not in messages


def test_reverse_route_and_existing_currency_are_explicit_estimates(caplog):
    """Report the permitted reverse RUB purchase, retaining native CNY residual."""
    plan = funding(leaf(target={'R': D(1)}, budget='1000'), cash={'cny': D(100)},
                   own=rules('R'), prices={'R': D(450)})
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        reporting.print_rebalance_plan(plan, False)
    messages = '\n'.join(caplog.messages)
    assert 'FX route=cny->rub pair=cny-rub side=SELL lots=38' in messages
    assert 'Valuation uid=R native_price=450.0 currency=rub rub_price=450.0' in messages
    assert 'projected_residual_including_lot_surplus=' in messages
    assert 'confirmed_cash' not in messages


@pytest.mark.parametrize('kind', [
    InstrumentType.SHARE, InstrumentType.OTHER, InstrumentType.CURRENCY])
def test_valued_position_never_prints_raw_sdk_diagnostic(kind):
    """Broker valuation is identified separately from native midpoint and RUB mark."""
    entry = PortfolioEntry('C', kind, 'rub', D(500), D(2),
                           diagnostic_text='raw SDK token=DO-NOT-LOG',
                           native_price=D(60), settlement_currency='cny', rub_price=D(720))
    text = reporting.strategy_position_to_string(entry)
    assert 'native_price=60.0 currency=cny rub_price=720.0' in text
    assert 'broker_price=500.0 broker_currency=rub' in text
    assert 'quantity=2.0' in text and 'DO-NOT-LOG' not in text


@pytest.mark.parametrize('side,expected', [('BUY', 'debit_upper'), ('SELL', 'credit_lower')])
@pytest.mark.parametrize('inclusion', list(FeeInclusion))
def test_confirmed_money_is_bound_and_commission_not_fabricated_cash(
        side, expected, inclusion, caplog):
    """The submit boundary reports matching FILL money without asserting exact net cash."""
    _, _, plan, _, executor, _, _ = setup_plan()
    intent = replace(plan.funding.demands[0], side=side)
    result = receipt(intent, '60', '.5', 'cny')
    result = replace(result, amounts=replace(result.amounts, fee_inclusion=inclusion))
    executor.submit_order.return_value = result
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert _submit('dst', intent, executor) is result
    messages = '\n'.join(caplog.messages)
    amount = '60.0' if inclusion == FeeInclusion.INCLUDED else (
        '60.5' if side == 'BUY' else '59.5')
    assert f'cash_bound={expected} currency=cny amount={amount}' in messages
    assert 'executed_commission=0.5 commission_currency=cny' in messages
    assert 'confirmed_cash={}' in messages and result.cash == {}
    assert 'exact_debit' not in messages and 'exact_credit' not in messages


@pytest.mark.parametrize('damage', ['missing', 'identity', 'currency', 'negative', 'raw'])
def test_incomplete_or_unmatched_money_not_reported_as_confirmed(damage, caplog):
    """Unproven optional amounts cannot leak raw objects or become reported cash facts."""
    _, _, plan, _, executor, _, _ = setup_plan()
    intent = plan.funding.demands[0]
    result = receipt(intent, '60', '.5', 'cny')
    changes = {'missing': {'executed_commission': None}, 'identity': {'order_id': 'wrong'},
               'currency': {'executed_commission': MoneyAmount('rub', D('.5'))},
               'negative': {'total': MoneyAmount('cny', D(-1))},
               'raw': {'total': 'token=DO-NOT-LOG'}}
    result = replace(result, amounts=replace(result.amounts, **changes[damage]))
    executor.submit_order.return_value = result
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert _submit('dst', intent, executor) is result
    messages = '\n'.join(caplog.messages)
    assert 'cash_bound=unavailable confirmed_cash={}' in messages
    assert 'executed_commission=' not in messages and 'DO-NOT-LOG' not in messages


def test_documented_exact_receipt_cash_reported_without_inference(caplog):
    """Only the existing executor cash contract can supply exact proceeds."""
    _, _, plan, _, executor, _, _ = setup_plan()
    intent = replace(plan.funding.demands[0], side='SELL')
    result = replace(receipt(intent), cash={'cny': D('59.5')})
    executor.submit_order.return_value = result
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        _submit('dst', intent, executor)
    assert any('cash_bound=unavailable confirmed_cash={cny: 59.5}' in message
               for message in caplog.messages)


def test_deferred_buy_after_fx_explains_fresh_caps_and_retains_fill(caplog):
    """A completed FX with new cap0 needs an explicit stop reason, no rollback claim."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    data.get_destination.return_value = replace(
        plan.snapshot, available_cash={'rub': D(172), 'cny': D(69)})
    data.get_trade_rules.return_value = rules('C', currency='cny', buy=0)
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        results = broker.execute('dst', plan, data, executor, value)
    assert [item.uid for item in results] == ['cny-rub']
    assert any('Defer remaining purchases: native cash/FX lot/own caps/budget' in message
               for message in caplog.messages)
    assert any('stage=execution' in message and 'rub_per_unit=12.0' in message
               for message in caplog.messages)
    executor.submit_order.assert_called_once()


@pytest.mark.parametrize('change,reason', [
    ({'available_cash': {'rub': D(0), 'cny': D(0)}}, 'owned cash exceeds fresh availability'),
    ({'quantities': {'other': D(1)}}, 'positions differ from fixed ownership')])
def test_initial_fresh_mismatch_explains_deferral(change, reason, caplog, monkeypatch):
    """Fresh guards report their reason without changing ownership or money."""
    broker, value, plan, data, executor, _, _ = setup_plan()
    # A confirmed exchange leads to fresh guards; mismatch must stop before the paper.
    data.get_destination.return_value = replace(plan.snapshot, **change)
    data.get_trade_rules.return_value = rules('C', currency='cny')
    executor.submit_order.side_effect = lambda account, order: receipt(order, '828')
    monkeypatch.setattr('autorepeater.execution_market.SETTLEMENT_TIMEOUT', 0)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        broker.execute('dst', plan, data, executor, value)
    assert any(reason in message for message in caplog.messages)
    executor.submit_order.assert_called_once()
