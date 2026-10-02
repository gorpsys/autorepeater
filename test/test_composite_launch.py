# pylint: disable=redefined-outer-name,too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals
"""Real entrypoints and engine with saved composite trees and mocked execution."""
import json
import logging
import sys
from contextlib import nullcontext
from decimal import Decimal
from test.test_strategy_contract import EndOfTestStream
from test.test_strategy_contract import fixture_launch  # pylint: disable=unused-import
from unittest.mock import ANY, Mock, call, patch

import pytest
from t_tech import invest

import handler as cloud
import main as cli
from autorepeater import account_config, composite_config, index_config, orders, strategies
from autorepeater import runner as runner_module
from autorepeater.logging_config import LOGGER_NAME
from autorepeater.portfolio import TargetPortfolio
from autorepeater.strategy_contract import AlgorithmDefinition
from autorepeater.strategy_data import (
    DataAccessError, InstrumentInfo, InstrumentMatch, InstrumentType, MoneyBlocking,
    PositionEvent, PriceQuote,
)


@pytest.fixture(name='catalog')
def fixture_catalog(tmp_path, monkeypatch):
    """Actual JSON selectors prepare a tree with repeated and nested algorithms."""
    composites = tmp_path / 'composite'
    indexes = tmp_path / 'index'
    composites.mkdir()
    indexes.mkdir()
    monkeypatch.setenv('COMPOSITE_CONFIG_DIR', str(composites))
    monkeypatch.setenv('INDEX_CONFIG_DIR', str(indexes))
    monkeypatch.setenv('ACCOUNT_CONFIG_PATH', str(tmp_path / 'account.json'))
    (tmp_path / 'account.json').write_text('{"reserve":"0.01"}', encoding='utf-8')
    (indexes / 'one.json').write_text(json.dumps({
        'name': 'ONE', 'reserve': '0.03', 'max_lot_weight_error': '0.05',
        'instruments': [{
            'ticker': 'ONE', 'effective_quantity': '1', 'free_float': '1',
            'weight_limit': '1', 'reference_price': '2', 'reference_weight': '100',
            'reference_index_capitalization': '12'}]}), encoding='utf-8')

    def write(name, components):
        path = composites / (name.lower() + '.json')
        path.write_text(json.dumps({'name': name, 'components': [
            {'algoritm': algorithm, 'src': src, 'weight': weight}
            for algorithm, src, weight in components]}), encoding='utf-8')
        return path

    write('BRANCH', [('ACCOUNT', '00123', '0.4'), ('INDEX', 'ONE', '0.2')])
    write('ROOT', [('COMPOSITE', 'BRANCH', '0.5'), ('INDEX', 'ONE', '0.25'),
                   ('INDEPENDENT', 'quote:other', '0.25')])
    return write


@pytest.fixture(name='execution')
def fixture_execution(launch, monkeypatch):
    """Gross destination 100, shared UID held above the combined target."""
    client, data, prepare, create, sdk, adapter = launch
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(instrument_uid=uid, instrument_type=kind,
                                 current_price=invest.MoneyValue(
                                     currency='RUB', units=price, nano=0),
                                 quantity=invest.Quotation(units=quantity, nano=0))
        for uid, kind, price, quantity in [('uid', 'share', 2, 40), ('old', 'share', 2, 5),
                                           ('cash', 'currency', 1, 10)]])
    client.instruments.get_instrument_by.side_effect = lambda **params: invest.InstrumentResponse(
        instrument=invest.Instrument(
            uid=params['id'], ticker='ONE', name='One', lot=1,
            trading_status=invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING))
    client.instruments.find_instrument.side_effect = lambda query: invest.FindInstrumentResponse(
        instruments=[invest.InstrumentShort(uid=query, ticker='ONE', name='One')])
    data.get_last_prices.side_effect = lambda uids: [PriceQuote(uid, Decimal('2'), None)
                                                    for uid in uids]
    for name in ('ALGORITM', 'SRC_ACCOUNT', 'DST_ACCOUNT', 'INVEST_TOKEN', 't_token'):
        monkeypatch.delenv(name, raising=False)
    return client, data, prepare, create, sdk, adapter


def expected_orders():
    """One diff sells before buying, with no child-specific opposing orders."""
    return [call(instrument_id=uid, quantity=quantity, direction=direction,
                 account_id='dst', order_type=invest.OrderType.ORDER_TYPE_BESTPRICE)
            for uid, quantity, direction in [
                ('uid', 15, invest.OrderDirection.ORDER_DIRECTION_SELL),
                ('old', 5, invest.OrderDirection.ORDER_DIRECTION_SELL),
                ('other', 12, invest.OrderDirection.ORDER_DIRECTION_BUY)]]


@pytest.mark.parametrize('entrypoint', ['run_sync', 'run', 'cli', 'query', 'environment'])
@pytest.mark.usefixtures('catalog')
def test_nested_tree_launches_through_actual_engine(execution, monkeypatch, entrypoint):
    """Entry paths share arithmetic and subscribe only in local mode."""
    client, data, prepare, create, sdk, adapter = execution
    monkeypatch.setenv('INVEST_TOKEN', 'test-token')
    monkeypatch.setenv('DST_ACCOUNT', 'dst')
    monkeypatch.setenv('ALGORITM', 'COMPOSITE')
    monkeypatch.setenv('SRC_ACCOUNT', 'ROOT')
    monkeypatch.setattr(sys, 'argv', [
        'main.py', '--algoritm', 'COMPOSITE', '-s', 'ROOT', '-d', 'dst'])
    streaming = entrypoint in ('cli', 'run')

    def invoke():
        if entrypoint == 'cli':
            return cli.main()
        if entrypoint in ('run', 'run_sync'):
            runner = runner_module.Runner(
                'test-token', strategies.prepare_strategy('COMPOSITE', 'ROOT'), 'dst')
            return getattr(runner, entrypoint)()
        query = {'algoritm': 'COMPOSITE', 'src': 'ROOT'} if entrypoint == 'query' else {}
        if entrypoint == 'query':
            monkeypatch.setenv('ALGORITM', 'UNKNOWN')
            monkeypatch.setenv('SRC_ACCOUNT', 'MISSING')
        return cloud.handler({'queryStringParameters': query}, None)

    with pytest.raises(EndOfTestStream) if streaming else nullcontext():
        result = invoke()
    if entrypoint in ('query', 'environment'):
        assert result['body'] == 'Success sync, ROOT dst!'
    count = 2 if streaming else 1
    assert client.orders.post_order.call_args_list == expected_orders() * count
    assert client.operations.get_portfolio.call_args_list == [call(account_id='dst')] * count
    assert data.get_portfolio.call_args_list == [call('00123')] * count
    assert data.get_last_prices.call_args_list == [
        call(['uid']), call(['uid']), call(['other'])] * count
    assert data.position_events.call_args_list == (
        [call(('00123', 'dst'))] * 2 if streaming else [])
    prepare.assert_called_once_with('quote:other', ANY)
    create.assert_called_once()
    sdk.assert_called_once_with(token='test-token', target=runner_module.INVEST_GRPC_API)
    sdk.return_value.__exit__.assert_called_once()
    adapter.assert_called_once_with(client)


@pytest.mark.parametrize('threshold, debug, submits', [(0.399, False, True), (0.4, False, False),
                                                       (0, True, False)])
@pytest.mark.usefixtures('catalog')
def test_unified_target_and_gross_threshold(execution, threshold, debug, submits):
    """Sell value 40 is compared strictly to gross 100, after UID targets are summed."""
    client, data, *_ = execution
    runner = runner_module.Runner('test-token', strategies.prepare_strategy('COMPOSITE', 'ROOT'),
                                 'dst', runner_module.RunnerParams(debug, threshold))
    snapshot = runner.strategy.load_snapshot(data)
    assert runner.strategy.build_target(snapshot, Decimal('100')) == TargetPortfolio(
        {'uid': Decimal('24.90'), 'other': Decimal('12.25')},
        {'uid': Decimal('2'), 'other': Decimal('2')})
    with patch('autorepeater.repeater.get_max_sum_positions_price',
               wraps=orders.get_max_sum_positions_price) as volume:
        runner.run_sync()
    assert volume.call_count == (0 if debug else 1)
    assert client.orders.post_order.call_args_list == (expected_orders() if submits else [])
    data.position_events.assert_not_called()


@pytest.mark.usefixtures('catalog')
def test_nontrading_shared_uid_keeps_goal_but_has_no_order(execution):
    """Trading status is enforced after composition by the existing executor."""
    client, *_ = execution
    lookup = client.instruments.get_instrument_by.side_effect

    def instrument(**params):
        response = lookup(**params)
        if params['id'] == 'uid':
            response.instrument.trading_status = (
                invest.SecurityTradingStatus.SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING)
        return response

    client.instruments.get_instrument_by.side_effect = instrument
    runner_module.Runner('test-token', strategies.prepare_strategy('COMPOSITE', 'ROOT'),
                         'dst').run_sync()
    assert client.orders.post_order.call_args_list == expected_orders()[1:]


@pytest.mark.parametrize('bad', ['source', 'algorithm', 'config', 'direct', 'indirect'])
@pytest.mark.parametrize('entrypoint', ['cli', 'cloud'])
def test_preparation_failure_precedes_credentials_and_factories(
        catalog, execution, monkeypatch, bad, entrypoint, caplog):  # pylint: disable=too-many-locals
    """A later selected BAD config remains fatal after GOOD issued a foreign warning."""
    client, _, _, _, sdk, _ = execution
    if bad == 'config':
        index_root = index_config._index_paths()[0].parent  # pylint: disable=protected-access
        document = json.loads((index_root / 'one.json').read_text(encoding='utf-8'))
        document.update(name='BAD', reserve='NaN')
        (index_root / 'bad.json').write_text(json.dumps(document), encoding='utf-8')
        refs = [('INDEX', 'ONE', '0.5'), ('INDEX', 'BAD', '0.5')]
        message = 'BAD.*reserve'
    elif bad == 'direct':
        refs, message = [('COMPOSITE', 'ROOT', '1')], 'COMPOSITE/ROOT -> COMPOSITE/ROOT'
    elif bad == 'indirect':
        catalog('BRANCH', [('COMPOSITE', 'ROOT', '1')])
        refs = [('COMPOSITE', 'BRANCH', '1')]
        message = 'COMPOSITE/ROOT -> COMPOSITE/BRANCH -> COMPOSITE/ROOT'
    else:
        refs = [('ACCOUNT', '00123', '0.5'),
                ('ACCOUNT' if bad == 'source' else 'MISSING', 'bad', '0.5')]
        message = 'unsupported src' if bad == 'source' else 'unsupported algoritm'
    catalog('ROOT', refs)
    factories = []
    for name, definition in list(strategies.ALGORITHMS.items()):
        factory = Mock(wraps=definition.create)
        factories.append(factory)
        monkeypatch.setitem(strategies.ALGORITHMS, name,
                            AlgorithmDefinition(definition.prepare_source, factory))
    monkeypatch.setattr(sys, 'argv', ['main.py', '--algoritm', 'COMPOSITE', '-s', 'ROOT'])
    with patch('autorepeater.serverless.get_param',
               side_effect=AssertionError('credentials read')), \
            patch.object(cli, 'os') as environment:
        with pytest.raises(ValueError, match=message):
            if entrypoint == 'cli':
                cli.main()
            else:
                cloud.handler({'queryStringParameters': {
                    'algoritm': 'COMPOSITE', 'src': 'ROOT'}}, None)
        assert environment.mock_calls == []
    if bad == 'config':
        assert any('bad.json' in message and 'reserve' in message for message in caplog.messages)
    for factory in factories:
        factory.assert_not_called()
    sdk.assert_not_called()
    assert client.mock_calls == []


def test_invalid_child_contract_before_client(catalog, execution):
    """A saved child factory returning a damaged contract cannot enter the SDK."""
    client, _, _, _, sdk, _ = execution
    catalog('ROOT', [('ACCOUNT', '00123', '0.5'), ('BROKEN', 'opaque', '0.5')])
    strategies.register_algorithm('BROKEN', AlgorithmDefinition(lambda src, context: src,
                                                               lambda prepared: object()))
    prepared = strategies.prepare_strategy('COMPOSITE', 'ROOT')
    with pytest.raises(TypeError, match='load_snapshot'):
        runner_module.Runner('test-token', prepared, 'dst').run_sync()
    sdk.assert_not_called()
    assert client.mock_calls == []


@pytest.mark.parametrize('failure', ['empty', 'index_empty', 'data', 'transport', 'target'])
@pytest.mark.usefixtures('catalog')
def test_partial_failure_never_submits_and_next_sync_succeeds(execution, failure, caplog):
    """Successful prior children do not expose a partial portfolio to execution."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    client, data, *_ = execution
    runner = runner_module.Runner(
        'test-token', strategies.prepare_strategy('COMPOSITE', 'ROOT'), 'dst')
    child = runner.strategy.children[-1]
    if failure == 'empty':
        override = patch.object(child, 'build_target',
                                return_value=TargetPortfolio({}, {}, 'no holdings'))
    elif failure == 'index_empty':
        override = patch.object(data, 'get_last_prices', side_effect=lambda uids: [
            PriceQuote(uid, Decimal('1000'), None) for uid in uids])
    elif failure == 'target':
        override = patch.object(child, 'build_target',
                                return_value=TargetPortfolio({'bad': Decimal('1')}, {}))
    else:
        error = ValueError('bad quote') if failure == 'data' else DataAccessError('unavailable')
        override = patch.object(child, 'load_snapshot', side_effect=error)
    with override:
        if failure in ('empty', 'index_empty'):
            runner.run_sync()
            reason = ('COMPOSITE/ROOT -> INDEPENDENT/quote:other: no holdings'
                      if failure == 'empty' else
                      'COMPOSITE/ROOT -> COMPOSITE/BRANCH -> INDEX/ONE: empty target')
            assert any(reason in message for message in caplog.messages)
        else:
            with pytest.raises(DataAccessError if failure == 'transport' else ValueError):
                runner.run_sync()
        client.orders.post_order.assert_not_called()
        client.instruments.get_instrument_by.assert_not_called()
    runner.run_sync()
    assert client.orders.post_order.call_args_list == expected_orders()
    data.position_events.assert_not_called()


@pytest.mark.parametrize('failure', ['end', 'open', 'read', 'recalc', 'execution'])
@pytest.mark.usefixtures('catalog')
def test_single_stream_recovery_uses_same_tree(execution, failure):
    """All children consume one stream; transport recovery performs a fresh whole sync."""
    client, data, prepare, create, *_ = execution
    ready = PositionEvent(True, 'dst', (), (MoneyBlocking(Decimal('0')),), 'ready')

    def failed_read():
        yield PositionEvent(False, '', (), (), 'ping')
        raise DataAccessError('read failed')

    first = {'end': iter(()), 'open': DataAccessError('open failed'), 'read': failed_read(),
             'recalc': iter((ready,)), 'execution': iter((ready,))}[failure]
    data.position_events.side_effect = [first, iter((ready,)), EndOfTestStream()]
    if failure == 'recalc':
        read = data.get_last_prices.side_effect
        count = 0

        def prices(uids):
            nonlocal count
            count += 1
            if count == 4:
                raise DataAccessError('recalculation failed')
            return read(uids)

        data.get_last_prices.side_effect = prices
    elif failure == 'execution':
        from grpc import StatusCode  # pylint: disable=import-outside-toplevel
        client.orders.post_order.side_effect = [invest.PostOrderResponse()] * 3 + [
            invest.RequestError(StatusCode.UNAVAILABLE, 'post failed', ())] + [
                invest.PostOrderResponse()] * 3
    runner = runner_module.Runner(
        'test-token', strategies.prepare_strategy('COMPOSITE', 'ROOT'), 'dst')
    with pytest.raises(EndOfTestStream):
        runner.run()
    assert data.position_events.call_args_list == [call(('00123', 'dst'))] * 3
    assert client.orders.post_order.call_args_list == (
        expected_orders() + expected_orders()[:1] + expected_orders()
        if failure == 'execution' else expected_orders() * 2)
    assert data.get_portfolio.call_args_list == [call('00123')] * (
        3 if failure in ('recalc', 'execution') else 2)
    prepare.assert_called_once()
    create.assert_called_once()


def test_prepared_tree_is_fixed_with_one_prepare_and_factory_per_occurrence(
        catalog, execution, monkeypatch):
    """Changing every selected document and registry cannot affect a saved launch."""
    _, data, *_ = execution
    catalog('ROOT', [('COMPOSITE', 'BRANCH', '0.5'), ('COMPOSITE', 'BRANCH', '0.5')])
    calls = {}
    for name in ('COMPOSITE', 'ACCOUNT', 'INDEX'):
        definition = strategies.ALGORITHMS[name]
        prepare, factory = Mock(wraps=definition.prepare_source), Mock(wraps=definition.create)
        calls[name] = prepare, factory
        monkeypatch.setitem(strategies.ALGORITHMS, name, AlgorithmDefinition(prepare, factory))
    with patch.object(composite_config, 'read_composite_document',
                      wraps=composite_config.read_composite_document) as composites, \
            patch.object(index_config, 'read_index_document',
                         wraps=index_config.read_index_document) as indexes, \
            patch('autorepeater.account_strategy.load_account_config',
                  wraps=account_config.load_account_config) as accounts:
        prepared = strategies.prepare_strategy('COMPOSITE', 'ROOT')
    assert composites.call_count == 6  # Two catalog files for each of three occurrences.
    assert indexes.call_count == 2
    assert accounts.call_count == 2
    for name, expected in [('COMPOSITE', ['ROOT', 'BRANCH', 'BRANCH']),
                           ('ACCOUNT', ['00123', '00123']), ('INDEX', ['ONE', 'ONE'])]:
        prepare, factory = calls[name]
        assert prepare.call_args_list == [call(src, ANY) for src in expected]
        factory.assert_not_called()
    catalog('ROOT', [('COMPOSITE', 'ROOT', '1')])
    catalog('BRANCH', [('MISSING', 'bad', '1')])
    index_path = index_config._index_paths()[0]  # pylint: disable=protected-access
    document = json.loads(index_path.read_text(encoding='utf-8'))
    document['reserve'] = '0.8'
    index_path.write_text(json.dumps(document), encoding='utf-8')
    account_path = index_path.parent.parent / 'account.json'
    account_path.write_text('{"reserve":"0.9"}', encoding='utf-8')
    monkeypatch.setattr(strategies, 'ALGORITHMS', {})
    with patch('builtins.open', side_effect=AssertionError('config reread')), \
            patch('pathlib.Path.open', side_effect=AssertionError('config reread')):
        runner = runner_module.Runner('test-token', prepared, 'dst')
        target = runner.strategy.build_target(runner.strategy.load_snapshot(data), Decimal('100'))
        assert target == (
            TargetPortfolio({'uid': Decimal('25.80')}, {'uid': Decimal('2')}))
        for _ in range(2):
            runner.run_sync()
        assert runner.strategy.event_accounts('dst') == ('00123', 'dst')
        assert runner.strategy.should_rebalance(
            PositionEvent(False, '', (), (), 'ping'), 'dst') is False
    for name, count in [('COMPOSITE', 3), ('ACCOUNT', 2), ('INDEX', 2)]:
        assert calls[name][0].call_count == calls[name][1].call_count == count
    assert runner.strategy.children[0] is not runner.strategy.children[1]
    assert runner.strategy.children[0].children[0] is not runner.strategy.children[1].children[0]
    data.position_events.assert_not_called()


@pytest.mark.parametrize('event', [None, {'queryStringParameters': {}},
                                   {'queryStringParameters': {'algoritm': 'COMPOSITE'}}])
def test_builtin_balanced_default_runs_actual_engine(execution, monkeypatch, event):
    """The root deployed handler uses built-in JSON and real target/order calculations."""
    client, data, _, _, sdk, _ = execution
    monkeypatch.setenv('INVEST_TOKEN', 'test-token')
    monkeypatch.setenv('DST_ACCOUNT', 'dst')
    client.operations.get_portfolio.return_value = invest.PortfolioResponse(positions=[
        invest.PortfolioPosition(
            instrument_uid='cash', instrument_type='currency',
            current_price=invest.MoneyValue(currency='RUB', units=1, nano=0),
            quantity=invest.Quotation(units=100000, nano=0))])

    def info(ticker):
        kind = InstrumentType.ETF if ticker in ('OBLG', 'GOLD') else InstrumentType.SHARE
        return InstrumentInfo('uid-' + ticker, ticker, ticker, kind,
                              'TQTF' if kind == InstrumentType.ETF else 'TQBR', 1, 'RUB', True)

    def find(query):
        instrument = info(query)
        return [InstrumentMatch(instrument.uid, query, query, instrument.instrument_type,
                                instrument.class_code)]

    data.find_instruments.side_effect = find
    data.get_instrument.side_effect = lambda uid: info(uid.removeprefix('uid-'))
    data.get_last_prices.side_effect = lambda uids: [PriceQuote(uid, Decimal('10'), None)
                                                    for uid in uids]
    result = cloud.handler(event, None)
    assert result['body'] == 'Success sync, BALANCED dst!'
    orders_sent = client.orders.post_order.call_args_list
    sent = {item.kwargs['instrument_id']: item.kwargs['quantity'] for item in orders_sent}
    assert sent['uid-OBLG'] == 2092
    assert sent['uid-GOLD'] == 1046
    assert len(sent) == len(orders_sent) and len(sent) > 2
    assert sum(sent.values()) * 10 <= Decimal('100000')
    assert all(item.kwargs['account_id'] == 'dst' and item.kwargs['direction'] == (
        invest.OrderDirection.ORDER_DIRECTION_BUY) for item in orders_sent)
    assert data.get_instrument.call_count == 46
    assert data.get_last_prices.call_count == 3
    data.get_portfolio.assert_not_called()
    data.position_events.assert_not_called()
    client.operations.get_portfolio.assert_called_once_with(account_id='dst')
    sdk.assert_called_once_with(token='test-token', target=runner_module.INVEST_GRPC_API)
