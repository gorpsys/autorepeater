"""RUB marks retain broker facts and use native cash identity and one FX snapshot."""
from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.cash_facts import MoneyAmount, SecurityQuote, SecurityQuoteUnit
from autorepeater.fx import FxSnapshot
from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.strategy_data import (
    InstrumentType, PortfolioEntry, PortfolioSnapshot, StrategyData,
)
from autorepeater.valuation import SecurityData, Valuation

D = Decimal
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def valuation(rate='12', *, clock=None):
    """Only the own read port is mocked, with a fixed market observation."""
    pair = FxPair('cny-fx', 'cny', 'rub', D(1), 1000, True, True, True)
    fx = FxSnapshot({'cny': FxQuote(pair, D(rate), D(rate), NOW, NOW)}, NOW)
    data = create_autospec(SecurityData, instance=True, spec_set=True)
    data.get_security_quote.side_effect = lambda uid: SecurityQuote(
        'cny' if uid == 'foreign' else 'rub', D(99), D(101),
        SecurityQuoteUnit.MONEY_PER_PIECE)
    data.get_cash_currency.return_value = 'cny'
    return Valuation(fx, data, clock=clock if clock is not None else lambda: NOW), data


def position(uid, currency='rub', price='100', quantity='1', kind=InstrumentType.SHARE):
    """Original broker price has its own currency and is never overwritten."""
    return PortfolioEntry(uid, kind, currency, D(price), D(quantity), '', False, D(0))


def test_mixed_account_preserves_originals_and_excludes_source_cash():
    """100 RUB and 100 CNY give 100 and 1200 RUB, preserving a leading-zero ID."""
    value, books = valuation()
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    original = (position('rub'), position('foreign', 'rub', '900'),
                position('cash', price='10', quantity='100', kind=InstrumentType.CURRENCY))
    data.get_portfolio.return_value = PortfolioSnapshot(original)
    config = AccountConfig('source', '00123', D('0.1'), D('0.01'))
    strategy = AccountStrategy(PreparedAccountSource('00123', config))
    snapshot = strategy.load_snapshot(value.strategy_data(data))
    entries, total = snapshot
    assert total == D(1300)
    assert entries['foreign'].current_price == D(900)
    assert entries['foreign'].currency == 'rub'
    assert entries['foreign'].settlement_currency == 'cny'
    assert entries['foreign'].native_price == D(100)
    assert entries['foreign'].rub_price == D(1200)
    assert strategy.build_target(snapshot, D(1300)).quantities == {
        'rub': D('0.9'), 'foreign': D('0.9')}
    assert strategy.allocation_profile(snapshot).prices == {'rub': D(100), 'foreign': D(1200)}
    data.get_portfolio.assert_called_once_with('00123')
    books.get_cash_currency.assert_called_once_with('cash')
    assert original[1].rub_price is None


def test_cash_uses_units_times_fx_and_never_broker_rub_mark():
    """100 CNY reported as 1000 RUB is 1200 RUB at our midpoint of 12."""
    value, books = valuation()
    cash = position('cash', price='10', quantity='100', kind=InstrumentType.CURRENCY)
    result = value.portfolio(PortfolioSnapshot((cash,)))
    assert result.positions[0].rub_price == D(12)
    assert result.positions[0].native_cash == MoneyAmount('cny', D(100))
    assert result.positions[0].current_price == D(10)
    books.get_security_quote.assert_not_called()


@pytest.mark.parametrize('currency', ['', None, 'unknown'])
def test_cash_identity_cannot_be_inferred_from_broker_price(currency):
    """Even a RUB portfolio price cannot supply native currency identity."""
    value, books = valuation()
    books.get_cash_currency.return_value = currency
    with pytest.raises(ValueError, match='currency'):
        value.portfolio(PortfolioSnapshot((position('cash', kind=InstrumentType.CURRENCY),)))


def test_missing_fx_rejects_even_rub_valued_cash():
    """A broker RUB mark does not bypass a missing native market observation."""
    value, books = valuation()
    books.get_cash_currency.return_value = 'usd'
    with pytest.raises(ValueError, match='missing FX'):
        value.portfolio(PortfolioSnapshot((position('cash', kind=InstrumentType.CURRENCY),)))


def test_bond_percent_and_aci_are_converted_once():
    """Book midpoint is 90% of 1000 CNY plus 5 CNY ACI, then times FX 12."""
    value, books = valuation()
    books.get_security_quote.return_value = SecurityQuote(
        'cny', D(89), D(91), SecurityQuoteUnit.PERCENT_NOMINAL,
        MoneyAmount('cny', D(1000)), MoneyAmount('cny', D(5)))
    books.get_security_quote.side_effect = None
    result = value.portfolio(PortfolioSnapshot((position('bond', 'rub', '500'),)))
    assert result.positions[0].native_price == D(905)
    assert result.positions[0].rub_price == D(10860)
    assert result.positions[0].current_price == D(500)


def test_reused_uid_reads_one_book_per_context():
    """Children share a stable security book independently of broker portfolio marks."""
    value, books = valuation()
    first = value.portfolio(PortfolioSnapshot((position('foreign'),)))
    second = value.portfolio(PortfolioSnapshot((position('foreign', price='999'),)))
    assert first.positions[0].rub_price == second.positions[0].rub_price == D(1200)
    books.get_security_quote.assert_called_once_with('foreign')


@pytest.mark.parametrize('currency', ['', None, 'unknown'])
def test_invalid_original_valuation_currency_rejected(currency):
    """Broker currency remains an explicit validated fact even with a native book."""
    value, _ = valuation()
    with pytest.raises(ValueError, match='currency'):
        value.portfolio(PortfolioSnapshot((position('foreign', currency),)))


@pytest.mark.parametrize('quantity', [None, True, D('NaN')])
def test_unknown_native_cash_quantity_rejected(quantity):
    """A RUB broker valuation cannot reconstruct unknown native units."""
    value, _ = valuation()
    cash = replace(position('cash', kind=InstrumentType.CURRENCY), quantity=quantity)
    with pytest.raises(ValueError, match='finite Decimal'):
        value.portfolio(PortfolioSnapshot((cash,)))


def test_fully_foreign_source_uses_rub_marks_without_cash_targets():
    """An entirely CNY security source has a valid RUB-normalized profile."""
    value, _ = valuation()
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.get_portfolio.return_value = PortfolioSnapshot((position('foreign', 'cny'),))
    config = AccountConfig('source', '00123', D('0.1'), D('0.01'))
    source = AccountStrategy(PreparedAccountSource('00123', config))
    snapshot = source.load_snapshot(value.strategy_data(data))
    assert snapshot[1] == D(1200)
    assert source.allocation_profile(snapshot).exposures == {'foreign': D('0.9')}
    assert source.build_target(snapshot, D(2400)).quantities == {'foreign': D('1.8')}


def test_foreign_broker_valuation_of_rub_settlement_does_not_require_foreign_fx():
    """A security's own RUB book needs no conversion of its unrelated broker mark."""
    value, books = valuation()
    value = Valuation(FxSnapshot({}, NOW), books, clock=lambda: NOW)
    entry = value.portfolio(PortfolioSnapshot((position('rub', 'usd', '2'),))).positions[0]
    assert entry.current_price == D(2)
    assert entry.currency == 'usd'
    assert entry.settlement_currency == 'rub'
    assert entry.native_price == entry.rub_price == D(100)
