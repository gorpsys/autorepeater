"""Stage-six regression through the public runner and actual SDK adapter contracts."""
from decimal import Decimal as D
import logging
from test.test_composite_strategy import make_composite
from test.test_fx_runtime import fixture_sdk_runtime, launch
from unittest.mock import patch

import pytest

from autorepeater import reporting, runner as application
from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.strategy_plan import exact_sum

__all__ = ['fixture_sdk_runtime']


def nested_prepared():
    """Two equally weighted leaves inside a half-funded root, using real ACCOUNT."""
    config = AccountConfig('synthetic', '0004', D('.1'), D('.01'))
    children = [AccountStrategy(PreparedAccountSource('0004', config)) for _ in range(2)]
    nested = make_composite(children, weights=['0.5', '0.5'])
    root = make_composite([nested], weights=['0.5'])
    return PreparedStrategy(lambda value: value, root, 'nested')


def source_facts(response):
    """Compare populated SDK fields; copying an unset SDK sentinel changes its identity."""
    return [(item.instrument_uid, item.instrument_type, item.quantity.units,
             item.quantity.nano, item.current_price.currency, item.current_price.units,
             item.current_price.nano, item.blocked, item.blocked_lots.units,
             item.blocked_lots.nano) for item in response.positions]


def test_next_pass_after_fx_success_reuses_currency_and_never_repeats_exchange(sdk_runtime):
    """A blocked dependent buy is retried only by a new pass with fresh broker facts."""
    client, state = sdk_runtime
    caps = client.orders.get_max_lots.side_effect

    def blocked_after_fx(*, request):
        response = caps(request=request)
        if request.instrument_id == 'C' and state.cash['cny']:
            response.buy_limits.buy_max_lots = 0
        return response

    client.orders.get_max_lots.side_effect = blocked_after_fx
    first = launch(client)
    assert first.funding.exchanges
    assert state.held == 0 and state.cash == {'rub': D(219), 'cny': D(65)}
    assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list] == [
        'cny-rub']
    client.orders.get_max_lots.side_effect = caps
    second = launch(client)
    assert not second.funding.exchanges
    assert state.held == 1 and state.cash == {'rub': D(219), 'cny': D(4)}
    third = launch(client)
    assert not third.funding.demands and not third.funding.exchanges
    assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list] == [
        'cny-rub', 'C']
    assert len(state.requests) == len(set(state.requests)) == 2
    assert all(call.kwargs['account_id'] == 'dst'
               for call in client.orders.post_order.call_args_list)
    client.orders.get_order_price.assert_not_called()
    client.operations.get_operations.assert_not_called()
    client.operations.get_operations_by_cursor.assert_not_called()


@pytest.mark.parametrize('debug', [True, False])
def test_nested_shared_uid_account_sources_readonly_and_unallocated_cash_preserved(
        sdk_runtime, debug, caplog):
    """One shared FX preserves isolation and RUB room for each owner's fee."""
    client, state = sdk_runtime
    state.cash['rub'] = D(4000)
    portfolio = client.operations.get_portfolio.side_effect
    observed_sources = []

    def observe_source(*, account_id):
        result = portfolio(account_id=account_id)
        if account_id == '0004':
            observed_sources.append((result, source_facts(result)))
        return result

    client.operations.get_portfolio.side_effect = observe_source
    with patch.object(application.Runner, '_create_client', autospec=True) as connect, \
            patch('autorepeater.repeater.reporting.print_rebalance_plan',
                  wraps=reporting.print_rebalance_plan) as report, \
            caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        connect.return_value.__enter__.return_value = client
        application.Runner('synthetic', nested_prepared(), 'dst',
                           application.RunnerParams(debug)).run_sync()
    plan = report.call_args.args[0]
    assert plan.strategy.budget == D(4000)
    assert plan.strategy.cash_floor == D(2200)
    assert plan.funding.cash.protected == {'rub': D(2000), 'cny': D(0)}
    assert plan.funding.spending_limits == {(0, 0): D(900), (0, 1): D(900)}
    exchange, = plan.funding.exchanges
    assert (exchange.uid, exchange.lots) == ('cny-rub', 129)
    assert set(exchange.base_contributions) == {(0, 0), (0, 1)}
    assert exact_sum(exchange.base_contributions.values()) == D(129)
    assert exchange.base_contributions[(0, 0)] >= exact_sum((
        D(75), plan.funding.reserve_shares[(0, 0)]['cny']))
    assert exchange.base_contributions[(0, 1)] >= exact_sum((
        D(45), plan.funding.reserve_shares[(0, 1)]['cny']))
    assert exchange.debits[(0, 0)]['rub'] <= D(950)
    assert exchange.debits[(0, 1)]['rub'] <= D(950)
    purchase, = plan.funding.purchases
    assert purchase.intent.pieces == {(0, 0): D('1.25'), (0, 1): D('.75')}
    assert len(observed_sources) == 2
    for response, facts in observed_sources:
        assert source_facts(response) == facts == [('C', 'bond', 1, 0, 'rub', 700, 0, False, 0, 0)]
    assert [call.kwargs['account_id'] for call in client.operations.get_portfolio.call_args_list
            ].count('0004') == 2
    assert 'Funding protected_native={rub: 2000.0, cny: 0.0}' in caplog.messages
    assert any('path=(0, 1)' in message for message in caplog.messages)
    assert any('FX route=rub->cny' in message for message in caplog.messages)
    if debug:
        assert state.cash == {'rub': D(4000), 'cny': D(0)} and state.held == 0
        assert 'confirmed_cash=' not in '\n'.join(caplog.messages)
        client.orders.post_order.assert_not_called()
        client.orders.get_order_state.assert_not_called()
    else:
        assert state.cash == {'rub': D(2451), 'cny': D(8)} and state.held == 2
        assert [call.kwargs['instrument_id'] for call in client.orders.post_order.call_args_list
                ] == ['cny-rub', 'C']
        assert all(call.kwargs['account_id'] == 'dst'
                   for call in client.orders.post_order.call_args_list)
        assert all('price' not in call.kwargs for call in client.orders.post_order.call_args_list)
        assert not any('FX debit exceeds owner limit' in message
                       for message in caplog.messages)
    client.orders.get_order_price.assert_not_called()
    client.operations.get_operations.assert_not_called()
    client.operations.get_operations_by_cursor.assert_not_called()
