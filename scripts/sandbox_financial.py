"""Scoped observation of existing SDK calls on this live fixture's owned accounts."""
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
from typing import Any
from unittest.mock import patch

from t_tech.invest import OrderDirection, OrderExecutionReportStatus, services

from autorepeater.fx_diagnostics import amount_fact, scalar_fact
from autorepeater.market_diagnostics import safe_symbol
from scripts.sandbox_evidence import evidence_json
from scripts.sandbox_support import SandboxSession


def _response_facts(method: str, response: object) -> dict[str, object]:
    if method == 'get_positions':
        return {key: [amount_fact(value) for value in values]
                if type(values := getattr(response, key, None)) in (list, tuple)
                else scalar_fact(values) for key in ('money', 'blocked')}
    facts: dict[str, object] = {
        key: safe_symbol(getattr(response, field, None))
        for key, field in (('order_id', 'order_id'), ('uid', 'instrument_uid'))}
    for key, field, expected in (
            ('direction', 'direction', OrderDirection),
            ('status', 'execution_report_status', OrderExecutionReportStatus)):
        value = getattr(response, field, None)
        facts[key] = safe_symbol(value.name) if isinstance(value, expected) else scalar_fact(value)
    for key, field in (('requested', 'lots_requested'), ('executed', 'lots_executed')):
        value = getattr(response, field, None)
        facts[key] = (value if isinstance(value, int) and not isinstance(value, bool)
                      else scalar_fact(value))
    facts.update({key: amount_fact(getattr(response, key, None)) for key in (
        'total_order_amount', 'executed_commission', 'executed_order_price')})
    return facts


def _observe(session: SandboxSession, method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def observed(client: object, **kwargs: Any) -> Any:
        account_id = kwargs.get('account_id')
        if account_id not in session.lifecycle.created:
            return method(client, **kwargs)
        record = {
            'event': 'financial_rpc', 'method': method.__name__,
            'scenario': safe_symbol(session.scenario, limit=240),
            'account_id': account_id, 'started_utc': datetime.now(timezone.utc).isoformat(),
            'monotonic_started': session.monotonic(), 'outcome': 'error',
        }
        try:
            response = method(client, **kwargs)
            record['outcome'] = 'returned'
            try:
                facts = _response_facts(method.__name__, response)
                json.dumps(facts, default=evidence_json, allow_nan=False)
            except (AttributeError, TypeError, ValueError):
                record['response_observation'] = 'invalid'
            else:
                record.update(facts)
            if method.__name__ != 'get_positions':
                record['request_order_id'] = safe_symbol(kwargs.get('order_id', 'missing'))
            return response
        finally:
            finished = session.monotonic()
            record.update({'finished_utc': datetime.now(timezone.utc).isoformat(),
                           'monotonic_finished': finished,
                           'elapsed_seconds': finished - record['monotonic_started']})
            encoded = json.dumps(record, default=evidence_json, allow_nan=False)
            session.records.append(json.loads(encoded))
            print('Sandbox financial: ' + encoded, flush=True)
    return observed


@contextmanager
def observe_finances(session: SandboxSession) -> Iterator[None]:
    """Cover setup and separate Runner clients, restoring methods even after failures."""
    with ExitStack() as stack:
        for service, names in (
                (services.OrdersService, ('post_order', 'get_order_state')),
                (services.OperationsService, ('get_positions',))):
            target = inspect.unwrap(service)
            for name in names:
                observed = _observe(session, getattr(target, name))
                stack.enter_context(patch.object(target, name, observed))
        yield
