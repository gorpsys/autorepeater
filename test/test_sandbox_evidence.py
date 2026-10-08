"""Typed sandbox evidence serializes without leaking arbitrary objects."""
import json
from dataclasses import asdict

import pytest
from t_tech.invest import MoneyValue

from scripts.sandbox_evidence import (
    AccountEvidence, IdentifierEvidence, MoneyEvidence,
    OperationEvidence, OrderEvidence, QuoteEvidence, TradeEvidence, evidence_json,
)


@pytest.mark.parametrize('evidence', [
    MoneyEvidence('rub', '2.000000001'),
    QuoteEvidence('uid', 10, '2.000000001', '2026-10-08T00:00:00+00:00'),
    OrderEvidence('broker', 'uid', 'BUY', 'FILL', 2, 2),
    TradeEvidence('trade', 20, '2026-10-08T00:00:00+00:00', MoneyEvidence('rub', '2')),
    OperationEvidence('operation', 'uid', 'figi', 'BUY', 'EXECUTED', 20, 0,
                      '2026-10-08T00:00:00+00:00', MoneyEvidence('rub', '40'),
                      MoneyEvidence('rub', '2'), ()),
    AccountEvidence('100', True, {'uid': '20'}, {'uid': '2'}, {'rub': '60'}, (), ()),
    IdentifierEvidence('account', 'name', 'account', 'account', 'account', 'account',
                       False, False),
], ids=['money', 'quote', 'order', 'trade', 'operation', 'account', 'identifier'])
def test_evidence_has_a_stable_explicit_json_schema(evidence):
    """Nested DTOs keep exact amounts and the existing diagnostic JSON shape."""
    assert evidence_json(evidence) == asdict(evidence)
    expected = json.loads(json.dumps(asdict(evidence)))
    assert json.loads(json.dumps(evidence, default=evidence_json)) == expected


@pytest.mark.parametrize('value', [MoneyValue(currency='private'), object(), MoneyEvidence],
                         ids=['sdk', 'object', 'type'])
def test_serializer_rejects_non_evidence_without_repr(value):
    """SDK DTOs and arbitrary objects must not gain a str/repr fallback."""
    with pytest.raises(TypeError, match='unsupported sandbox evidence type'):
        evidence_json(value)
