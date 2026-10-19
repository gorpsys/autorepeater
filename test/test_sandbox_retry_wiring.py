"""The v3 recorder classifies real PR40 validators without broker connections."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest
from t_tech.invest import schemas as sdk
from t_tech.invest.services import InstrumentsService, MarketDataService

from autorepeater.fx_data import FxPair, FxQuote
from autorepeater.tinvest_security_data import TInvestSecurityData
from scripts import sandbox_retry_evidence as evidence
from scripts.sandbox_retry_report import AttemptRecorder

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
NODE = 'e2e/test_sandbox.py::test_full_balanced_imoex_oblg_gold'


@pytest.fixture(name='security')
def fixture_security():
    """Strict SDK read ports return an exact UID and a two-sided native book."""
    client = SimpleNamespace(**{
        name: create_autospec(inspect.unwrap(service), instance=True, spec_set=True)
        for name, service in [('instruments', InstrumentsService),
                              ('market_data', MarketDataService)]
    })
    client.instruments.get_instrument_by.return_value = sdk.InstrumentResponse(
        instrument=sdk.Instrument(uid='uid', currency='cny', instrument_type='share'))
    level = sdk.Order(price=sdk.Quotation(units=10, nano=0), quantity=1)
    client.market_data.get_order_book.return_value = sdk.GetOrderBookResponse(
        instrument_uid='uid', orderbook_ts=NOW, bids=[level], asks=[level])
    return client, TInvestSecurityData(client, clock=lambda: NOW)


@pytest.mark.parametrize('protocol', ['retry-v2', 'retry-v3'])
@pytest.mark.parametrize('boundary', ['security', 'fx'])
@pytest.mark.parametrize('clock,eligible', [
    ('stale', True), ('just-stale', True), ('exact-limit', False),
    ('future', False), ('missing', False), ('naive', False),
])
def test_actual_validator_to_original_report(
        security, tmp_path, monkeypatch, protocol, boundary, clock, eligible):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Only proven stale rejections gain retry permission, and only under v3."""
    stamp = {
        'stale': NOW - timedelta(seconds=91),
        'just-stale': NOW - timedelta(seconds=90, microseconds=1),
        'exact-limit': NOW - timedelta(seconds=90),
        'future': NOW + timedelta(seconds=1),
        'missing': None,
        'naive': NOW.replace(tzinfo=None),
    }[clock]
    client, adapter = security
    client.market_data.get_order_book.return_value.orderbook_ts = stamp
    monkeypatch.setenv('E2E_EVIDENCE_PROTOCOL', protocol)
    monkeypatch.setenv('E2E_TESTED_SHA', 'a' * 40)
    recorder = AttemptRecorder(tmp_path / 'report.json')
    recorder.selected = (NODE,)
    recorder.entered = recorder.cleaned = [NODE]
    recorder.session_cleanup = True
    recorder.phase(NODE, 'setup', 'passed', None)
    try:
        if boundary == 'security':
            adapter.get_security_quote('uid')
        else:
            FxQuote(FxPair('uid', 'cny', 'rub', Decimal(1), 1000, True, True, True),
                    Decimal(10), Decimal(11), stamp, NOW).validate_fresh(NOW)
    except ValueError as error:
        recorder.phase(NODE, 'call', 'failed', error)
        status = 1
    else:
        recorder.phase(NODE, 'call', 'passed', None)
        status = 0
    recorder.phase(NODE, 'teardown', 'passed', None)
    recorder.finish(status)
    report = evidence.read_report(recorder.path, protocol=protocol)
    evidence.validate_report(report, status)
    assert status == (0 if clock == 'exact-limit' else 1)
    assert evidence.retry_nodes(report, protocol=protocol) == (
        (NODE,) if eligible and protocol == 'retry-v3' else ())
    fact = report.phases[1].stale_book
    assert (fact is not None) == (eligible and protocol == 'retry-v3')
    if fact is not None:
        assert fact == {
            'reason': 'market_time_stale', 'expected_uid': 'uid', 'received_uid': 'uid',
            'market_time': stamp.isoformat(), 'observed_utc': NOW.isoformat(),
            'freshness_limit_seconds': 90,
        }
    assert client.market_data.mock_calls == (
        [call.get_order_book(instrument_id='uid', depth=1)] if boundary == 'security' else [])
    assert client.instruments.mock_calls == ([call.get_instrument_by(
        id_type=sdk.InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id='uid')]
        if boundary == 'security' else [])
