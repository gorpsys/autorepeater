"""Offline monetary contracts; these helpers do not enable live FX execution."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
import inspect
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest
from t_tech.invest import (
    MoneyValue, OrderDirection, OrderExecutionReportStatus, OrderState, OrderType,
    PostOrderResponse,
)
from t_tech.invest.services import OrdersService

from autorepeater.cash_facts import (
    CashBound, FeeInclusion, MoneyAmount, OrderAmounts, PurchaseCashNeed,
    SecurityQuote, SecurityQuoteUnit, order_cash_bound,
)
from autorepeater.execution import ExecutionReceipt
from autorepeater.execution_data import TradeRules
from autorepeater.fx_data import FxLotQuote
from autorepeater.order_execution import TInvestOrderExecutor
from autorepeater.order_plan import OrderIntent


def money(amount, currency='cny'):
    """Explicit native units, never binary floats."""
    return MoneyAmount(currency, Decimal(amount))


def receipt(side='BUY', **changes):
    """Broker facts independent of the money fields."""
    return replace(ExecutionReceipt('bond', side, 'broker-id', 'FILL', 2, 2, {}), **changes)


def amounts(side='BUY', **changes):
    """An order amount includes ACI; fee inclusion remains unknown by default."""
    return replace(OrderAmounts('bond', side, 'broker-id', 2,
                                money('2045.72'), money('1.005')), **changes)


@pytest.mark.parametrize('units,nano,expected', [
    (2045, 720000001, '2045.720000001'), (0, 1, '0.000000001'),
    (0, -1, '-0.000000001'), (-2, -5, '-2.000000005'),
    (123456789012345678901234567890, 1, '123456789012345678901234567890.000000001'),
])
def test_units_nano_are_exact_even_in_small_decimal_context(units, nano, expected):
    """Money boundary preserves nanos, including signed SDK money values."""
    with localcontext() as context:
        context.prec = 6
        assert MoneyAmount.from_units_nano('CNY', units, nano) == money(expected)


@pytest.mark.parametrize('units,nano', [
    (True, 0), ('1', 0), (1, False), (1, 1.5), (1, 1000000000),
    (0, -1000000000), (1, -1), (-1, 1), (None, 0),
])
def test_invalid_units_are_not_coerced(units, nano):
    """Invalid integer components cannot manufacture money."""
    with pytest.raises(ValueError, match='money units'):
        MoneyAmount.from_units_nano('rub', units, nano)


@pytest.mark.parametrize('currency,value', [
    ('', Decimal(1)), (None, Decimal(1)), (' CNY', Decimal(1)), ('ruble', Decimal(1)),
    ('123', Decimal(1)), ('cny', 1), ('cny', True), ('cny', Decimal('NaN')),
    ('cny', Decimal('Infinity')), ('cny', Decimal('-Infinity')),
])
def test_malformed_money_is_rejected(currency, value):
    """Currency and numeric dimension must be explicit and finite."""
    with pytest.raises(ValueError):
        MoneyAmount(currency, value)


@pytest.mark.parametrize('side,expected', [('BUY', '2046.725'), ('SELL', '2044.715')])
def test_unknown_fee_semantics_give_bounds_and_never_receipt_cash(side, expected):
    """An uncertain inclusion rule yields a bound, not attributed proceeds."""
    facts = receipt(side)
    bound = order_cash_bound(facts, amounts(side))
    assert bound == CashBound('broker-id', side, money(expected))
    assert not facts.cash
    with pytest.raises(FrozenInstanceError):
        bound.amount = money('0')


@pytest.mark.parametrize('inclusion,side,expected', [
    (FeeInclusion.INCLUDED, 'BUY', '100'), (FeeInclusion.INCLUDED, 'SELL', '100'),
    (FeeInclusion.SEPARATE, 'BUY', '103'), (FeeInclusion.SEPARATE, 'SELL', '97'),
    (FeeInclusion.UNKNOWN, 'BUY', '103'), (FeeInclusion.UNKNOWN, 'SELL', '97'),
])
def test_fee_is_applied_once_for_explicit_field_semantics(inclusion, side, expected):
    """Only separately evidenced field semantics permit omitting the adjustment."""
    fields = amounts(side, total=money('100'), executed_commission=money('3'),
                     fee_inclusion=inclusion)
    assert order_cash_bound(receipt(side), fields).amount == money(expected)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_explicit_zero_fee_is_valid_but_does_not_prove_net_cash(side):
    """An explicit zero differs from an absent commission field."""
    facts = receipt(side)
    fields = amounts(side, executed_commission=money('0'))
    assert order_cash_bound(facts, fields).amount == money('2045.72')
    assert not facts.cash


@pytest.mark.parametrize('changes', [
    {'executed_commission': None}, {'executed_commission': '1.005'},
    {'executed_commission': money('-0.000000001')},
    {'executed_commission': money('1.005', 'rub')},
    {'total': None}, {'total': Decimal(100)}, {'total': money('-1')},
    {'fee_inclusion': 'INCLUDED'}, {'uid': ''}, {'order_id': ''},
    {'side': 'HOLD'}, {'lots': True}, {'lots': 0},
])
def test_missing_or_inconsistent_money_has_no_usable_bound(changes):
    """Incomplete money affects only the dependent money calculation."""
    assert order_cash_bound(receipt(), amounts(**changes)) is None


@pytest.mark.parametrize('changes', [
    {'uid': 'other'}, {'side': 'SELL'}, {'order_id': 'request-id'},
    {'status': 'NEW', 'lots_executed': 0}, {'status': 'PARTIALLYFILL', 'lots_executed': 1},
    {'status': 'REJECTED'}, {'lots_requested': 3}, {'lots_executed': 1},
    {'lots_requested': True}, {'lots_executed': True},
])
def test_bound_requires_complete_matching_broker_facts(changes):
    """Neither a request ID nor another instrument's fill authorizes a bound."""
    assert order_cash_bound(receipt(**changes), amounts()) is None


def test_sell_bound_never_becomes_negative_and_aci_is_not_added_again():
    """Clipping proceeds and one-time ACI accounting prevent invented cash."""
    assert order_cash_bound(receipt('SELL'), amounts(
        'SELL', total=money('1'), executed_commission=money('2'))).amount == money('0')
    # 2045.72 is already the bond total, including ACI for both pieces.
    assert order_cash_bound(receipt(), amounts()).amount == money('2046.725')


def test_security_midpoint_units_nominal_and_aci_are_separate_from_fx():
    """A percent bond quote and direct FX quote have distinct denominators."""
    quote = SecurityQuote('cny', Decimal('99'), Decimal('101'),
                          SecurityQuoteUnit.PERCENT_NOMINAL, money('1000'), money('22.86'))
    assert quote.estimate_per_piece() == money('1022.86')
    assert quote.estimate_per_piece().amount * 2 == Decimal('2045.72')
    direct = SecurityQuote('rub', Decimal('280'), Decimal('284'),
                           SecurityQuoteUnit.MONEY_PER_PIECE)
    assert direct.estimate_per_piece() == money('282', 'rub')
    fx = fx_quote()
    assert fx.midpoint_per_unit == Decimal('12')
    assert quote.estimate_per_piece().amount * fx.midpoint_per_unit == Decimal('12274.32')


@pytest.mark.parametrize('changes', [
    {'bid': Decimal(0)}, {'ask': Decimal('98')}, {'bid': Decimal('NaN')},
    {'unit': 'percent'}, {'nominal': None}, {'nominal': money('1000', 'usd')},
    {'nominal': money('0')}, {'accrued_interest': None},
    {'accrued_interest': money('1', 'rub')}, {'accrued_interest': money('-1')},
])
def test_bond_valuation_rejects_ambiguous_or_inconsistent_units(changes):
    """Missing nominal or ACI cannot silently become zero or another currency."""
    quote = SecurityQuote('cny', Decimal(99), Decimal(101),
                          SecurityQuoteUnit.PERCENT_NOMINAL, money('1000'), money('22.86'))
    with pytest.raises(ValueError):
        replace(quote, **changes).estimate_per_piece()


def fx_quote(**changes):
    """Quote is RUB per nominal base units; lot is base units, not price units."""
    return replace(FxLotQuote('cny-rub', 'cny', 'rub', Decimal(10), 1000,
                              Decimal(119), Decimal(121)), **changes)


def test_fx_nominal_and_lot_are_not_applied_twice():
    """Nominal scales price while a lot scales base currency units."""
    quote = fx_quote()
    assert quote.midpoint_per_unit == Decimal(12)
    assert quote.quote_amount('BUY', 2) == money('24200', 'rub')
    assert quote.quote_amount('SELL', 2) == money('23800', 'rub')
    assert quote.base_amount(2) == money('2000')


@pytest.mark.parametrize('changes', [
    {'base_currency': 'rub'}, {'quote_currency': 'usd'}, {'nominal': Decimal(0)},
    {'lot_size': True}, {'lot_size': 0}, {'bid': Decimal(0)},
    {'ask': Decimal(100)}, {'uid': ''},
])
def test_only_well_formed_direct_rub_pairs_are_usable(changes):
    """Pair identity must prove the direct RUB route before valuation."""
    with pytest.raises(ValueError):
        fx_quote(**changes)


@pytest.mark.parametrize('side,ready,cash,base,changes,expected', [
    ('BUY', True, '24200', '0', {}, True),
    ('BUY', True, '24199.999999999', '0', {}, False),
    ('BUY', True, '24200', '0', {'buy_money_amount': Decimal('24199')}, False),
    ('BUY', True, '24200', '0', {'buy_max_lots': 1}, False),
    ('SELL', True, '0', '2000', {}, True),
    ('SELL', True, '0', '1999.999999999', {}, False),
    ('SELL', True, '0', '2000', {'sell_max_lots': 1}, False),
    ('BUY', False, '24200', '0', {}, False),
    ('BUY', True, '24200', '0', {'api_trade_available': False}, False),
    ('BUY', True, '24200', '0', {'bestprice_order_available': False}, False),
    ('BUY', True, '24200', '0', {'currency': 'cny'}, False),
    ('BUY', True, '24200', '0', {'lot': 1}, False),
])
# Six independent dimensions keep the side-specific boundary cases readable.
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def test_fx_side_readiness_and_own_caps_are_required(side, ready, cash, base, changes, expected):
    """Current availability, own caps and permissions all constrain either side."""
    rule = replace(TradeRules(1000, 'rub', True, True, Decimal('24200'), 2, 2), **changes)
    assert fx_quote().can_trade(side, 2, rule, {'rub': Decimal(cash), 'cny': Decimal(base)},
                                ready=ready) is expected


def test_purchase_fee_can_use_reserve_without_requiring_its_refill():
    """The pure purchase predicate does not demand a second reserved cash balance."""
    # Leaf budget=1000, target securities=900, reserve=100: no extra fee percentage.
    need = PurchaseCashNeed(money('900'), money('5'))
    assert need.debit_upper == money('905')
    assert not need.requires_exchange(money('1000'))
    # Even below the original target reserve, a purchase including its fee fits.
    assert not need.requires_exchange(money('905'))
    assert need.requires_exchange(money('904.999999999'))
    # A reserve shortage alone never starts FX when no security purchase exists.
    assert not PurchaseCashNeed(money('0'), money('0')).requires_exchange(money('0'))


@pytest.mark.parametrize('changes', [
    {'purchase': money('-1')}, {'fee_upper': money('-1')},
    {'fee_upper': money('1', 'rub')},
])
def test_purchase_need_rejects_invalid_or_cross_currency_fields(changes):
    """A fee is spendable only in the matching monetary dimension."""
    with pytest.raises(ValueError):
        replace(PurchaseCashNeed(money('900'), money('5')), **changes)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('fee', [None, MoneyValue('cny', 0, 0), MoneyValue('rub', 1, 5000000)])
def test_bestprice_uses_only_direct_broker_id_facts_without_optional_money_rpcs(side, fee):
    """The real SDK boundary uses broker IDs and sends no price or optional RPCs."""
    orders = create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True)
    direction = (OrderDirection.ORDER_DIRECTION_BUY if side == 'BUY'
                 else OrderDirection.ORDER_DIRECTION_SELL)
    fields = {'instrument_uid': 'bond', 'direction': direction, 'order_id': 'broker-id',
              'execution_report_status': OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL,
              'lots_requested': 2, 'lots_executed': 2,
              'total_order_amount': MoneyValue('cny', 2045, 720000000)}
    if fee is not None:
        fields['executed_commission'] = fee
    orders.post_order.return_value = PostOrderResponse(**fields)
    orders.get_order_state.return_value = OrderState(**fields)
    executor = TInvestOrderExecutor(SimpleNamespace(orders=orders),
                                    order_id_factory=lambda: 'request-id')
    result = executor.submit_order('dst', OrderIntent('bond', side, 2, 1, {(): Decimal(2)}))
    assert result == receipt(side)
    assert executor.get_order_state('dst', result.order_id) == receipt(side)
    assert orders.mock_calls == [call.post_order(
        account_id='dst', instrument_id='bond', quantity=2, direction=direction,
        order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id='request-id'),
        call.get_order_state(account_id='dst', order_id='broker-id')]


@pytest.mark.parametrize('field,value', [('uid', 1), ('order_id', True)])
def test_malformed_identity_cannot_validate_its_own_money(field, value):
    """Two matching malformed identifiers still do not prove order identity."""
    assert order_cash_bound(receipt(**{field: value}), amounts(**{field: value})) is None


def test_included_fee_cannot_exceed_its_total():
    """Explicit inclusion must be internally consistent before any money bound."""
    fields = amounts(total=money('1'), executed_commission=money('2'),
                     fee_inclusion=FeeInclusion.INCLUDED)
    assert order_cash_bound(receipt(), fields) is None


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('bad_cap', [True, -1, Decimal(2), None])
def test_fx_rejects_malformed_own_lot_caps(side, bad_cap):
    """No margin or coercion fallback when the own cap is absent or malformed."""
    rule = TradeRules(1000, 'RUB', True, True, Decimal('24200'), bad_cap, bad_cap)
    cash = {'rub': Decimal('24200'), 'cny': Decimal('2000')}
    assert not fx_quote().can_trade(side, 2, rule, cash, ready=True)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_fx_missing_native_cash_does_not_authorize_a_trade(side):
    """A route and FILL do not prove ready, owned money for the next purchase."""
    rule = TradeRules(1000, 'rub', True, True, Decimal('24200'), 2, 2)
    assert not fx_quote().can_trade(side, 2, rule, {}, ready=True)


@pytest.mark.parametrize('lots', [0, -1, True, Decimal(1)])
def test_fx_lot_count_is_explicit_positive_integer(lots):
    """No fractional or bool trading lots are admitted at the units boundary."""
    with pytest.raises(ValueError):
        fx_quote().base_amount(lots)


def test_invalid_side_and_money_quote_aci_are_rejected():
    """No implicit side or second ACI addition for a money-valued bond quote."""
    with pytest.raises(ValueError):
        fx_quote().quote_amount('HOLD', 1)
    with pytest.raises(ValueError):
        SecurityQuote('cny', Decimal(100), Decimal(102), SecurityQuoteUnit.MONEY_PER_PIECE,
                      accrued_interest=money('1')).estimate_per_piece()
    with pytest.raises(ValueError):
        PurchaseCashNeed(money('900'), money('5')).requires_exchange(money('1000', 'rub'))
