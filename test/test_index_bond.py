"""The requested mixed sandbox INDEX must resolve real bond metadata, not fake shares."""
from decimal import Decimal as D
from unittest.mock import create_autospec
from test.test_index_currency import document

from autorepeater.index_strategy import IndexStrategy
from autorepeater.index_strategy import _resolve_index_instrument
from autorepeater.strategy_data import InstrumentInfo, InstrumentMatch, PriceQuote, StrategyData
from autorepeater.tinvest_strategy_data import _instrument_type
from autorepeater.index_config import validate_index_config


def test_mixed_index_resolves_actual_bond_category():
    """Bond clean percent is already converted by valuation before INDEX receives price."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    bond_type = _instrument_type('bond')
    assert bond_type.value == 'bond'
    data.find_instruments.side_effect = lambda ticker: [
        InstrumentMatch(ticker, ticker, ticker, _instrument_type(
            'bond' if ticker == 'CNY' else 'share'), 'T')]
    data.get_instrument.side_effect = lambda uid: InstrumentInfo(
        uid, uid, uid, _instrument_type('bond' if uid == 'CNY' else 'share'), 'T', 1,
        'cny' if uid == 'CNY' else 'rub', True)
    data.get_last_prices.return_value = [PriceQuote('RUB', D(100), None),
                                         PriceQuote('CNY', D(1200), None)]
    strategy = IndexStrategy(validate_index_config(document()))
    snapshot = strategy.load_snapshot(data)
    assert snapshot['CNY'].uid == 'CNY' and snapshot['CNY'].currency == 'cny'
    assert set(strategy.build_target(snapshot, D(26000)).quantities) == {'RUB', 'CNY'}
    data.get_last_prices.assert_called_once_with(['RUB', 'CNY'])


def test_bond_does_not_change_existing_share_etf_ticker_resolution():
    """An ambiguous cross-type ticker cannot silently turn an old share index into bonds."""
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.return_value = [
        InstrumentMatch('bond', 'SAME', 'bond', _instrument_type('bond'), 'B'),
        InstrumentMatch('share', 'SAME', 'share', _instrument_type('share'), 'S')]
    data.get_instrument.side_effect = lambda uid: InstrumentInfo(
        uid, 'SAME', uid, _instrument_type(uid), 'S' if uid == 'share' else 'B', 1, 'rub', True)
    assert _resolve_index_instrument(data, 'SAME', {}).uid == 'share'
    data.get_instrument.assert_called_once_with('share')
