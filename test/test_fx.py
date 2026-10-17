"""Direct FX valuation is immutable and grants no permission to spend money."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from unittest.mock import create_autospec

import pytest

from autorepeater.cash_facts import MoneyAmount
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.fx import FxSnapshot, build_fx_snapshot, select_direct_pair
from autorepeater.fx_data import FxData, FxPair, FxQuote
from autorepeater.strategy_data import PortfolioSnapshot

D = Decimal
NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


def pair(**changes):
    """Explicit ISO identity; UID need not contain a recognizable ticker."""
    return replace(FxPair('cny-tom', 'cny', 'rub', D(10), 1000, True, True, True),
                   **changes)


def quote(**changes):
    """RUB per ten CNY, independently of the thousand-unit trading lot."""
    return replace(FxQuote(pair(), D(119), D(121), NOW - timedelta(seconds=2), NOW),
                   **changes)


def rules(**changes):
    """Fresh own caps; margin and market-order caps are never a substitute."""
    return replace(TradeRules(1000, 'rub', True, True, D(24200), 2, 2), **changes)


def state(cash, *, ready=True):
    """Availability and readiness are read together, never inferred from the quote."""
    return ExecutionSnapshot(PortfolioSnapshot(()), D(0), {}, {}, cash, {}, (), ready)


def test_rub_identity_has_no_reads():
    """Empty and RUB-only snapshots require no FX RPCs or synthetic market data."""
    data = create_autospec(FxData, instance=True, spec_set=True)
    for currencies in ([], ['rub'], ['RUB', 'rub']):
        snapshot = build_fx_snapshot(data, currencies, now=NOW)
        assert snapshot.rate('RUB') == D(1)
        amount = MoneyAmount('rub', D('123456789012345678901234567890.123456789'))
        assert snapshot.to_rub(amount) == amount
        assert snapshot.from_rub(amount, 'rub') == amount
        assert not snapshot.quotes
    assert data.mock_calls == []


def test_one_snapshot_deduplicates_and_preserves_native_units():
    """Midpoint valuation and bid/ask funding estimates have different purposes."""
    data = create_autospec(FxData, instance=True, spec_set=True)
    data.list_pairs.return_value = [pair()]
    data.get_quote.return_value = quote()
    snapshot = build_fx_snapshot(data, ['CNY', 'rub', 'cny'], now=NOW)
    assert snapshot.rate('cny') == D(12)
    assert snapshot.to_rub(MoneyAmount('cny', D('100.000000001'))) == MoneyAmount(
        'rub', D('1200.000000012'))
    assert snapshot.from_rub(MoneyAmount('rub', D(1200)), 'cny') == MoneyAmount('cny', D(100))
    assert snapshot.quotes['cny'].lot_quote.base_amount(2) == MoneyAmount('cny', D(2000))
    assert snapshot.quotes['cny'].lot_quote.quote_amount('BUY', 2) == MoneyAmount('rub', D(24200))
    assert snapshot.quotes['cny'].lot_quote.quote_amount('SELL', 2) == MoneyAmount('rub', D(23800))
    data.list_pairs.assert_called_once_with()
    data.get_quote.assert_called_once_with(pair())
    data.get_trade_rules.assert_not_called()


def test_snapshot_defensively_copies_and_is_frozen():
    """Caller mutations and subsequent reads cannot change a calculation's rates."""
    source = {'CNY': quote()}
    snapshot = FxSnapshot(source, NOW)
    source.clear()
    assert snapshot.rate('cny') == D(12)
    with pytest.raises(TypeError):
        snapshot.quotes['cny'] = quote(bid=D(100))
    with pytest.raises(FrozenInstanceError):
        snapshot.as_of = NOW + timedelta(seconds=1)
    with pytest.raises(FrozenInstanceError):
        snapshot.quotes['cny'].pair.nominal = D(1)


def test_snapshot_rate_is_fixed_despite_decimal_context_changes():
    """Recurring nominal conversion follows the financial precision of 28 digits."""
    snapshot = FxSnapshot({'cny': quote(pair=pair(nominal=D(7)))}, NOW)
    expected = D('17.14285714285714285714285714')
    for precision in (6, 28, 40):
        with localcontext() as context:
            context.prec = precision
            assert snapshot.rate('cny') == expected


def test_conversion_and_lot_estimates_preserve_nanos_in_small_context():
    """Division must not undo exact SDK conversion and multiplication."""
    book = quote(bid=D('119.000000001'), ask=D('120.999999999'))
    snapshot = FxSnapshot({'cny': book}, NOW)
    with localcontext() as context:
        context.prec = 6
        assert snapshot.from_rub(MoneyAmount('rub', D('1200.000000012')), 'cny') == (
            MoneyAmount('cny', D('100.000000001')))
        assert book.lot_quote.quote_amount('BUY', 2) == MoneyAmount('rub', D('24199.9999998'))
        assert book.lot_quote.quote_amount('SELL', 2) == MoneyAmount('rub', D('23800.0000002'))


def test_selector_uses_identity_not_ticker_and_never_bridges():
    """Only base CNY / quote RUB is supported; inverse rates are not inverted."""
    inverse = pair(uid='rub-cny', base_currency='rub', quote_currency='cny')
    bridge = pair(uid='CNYRUB_TOM', quote_currency='usd')
    correct = pair(uid='opaque-uid', base_currency='CNY', quote_currency='RUB')
    assert select_direct_pair([inverse, bridge, correct], 'CNY') == correct
    with pytest.raises(ValueError, match='route'):
        select_direct_pair([inverse, bridge], 'cny')


@pytest.mark.parametrize('reverse', [False, True])
def test_ambiguous_settlement_requires_explicit_uid(reverse):
    """TOD/TOM catalog order or current permission cannot silently choose a route."""
    candidates = [pair(uid='tod', api_trade_available=False), pair()]
    if reverse:
        candidates.reverse()
    with pytest.raises(ValueError, match='ambiguous'):
        select_direct_pair(candidates, 'cny')
    assert select_direct_pair(candidates, 'cny', pair_uid='cny-tom') == pair()
    with pytest.raises(ValueError, match='route'):
        select_direct_pair(candidates, 'cny', pair_uid='absent')


def test_duplicate_uid_is_rejected_even_with_explicit_choice():
    """Conflicting catalog facts do not become an unambiguous route."""
    with pytest.raises(ValueError, match='duplicate'):
        select_direct_pair([pair(), pair(lot_size=1)], 'cny', pair_uid='cny-tom')


@pytest.mark.parametrize('changes', [
    {'uid': ''}, {'uid': 'bad uid'}, {'base_currency': None}, {'quote_currency': ''},
    {'nominal': D(0)}, {'nominal': D(-1)}, {'nominal': D('NaN')}, {'nominal': 1},
    {'lot_size': 0}, {'lot_size': True}, {'lot_size': D(1000)},
    {'api_trade_available': None}, {'buy_available': 1}, {'sell_available': 'yes'},
])
def test_pair_rejects_incomplete_metadata(changes):
    """No guessed currency, nominal, lot or permission defaults."""
    with pytest.raises(ValueError):
        pair(**changes)


@pytest.mark.parametrize('changes', [
    {'bid': D(0)}, {'ask': D(-1)}, {'bid': D('NaN')}, {'ask': 121},
    {'bid': D(122)}, {'market_time': None}, {'received_time': None},
    {'market_time': NOW.replace(tzinfo=None)},
    {'received_time': NOW.replace(tzinfo=None)},
    {'market_time': NOW + timedelta(microseconds=1)},
    {'pair': pair(quote_currency='usd')},
])
def test_quote_rejects_invalid_book_or_timestamp(changes):
    """Only a real direct-pair book with timezone-aware market time is usable."""
    with pytest.raises(ValueError):
        quote(**changes)


@pytest.mark.parametrize('age', [timedelta(seconds=60), timedelta(seconds=60, microseconds=1)])
def test_freshness_uses_market_time_and_exact_inclusive_boundary(age):
    """Receiving stale data now does not make it fresh."""
    book = quote(market_time=NOW - age)
    if age == timedelta(seconds=60):
        assert FxSnapshot({'cny': book}, NOW).rate('cny') == D(12)
    else:
        with pytest.raises(ValueError, match='stale'):
            FxSnapshot({'cny': book}, NOW)


def test_snapshot_rechecks_age_without_mutating_original():
    """Execution can detect expiry and request a new snapshot, never reuse stale marks."""
    snapshot = FxSnapshot({'cny': quote()}, NOW)
    snapshot.validate_fresh(NOW + timedelta(seconds=58))
    with pytest.raises(ValueError, match='stale'):
        snapshot.validate_fresh(NOW + timedelta(seconds=59))
    assert snapshot.rate('cny') == D(12)


@pytest.mark.parametrize('mapping', [
    {'usd': quote()}, {'rub': quote()}, {'cny': quote(), 'CNY': quote()},
])
def test_snapshot_rejects_conflicting_currency_identity(mapping):
    """A caller cannot relabel a CNY rate as USD or RUB."""
    with pytest.raises(ValueError):
        FxSnapshot(mapping, NOW)


@pytest.mark.parametrize('now', [None, NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)])
def test_snapshot_rejects_unknown_clock_or_future_receipt(now):
    """Clock ordering is explicit rather than inferred from a timestamp string."""
    with pytest.raises(ValueError):
        FxSnapshot({'cny': quote()}, now)


def test_missing_currency_never_defaults_to_one():
    """Only RUB has an implicit identity conversion."""
    snapshot = FxSnapshot({}, NOW)
    with pytest.raises(ValueError, match='rate'):
        snapshot.rate('cny')
    with pytest.raises(ValueError, match='rub'):
        snapshot.from_rub(MoneyAmount('usd', D(12)), 'rub')


def test_builder_checks_quote_identity_and_revalidates_before_return():
    """A provider cannot answer a selected route with a different UID or units."""
    data = create_autospec(FxData, instance=True, spec_set=True)
    data.list_pairs.return_value = [pair()]
    data.get_quote.return_value = quote(pair=pair(uid='other'))
    with pytest.raises(ValueError, match='identity'):
        build_fx_snapshot(data, ['cny'], now=NOW)
    data.get_quote.return_value = quote(market_time=NOW - timedelta(seconds=61))
    with pytest.raises(ValueError, match='stale'):
        build_fx_snapshot(data, ['cny'], now=NOW)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_valuation_and_route_survive_closed_trading_but_permission_does_not(side):
    """Read-only marks do not depend on an open market or an account's caps."""
    closed = pair(api_trade_available=False)
    assert select_direct_pair([closed], 'cny') == closed
    book = quote(pair=closed)
    assert FxSnapshot({'cny': book}, NOW).rate('cny') == D(12)
    assert not book.can_trade(side, 1, rules(), state({'rub': D(50000), 'cny': D(2000)}),
                              now=NOW)


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_trade_requires_fresh_broker_facts_and_direction_permission(side):
    """TOM has no calendar wait, and a valuation grants no spending rights."""
    cash = {'rub': D(24200), 'cny': D(2000)}
    book = quote()
    assert book.can_trade(side, 2, rules(), state(cash), now=NOW)
    assert not book.can_trade(side, 2, rules(), state(cash, ready=False), now=NOW)
    assert not book.can_trade(side, 2, rules(), state({}), now=NOW)
    assert not book.can_trade(side, 2, rules(api_trade_available=False), state(cash), now=NOW)
    assert not book.can_trade(side, 2, rules(bestprice_order_available=False), state(cash), now=NOW)
    cap = {'buy_max_lots': 1} if side == 'BUY' else {'sell_max_lots': 1}
    assert not book.can_trade(side, 2, rules(**cap), state(cash), now=NOW)
    permission = {'buy_available': False} if side == 'BUY' else {'sell_available': False}
    assert not quote(pair=pair(**permission)).can_trade(
        side, 2, rules(), state(cash), now=NOW)
    cash['rub' if side == 'BUY' else 'cny'] -= D('0.000000001')
    assert not book.can_trade(side, 2, rules(), state(cash), now=NOW)
    with pytest.raises(ValueError, match='stale'):
        book.can_trade(side, 1, rules(), state(cash), now=NOW + timedelta(seconds=61))


def test_buy_own_money_limit_is_not_reserve_or_margin():
    """No floor, inferred future limits or arbitrary buffer is introduced."""
    assert not quote().can_trade('BUY', 2, rules(buy_money_amount=D(24199)),
                                 state({'rub': D(999999)}), now=NOW)
    with pytest.raises(ValueError, match='side'):
        quote().can_trade('OTHER', 1, rules(), state({}), now=NOW)
