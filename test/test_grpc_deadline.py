"""The real sync gRPC interceptor bounds unary calls before their first attempt."""
from collections import namedtuple
from contextvars import Context, copy_context
from unittest.mock import Mock
from types import SimpleNamespace

import grpc
import pytest

from autorepeater.grpc_deadline import UnaryDeadlineInterceptor
from autorepeater import grpc_deadline
from autorepeater import runner as runner_module
from autorepeater.strategy_contract import PreparedStrategy
from scripts import check_imoex_strategy as calibration

Details = namedtuple('Details', 'method timeout metadata credentials wait_for_ready compression')


@pytest.mark.parametrize('original, expected', [
    (None, 10), (30, 10), (2, 2), (0, 0), (float('inf'), 10),
])
def test_deadline_preserves_call_details_and_smaller_timeout(original, expected):
    """Continuation observes the deadline on its first and only invocation."""
    interceptor = UnaryDeadlineInterceptor(10)
    details = Details('/service/read', original, (('key', 'value'),), object(),
                      True, grpc.Compression.Gzip)
    continuation = Mock(return_value='response')
    assert interceptor.intercept_unary_unary(continuation, details, 'request') == 'response'
    forwarded = continuation.call_args.args[0]
    assert isinstance(forwarded, grpc.ClientCallDetails)
    assert forwarded.timeout == expected
    assert tuple(forwarded)[0:1] + tuple(forwarded)[2:] == tuple(details)[0:1] + tuple(details)[2:]
    continuation.assert_called_once_with(forwarded, 'request')


@pytest.mark.parametrize('timeout', [None, True, 0, -1, float('nan'), float('inf'), '10'])
def test_invalid_default_deadline(timeout):
    """The configured deadline is positive and finite before Client creation."""
    with pytest.raises(ValueError, match='timeout'):
        UnaryDeadlineInterceptor(timeout)


def test_streams_and_failures_are_not_retried():
    """Only unary-unary interception is installed; original failure escapes."""
    interceptor = UnaryDeadlineInterceptor()
    assert isinstance(interceptor, grpc.UnaryUnaryClientInterceptor)
    assert not isinstance(interceptor, grpc.UnaryStreamClientInterceptor)
    assert not hasattr(interceptor, 'intercept_unary_stream')
    continuation = Mock(side_effect=RuntimeError('failed'))
    with pytest.raises(RuntimeError, match='failed'):
        interceptor.intercept_unary_unary(
            continuation, Details('/s/write', None, None, None, None, None), None)
    continuation.assert_called_once()


@pytest.mark.parametrize('mode', ['run', 'run_sync'])
def test_runner_installs_sync_deadline_before_enter(monkeypatch, mode):
    """Both launch modes keep the old engine and supply the SDK interceptor."""
    constructor = Mock()
    constructor.return_value = Mock(__enter__=Mock(return_value=Mock()),
                                    __exit__=Mock(return_value=False))
    monkeypatch.setattr(runner_module, 'Client', constructor)
    monkeypatch.setattr(runner_module, 'print_all_portfolio', Mock())
    monkeypatch.setattr(runner_module, 'configure_local_logging', Mock())
    monkeypatch.setattr(runner_module, 'AutoRepeater', Mock())
    prepared = PreparedStrategy(lambda _: Mock(), None, 'source')
    runner = runner_module.Runner('synthetic', prepared, 'account')
    constructor.assert_not_called()
    getattr(runner, mode)()
    interceptor, = constructor.call_args.kwargs['interceptors']
    assert isinstance(interceptor, UnaryDeadlineInterceptor)
    assert interceptor.timeout == 10


@pytest.mark.parametrize('sandbox', [False, True])
def test_calibrator_installs_same_sync_deadline(monkeypatch, sandbox):
    """Explicit calibration only reads a synthetic snapshot."""
    constructor = Mock()
    constructor.return_value = Mock(__enter__=Mock(return_value=Mock()),
                                    __exit__=Mock(return_value=False))
    monkeypatch.setattr(calibration, 'Client', constructor)
    monkeypatch.setattr(calibration.os, 'environ', {'READ_ONLY_INVEST_TOKEN': 'synthetic'})
    args = SimpleNamespace(src='test', sandbox=sandbox, gross_values=[], budgets=[],
                           thresholds=[], output=None)
    monkeypatch.setattr(calibration, 'parse_args', Mock(return_value=(Mock(), args)))
    monkeypatch.setattr(calibration, 'select_index_config',
                        Mock(return_value=SimpleNamespace(reserve=0)))
    strategy = Mock()
    strategy.load_snapshot.return_value = {}
    monkeypatch.setattr(calibration, 'IndexStrategy', Mock(return_value=strategy))
    monkeypatch.setattr(calibration, 'asdict', Mock(return_value={}))
    monkeypatch.setattr(calibration.reporting, 'print_index_calibration', Mock())
    calibration.main([])
    interceptor, = constructor.call_args.kwargs['interceptors']
    assert isinstance(interceptor, UnaryDeadlineInterceptor)
    assert interceptor.timeout == 10


@pytest.mark.parametrize('stream_timeout', [None, 2])
def test_actual_grpc_channel_applies_timeout_and_leaves_stream_alone(stream_timeout):
    """Drive grpc.intercept_channel with an offline channel, including with_call."""
    channel = Mock(spec=grpc.Channel)
    channel.unary_unary.return_value.with_call.return_value = ('response', Mock(spec=grpc.Call))
    channel.unary_stream.return_value.return_value = iter(['event'])
    intercepted = grpc.intercept_channel(channel, UnaryDeadlineInterceptor())
    unary = intercepted.unary_unary('/service/read')
    assert unary.with_call('request')[0] == 'response'
    channel.unary_unary.return_value.with_call.assert_called_once()
    assert channel.unary_unary.return_value.with_call.call_args.kwargs['timeout'] == 10
    assert list(intercepted.unary_stream('/service/stream')(
        'request', timeout=stream_timeout)) == ['event']
    channel.unary_stream.return_value.assert_called_once_with('request', timeout=stream_timeout)


@pytest.mark.parametrize('timeout', [None, True, False, 0, -1, float('nan'),
                                     float('inf'), '30'])
def test_invalid_read_budget_does_not_install_scope(timeout):
    """Reject invalid durations before installing a deadline in this context."""
    with pytest.raises(ValueError, match='timeout'):
        with grpc_deadline.read_deadline(timeout):
            pytest.fail('invalid budget entered')
    assert grpc_deadline.remaining_read_timeout() is None


@pytest.mark.parametrize('default,explicit,remaining,expected', [
    (10, None, 30, 10), (10, 2, 4, 2), (10, 30, 4, 4),
    (3, None, 4, 3), (10, 0, 4, 0),
])
def test_scoped_timeout_uses_smallest_limit(monkeypatch, default, explicit, remaining, expected):
    """A scope only tightens existing default and explicit unary deadlines."""
    now = [100.0]
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: now[0], raising=False)
    details = Details('/service/read', explicit, (('key', 'value'),), object(),
                      True, grpc.Compression.Gzip)
    continuation = Mock(return_value='response')
    with grpc_deadline.read_deadline(30):
        now[0] += 30 - remaining
        assert UnaryDeadlineInterceptor(default).intercept_unary_unary(
            continuation, details, 'request') == 'response'
    forwarded = continuation.call_args.args[0]
    assert forwarded.timeout == expected
    assert tuple(forwarded)[0:1] + tuple(forwarded)[2:] == tuple(details)[0:1] + tuple(details)[2:]
    continuation.assert_called_once_with(forwarded, 'request')


def test_multiple_unary_reads_share_absolute_deadline(monkeypatch):
    """Later RPCs receive only the remainder; no new RPC starts at exactly 30s."""
    clock = iter([100, 100, 110, 129.5, 130])
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: next(clock), raising=False)
    continuation = Mock(return_value='response')
    interceptor = UnaryDeadlineInterceptor()
    details = Details('/service/read', None, None, None, None, None)
    with grpc_deadline.read_deadline(30):
        for _ in range(3):
            assert interceptor.intercept_unary_unary(continuation, details, None) == 'response'
        with pytest.raises(TimeoutError, match='^SDK read deadline expired$') as raised:
            interceptor.intercept_unary_unary(continuation, details, None)
    assert raised.value.__cause__ is None
    assert [item.args[0].timeout for item in continuation.call_args_list] == [10, 10, 0.5]
    assert grpc_deadline.remaining_read_timeout() is None


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_nested_deadlines_restore_parent_and_finally_clear_scope(monkeypatch, failure):
    """A longer child cannot extend the parent, even when unwinding an exception."""
    now = [100.0]
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: now[0], raising=False)
    with pytest.raises(failure):
        with grpc_deadline.read_deadline(30):
            now[0] = 105
            with grpc_deadline.read_deadline(100):
                assert grpc_deadline.remaining_read_timeout() == 25
            with pytest.raises(ValueError, match='inner failure'):
                with grpc_deadline.read_deadline(2):
                    now[0] = 106
                    assert grpc_deadline.remaining_read_timeout() == 1
                    raise ValueError('inner failure')
            assert grpc_deadline.remaining_read_timeout() == 24
            raise failure()
    assert grpc_deadline.remaining_read_timeout() is None
    with grpc_deadline.read_deadline(7):
        assert grpc_deadline.remaining_read_timeout() == 7
    assert grpc_deadline.remaining_read_timeout() is None


def test_contexts_keep_deadlines_isolated(monkeypatch):
    """A child context inherits its deadline but cannot change its parent's value."""
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: 100, raising=False)

    def child():
        assert grpc_deadline.remaining_read_timeout() == 30
        with grpc_deadline.read_deadline(2):
            assert grpc_deadline.remaining_read_timeout() == 2

    with grpc_deadline.read_deadline(30):
        copy_context().run(child)
        assert Context().run(grpc_deadline.remaining_read_timeout) is None
        assert grpc_deadline.remaining_read_timeout() == 30
    assert grpc_deadline.remaining_read_timeout() is None


def test_expired_parent_cannot_be_reopened_by_nested_scope(monkeypatch):
    """A child cannot restart an exhausted budget."""
    now = [100.0]
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: now[0], raising=False)
    continuation = Mock()
    with grpc_deadline.read_deadline(30):
        now[0] = 131
        with grpc_deadline.read_deadline(30):
            with pytest.raises(TimeoutError, match='^SDK read deadline expired$'):
                UnaryDeadlineInterceptor().intercept_unary_unary(
                    continuation, Details('/s/read', None, None, None, None, None), None)
    continuation.assert_not_called()


def test_actual_grpc_channel_honors_scope_without_intercepting_streams(monkeypatch):
    """An expired read scope stops unary transport but never bounds a stream."""
    now = [100.0]
    monkeypatch.setattr(grpc_deadline, 'monotonic', lambda: now[0], raising=False)
    channel = Mock(spec=grpc.Channel)
    channel.unary_unary.return_value.with_call.return_value = ('response', Mock(spec=grpc.Call))
    channel.unary_stream.return_value.return_value = iter(['event'])
    intercepted = grpc.intercept_channel(channel, UnaryDeadlineInterceptor())
    unary = intercepted.unary_unary('/service/read')
    with grpc_deadline.read_deadline(4):
        assert unary.with_call('request')[0] == 'response'
        assert channel.unary_unary.return_value.with_call.call_args.kwargs['timeout'] == 4
        now[0] = 104
        with pytest.raises(TimeoutError, match='^SDK read deadline expired$'):
            unary.with_call('never')
        assert list(intercepted.unary_stream('/service/stream')('request')) == ['event']
    channel.unary_unary.return_value.with_call.assert_called_once()
    channel.unary_stream.return_value.assert_called_once_with('request')
