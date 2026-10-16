"""Explicit SDK metadata fields at the FX boundary; no SDK reprs or transport text."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import json
import logging

from autorepeater.market_diagnostics import emit_market_fact, safe_symbol
from autorepeater.cash_facts import MoneyAmount

_REQUESTED = ContextVar('fx_diagnostic_requested_uid', default=None)

# These are exact internal guard messages, not classifications of arbitrary SDK errors.
_PAIR_GUARDS = {
    'FX nominal: expected a positive finite Decimal': ('nominal', 'positive_finite_decimal'),
    'invalid FX pair UID': ('uid', 'nonempty_uid_without_whitespace'),
    'invalid FX pair lot': ('lot', 'positive_integer_not_bool'),
    'invalid monetary currency': ('currency', 'three_ascii_letters'),
    'FX metadata permissions must be bool': ('permissions', 'boolean_permissions'),
}


def scalar_fact(value: object) -> dict[str, str]:
    """Keep numeric source types/values, without invoking arbitrary objects' str/repr."""
    known = (type(None), bool, int, float, str, Decimal)
    if type(value) not in known:
        return {'type': 'unsupported', 'value': 'invalid'}
    text = safe_symbol(value, limit=128) if isinstance(value, str) else (
        format(value, 'f') if isinstance(value, Decimal) else str(value))
    return {'type': type(value).__name__, 'value': text}


def amount_fact(value: object, *, currency: object = None) -> dict[str, object]:
    """Select currency and raw components even when the SDK value is malformed."""
    if value is None:
        return scalar_fact(value)
    normalized = 'unavailable'
    try:
        normalized = scalar_fact(MoneyAmount.from_units_nano(
            getattr(value, 'currency', currency), getattr(value, 'units', None),
            getattr(value, 'nano', None)).amount)
    except ValueError:
        pass
    return {'type': ('MoneyValue' if type(value).__name__ == 'MoneyValue'
                     else 'Quotation' if type(value).__name__ == 'Quotation' else 'unsupported'),
            'currency': scalar_fact(getattr(value, 'currency', None)),
            'units': scalar_fact(getattr(value, 'units', None)),
            'nano': scalar_fact(getattr(value, 'nano', None)), 'normalized': normalized}


def sdk_metadata_facts(item: object) -> dict[str, object]:
    """Ordinary instrument metadata is public; unexpected fields are never traversed."""
    symbols = ('uid', 'ticker', 'class_code', 'currency', 'iso_currency_name',
               'instrument_type', 'country_of_risk')
    facts = {field: _metadata_symbol(getattr(item, field, None)) for field in symbols}
    facts['quotation_scale'] = '0.000000001'
    for field in ('lot', 'api_trade_available_flag', 'buy_available_flag', 'sell_available_flag'):
        facts[field] = scalar_fact(getattr(item, field, None))
    for field in ('nominal', 'min_price_increment'):
        facts[field] = amount_fact(
            getattr(item, field, None), currency=getattr(item, 'currency', None))
    return facts


def _metadata_symbol(value: object) -> str:
    # SDK dataclass defaults use bare object sentinels before a response is populated.
    return 'missing' if type(value) in (type(None), object) else safe_symbol(value)


@contextmanager
def requested_pair(uid: str):
    """Scope correlation to this setup only, including a different rejected FX pair."""
    token = _REQUESTED.set(safe_symbol(uid))
    try:
        yield
    finally:
        _REQUESTED.reset(token)


def selected_pair(item: object, normalized: object) -> None:
    """No successful catalog stream: emit only the one explicitly requested setup UID."""
    if _REQUESTED.get() == getattr(item, 'uid', None):
        emit_market_fact({'event': 'fx_setup_pair', 'requested_uid': _REQUESTED.get(),
                          'metadata': sdk_metadata_facts(item),
                          'normalized_nominal': scalar_fact(normalized)}, level=logging.INFO)


def metadata_failure(item: object, error: ValueError, field: str, constraint: str,
                     *, normalized: object = 'unavailable') -> None:
    """Keep the same exception, adding a safe note from the exact failing operation."""
    source = (amount_fact(getattr(item, field, None)) if field == 'nominal'
              else scalar_fact(getattr(item, field, None)))
    if field == 'permissions':
        source = {name: scalar_fact(getattr(item, name, None)) for name in (
            'api_trade_available_flag', 'buy_available_flag', 'sell_available_flag')}
    elif field == 'nominal.currency':
        source = scalar_fact(getattr(getattr(item, 'nominal', None), 'currency', None))
    fact = emit_market_fact({
        'event': 'fx_metadata_rejected', 'operation': 'fx.list_pairs', 'field': field,
        'constraint': constraint, 'metadata': sdk_metadata_facts(item), 'source': source,
        'normalized': ('unavailable' if isinstance(normalized, str) and normalized == 'unavailable'
                       else scalar_fact(normalized)), 'requested_uid': _REQUESTED.get(),
    })
    error.add_note('FX diagnostic: ' + json.dumps(fact))


def pair_failure(item: object, normalized: object, error: ValueError) -> None:
    """A known pure FxPair guard supplies the field; unknown errors are never stringified."""
    message = error.args[0] if error.args and isinstance(error.args[0], str) else None
    field, constraint = _PAIR_GUARDS.get(message, ('pair', 'FxPair_validation'))
    metadata_failure(item, error, field, constraint,
                     normalized=normalized if field == 'nominal' else 'unavailable')
