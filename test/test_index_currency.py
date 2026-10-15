"""Historical units and current RUB values are independent inputs to INDEX."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
from test.test_account_currency import valuation

import pytest

from autorepeater.index_config import (
    load_index_config, reference_price_in_capitalization_currency, validate_index_config,
)
from autorepeater.index_strategy import IndexQuote, IndexStrategy, _capitalizations
from autorepeater.strategy_data import InstrumentInfo, InstrumentMatch, InstrumentType, StrategyData

D = Decimal


def document():
    """Different free float and cap coefficients are already included in capitalizations."""
    item = {'effective_quantity': '10', 'free_float': '0.5', 'weight_limit': '0.25',
            'reference_price': '100', 'reference_weight': '50',
            'reference_index_capitalization': '1000'}
    return {'name': 'MIXED', 'reserve': '0', 'max_lot_weight_error': '0.9',
            'allocation_drift_limits': [{'budget_from': '0', 'budget_to': None,
                                        'upper_inclusive': False, 'limit': '0'}],
            'instruments': [dict(item, ticker='RUB'), dict(item, ticker='CNY')]}


def test_omitted_reference_currency_always_means_rub():
    """Trading CNY does not require a historical rate for an implicitly RUB reference."""
    config = validate_index_config(document())
    assert config.reference_units.capitalization_currency == 'rub'
    assert not config.reference_units.price_currencies
    assert not config.reference_units.fx_rates


def test_mixed_index_historical_currency_changes_weights_and_targets():
    """10 RUB per historical CNY differs from today's 12 RUB midpoint."""
    payload = document()
    payload['instruments'][1].update(reference_price_currency='CNY', reference_fx_rate='10')
    config = validate_index_config(payload)
    quotes = {'RUB': IndexQuote('rub', D(100), 1),
              'CNY': IndexQuote('cny', D(1200), 1, 'cny')}
    assert _capitalizations(config, quotes) == {'RUB': D(1000), 'CNY': D(1200)}
    target = IndexStrategy(config).build_target(quotes, D(2200))
    assert target.quantities == {'cny': D(1), 'rub': D(10)}
    quotes['CNY'].price = D(2400)
    assert _capitalizations(config, quotes)['CNY'] == D(2400)


def test_cny_reference_price_in_common_usd_capitalizations():
    """Historical USD per CNY belongs in the denominator, never today's FX."""
    payload = document()
    payload['reference_capitalization_currency'] = 'USD'
    payload['instruments'][0]['reference_fx_rate'] = '0.01'
    payload['instruments'][1].update(reference_price_currency='CNY', reference_fx_rate='0.1')
    config = validate_index_config(payload)
    quotes = {'RUB': IndexQuote('rub', D(100), 1), 'CNY': IndexQuote('cny', D(1200), 1)}
    assert _capitalizations(config, quotes) == {'RUB': D(100000), 'CNY': D(120000)}


@pytest.mark.parametrize('value', [None, '', 'unknown', 'XXX'])
@pytest.mark.parametrize('common', [False, True])
def test_invalid_reference_currency(value, common):
    """Explicit empty/null/unknown units fail before data or orders."""
    payload = document()
    if common:
        payload['reference_capitalization_currency'] = value
    else:
        payload['instruments'][0]['reference_price_currency'] = value
    with pytest.raises(ValueError, match='currency'):
        validate_index_config(payload)


@pytest.mark.parametrize('rate', [None, '', '0', '-1', 'NaN', 'Infinity', 'bad', 1, True])
def test_explicit_foreign_reference_requires_positive_decimal_string_rate(rate):
    """No historical fallback for foreign reference units."""
    payload = document()
    payload['instruments'][1]['reference_price_currency'] = 'cny'
    if rate is not None:
        payload['instruments'][1]['reference_fx_rate'] = rate
    with pytest.raises(ValueError, match='reference_fx_rate'):
        validate_index_config(payload)


@pytest.mark.parametrize('currency', ['rub', 'cny'])
def test_equal_reference_currencies_require_unit_rate(currency):
    """An explicitly foreign same-currency pair still needs its historical field."""
    payload = document()
    payload['reference_capitalization_currency'] = currency
    for item in payload['instruments']:
        item.update(reference_price_currency=currency, reference_fx_rate='2')
    with pytest.raises(ValueError, match='reference_fx_rate'):
        validate_index_config(payload)


def test_same_foreign_reference_currency_still_requires_explicit_historical_rate():
    """Only RUB/RUB permits an omitted rate, even if both foreign units coincide."""
    payload = document()
    payload['reference_capitalization_currency'] = 'cny'
    for item in payload['instruments']:
        item['reference_price_currency'] = 'cny'
    with pytest.raises(ValueError, match='reference_fx_rate'):
        validate_index_config(payload)
    for item in payload['instruments']:
        item['reference_fx_rate'] = '1'
    config = validate_index_config(payload)
    assert config.reference_units.fx_rates == {'RUB': D(1), 'CNY': D(1)}


@pytest.mark.parametrize('currency', ['rub', 'cny'])
def test_null_reference_rate_is_not_an_omitted_rate(currency):
    """An explicit null must fail even where an omitted RUB/RUB rate would be one."""
    payload = document()
    payload['instruments'][1].update(reference_price_currency=currency, reference_fx_rate=None)
    with pytest.raises(ValueError, match='reference_fx_rate'):
        validate_index_config(payload)


def test_explicit_rub_preserves_legacy_decimal_arithmetic():
    """New unit fields do not alter the numerical RUB model or apply coefficients twice."""
    old = document()
    explicit = deepcopy(old)
    explicit['reference_capitalization_currency'] = 'RUB'
    for item in explicit['instruments']:
        item['reference_price_currency'] = 'RUB'
    quotes = {'RUB': IndexQuote('rub', D('100.000000001'), 1),
              'CNY': IndexQuote('cny', D(150), 1)}
    assert _capitalizations(validate_index_config(old), quotes) == _capitalizations(
        validate_index_config(explicit), quotes)


def test_original_reference_contract_without_new_fields_is_rub_rub():
    """Structural callers of the original numeric reference contract receive the same units."""
    reference = SimpleNamespace(reference_price=D('100.000000001'))
    assert reference_price_in_capitalization_currency(reference) == D('100.000000001')
    with pytest.raises(ValueError, match='reference_fx_rate'):
        reference_price_in_capitalization_currency(reference, 'usd')
    with pytest.raises(ValueError, match='reference_fx_rate'):
        reference_price_in_capitalization_currency(reference, 'cny', 'cny')


def test_rub_reference_denominator_keeps_all_digits_before_existing_arithmetic():
    """Multiplying a high precision RUB reference by implicit one must not round it first."""
    price = D('100.123456789012345678901234567890123456789')
    assert reference_price_in_capitalization_currency(
        SimpleNamespace(reference_price=price)) == price


def test_bundled_configs_add_only_currency_fields():
    """Reference numerical data are unchanged; every shipped price has explicit RUB units."""
    for name in ('imoex', 'gold', 'oblg', 'tmon'):
        path = Path(__file__).resolve().parents[1] / 'autorepeater/configs' / f'{name}.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert payload['reference_capitalization_currency'] == 'RUB'
        assert all(item['reference_price_currency'] == 'RUB' for item in payload['instruments'])
        assert load_index_config(path).reference_units.capitalization_currency == 'rub'


@pytest.mark.parametrize('rate, budget', [('12', '2200'), ('24', '3400')])
def test_index_load_uses_native_book_and_same_fx_not_last_trade(rate, budget):
    """RUB/CNY index valuation reaches the pure target with separate native prices."""
    value, books = valuation(rate)
    data = create_autospec(StrategyData, instance=True, spec_set=True)
    data.find_instruments.side_effect = lambda ticker: [InstrumentMatch(
        'foreign' if ticker == 'CNY' else 'rub', ticker, ticker, InstrumentType.SHARE, 'T')]
    data.get_instrument.side_effect = lambda uid: InstrumentInfo(
        uid, 'CNY' if uid == 'foreign' else 'RUB', uid, InstrumentType.SHARE, 'T',
        1, 'cny' if uid == 'foreign' else 'rub', True)
    payload = document()
    payload['instruments'][1].update(reference_price_currency='cny', reference_fx_rate='10')
    source = IndexStrategy(validate_index_config(payload))
    snapshot = source.load_snapshot(value.strategy_data(data))
    assert snapshot['CNY'].price == D(100) * D(rate)
    assert snapshot['CNY'].native_price == D(100)
    assert snapshot['CNY'].currency == 'cny'
    assert snapshot['RUB'].price == D(100)
    assert _capitalizations(source.config, snapshot) == {
        'RUB': D(1000), 'CNY': D(100) * D(rate)}
    assert source.config.reference_units.fx_rates == {'CNY': D(10)}
    assert source.build_target(snapshot, D(budget)).quantities == {'foreign': D(1), 'rub': D(10)}
    assert books.get_security_quote.call_count == 2
    data.get_last_prices.assert_not_called()
