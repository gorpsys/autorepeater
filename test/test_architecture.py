"""Static import boundaries; these checks are not a Python execution sandbox."""
import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1] / 'autorepeater'
STRATEGIES = {'autorepeater.account_strategy', 'autorepeater.index_strategy'}
ASSEMBLY = {'autorepeater.strategies', 'autorepeater.runner',
            'autorepeater.repeater', 'autorepeater.tinvest_strategy_data'}
NEUTRAL = {'constants', 'money', 'portfolio', 'reporting', 'logging_config',
           'strategy_contract', 'strategy_data', 'triggers', 'index_config', 'account_config',
           'strategy_budget', 'composite_config'}


def forbidden_imports(source, module):
    """Return forbidden static imports as (line, absolute module) pairs."""
    name = module.rsplit('.', 1)[-1]
    forbidden = set()
    if module in STRATEGIES or name.endswith('_strategy'):
        forbidden = (STRATEGIES - {module}) | ASSEMBLY | {'autorepeater.orders'}
        forbidden |= {'t_tech', 'grpc'}
    elif name in NEUTRAL:
        forbidden = STRATEGIES | ASSEMBLY | {'autorepeater.orders', 't_tech', 'grpc'}
        if name == 'composite_config':
            forbidden |= {'autorepeater.index_config', 'autorepeater.account_config'}
    elif name == 'repeater':
        forbidden = STRATEGIES | {'autorepeater.strategies', 'autorepeater.runner',
                                  'autorepeater.tinvest_strategy_data',
                                  'autorepeater.index_config', 'autorepeater.account_config',
                                  'autorepeater.composite_config',
                                  'autorepeater.triggers'}

    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            dependencies = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                base = resolve_name('.' * node.level + base, module.rsplit('.', 1)[0])
            dependencies = ([f'{base}.{alias.name}' for alias in node.names]
                            if base == 'autorepeater' else [base])
        else:
            continue
        for dependency in dependencies:
            if any(dependency == banned or dependency.startswith(banned + '.')
                   for banned in forbidden):
                violations.append((node.lineno, dependency))
    return sorted(violations)


@pytest.mark.parametrize('module, source, dependency', [
    ('account_strategy', 'import t_tech.invest as sdk', 't_tech.invest'),
    ('index_strategy', 'from grpc import RpcError', 'grpc'),
    ('index_strategy', 'from t_tech.invest import InstrumentIdType', 't_tech.invest'),
    ('account_strategy', 'import autorepeater.index_strategy as other',
     'autorepeater.index_strategy'),
    ('index_strategy', 'from autorepeater import account_strategy as other',
     'autorepeater.account_strategy'),
    ('account_strategy', 'from .index_strategy import IndexStrategy',
     'autorepeater.index_strategy'),
    ('index_strategy', 'from . import account_strategy', 'autorepeater.account_strategy'),
    ('account_strategy', 'from autorepeater.runner import Runner', 'autorepeater.runner'),
    ('index_strategy', 'from .tinvest_strategy_data import TInvestStrategyData',
     'autorepeater.tinvest_strategy_data'),
    ('index_strategy', 'from .repeater import AutoRepeater', 'autorepeater.repeater'),
    ('account_strategy', 'from .strategies import ALGORITHMS', 'autorepeater.strategies'),
    ('account_strategy', 'from .orders import OrderParams', 'autorepeater.orders'),
    ('strategy_data', 'from .account_strategy import AccountStrategy',
     'autorepeater.account_strategy'),
    ('strategy_contract', 'import autorepeater.index_strategy', 'autorepeater.index_strategy'),
    ('portfolio', 'from . import runner', 'autorepeater.runner'),
    ('reporting', 'from t_tech.invest import Client', 't_tech.invest'),
    ('money', 'import grpc.aio', 'grpc.aio'),
    ('strategy_budget', 'from .account_strategy import AccountStrategy',
     'autorepeater.account_strategy'),
    ('strategy_budget', 'from .repeater import AutoRepeater', 'autorepeater.repeater'),
    ('strategy_budget', 'import t_tech.invest', 't_tech.invest'),
    ('account_config', 'from t_tech.invest import Client', 't_tech.invest'),
    ('account_config', 'from .account_strategy import AccountStrategy',
     'autorepeater.account_strategy'),
    ('account_config', 'from .strategies import ALGORITHMS', 'autorepeater.strategies'),
    ('composite_config', 'from t_tech.invest import Client', 't_tech.invest'),
    ('composite_config', 'from .account_strategy import AccountStrategy',
     'autorepeater.account_strategy'),
    ('composite_config', 'from .index_strategy import IndexStrategy',
     'autorepeater.index_strategy'),
    ('composite_config', 'from .strategies import ALGORITHMS', 'autorepeater.strategies'),
    ('composite_config', 'from .index_config import IndexConfig', 'autorepeater.index_config'),
    ('composite_config', 'from .account_config import AccountConfig',
     'autorepeater.account_config'),
    ('repeater', 'from .index_strategy import IndexStrategy', 'autorepeater.index_strategy'),
    ('repeater', 'from autorepeater import strategies', 'autorepeater.strategies'),
    ('repeater', 'from .tinvest_strategy_data import TInvestStrategyData',
     'autorepeater.tinvest_strategy_data'),
    ('repeater', 'from .index_config import IndexConfig', 'autorepeater.index_config'),
    ('repeater', 'from .account_config import AccountConfig', 'autorepeater.account_config'),
    ('repeater', 'from .composite_config import CompositeConfig',
     'autorepeater.composite_config'),
    ('repeater', 'from .triggers import check_triggers', 'autorepeater.triggers'),
    ('index_strategy', 'def helper():\n    import grpc', 'grpc'),
])
def test_checker_rejects_forbidden_imports(module, source, dependency):
    """Every import spelling resolves to the forbidden module, including aliases."""
    assert forbidden_imports(source, f'autorepeater.{module}') == [
        (2 if source.startswith('def ') else 1, dependency)]


@pytest.mark.parametrize('module, source', [
    ('account_strategy', 'import decimal'),
    ('index_strategy', 'from .strategy_data import InstrumentType'),
    ('index_strategy', 'from . import reporting'),
    ('account_strategy', 'from autorepeater import reporting as output'),
    ('account_strategy', 'from autorepeater.portfolio import TargetPortfolio'),
    ('account_strategy', 'from .triggers import check_triggers'),
    ('index_strategy', 'from .index_config import select_index_config'),
    ('account_strategy', 'from .account_config import load_account_config'),
    ('account_config', 'from decimal import Decimal'),
    ('composite_config', 'from decimal import Decimal'),
    ('composite_config', 'from .reporting import print_config_warning'),
    ('strategy_budget', 'from decimal import Decimal'),
    ('account_strategy', 'from .strategy_budget import available_budget'),
    ('index_strategy', 'from .strategy_budget import available_budget'),
    ('strategy_contract', 'from .strategy_data import StrategyData'),
    ('strategy_contract', 'from .strategy_data import PositionEvent'),
    ('reporting', 'from .portfolio import get_portfolio'),
    ('repeater', 'from t_tech.invest import RequestError'),
    ('repeater', 'from .strategy_contract import validate_strategy'),
    ('repeater', 'from .strategy_data import DataAccessError'),
    ('runner', 'from .tinvest_strategy_data import TInvestStrategyData'),
    ('strategies', 'from .account_strategy import AccountStrategy'),
    ('tinvest_strategy_data', 'import t_tech.invest'),
    ('account_strategy', 'import t_technology'),
    ('account_strategy', 'import grpc_tools'),
    ('account_strategy', 'message = "import grpc"'),
])
def test_checker_accepts_allowed_imports(module, source):
    """Approved edges and unrelated names must not produce false positives."""
    assert forbidden_imports(source, f'autorepeater.{module}') == []


def test_checker_reports_each_import_and_line():
    """Multiple aliases and relative imports cannot hide a later dependency."""
    source = ('import decimal, grpc as transport\n'
              'from . import reporting, runner as launch\n')
    assert forbidden_imports(source, 'autorepeater.account_strategy') == [
        (1, 'grpc'), (2, 'autorepeater.runner')]


def test_production_import_boundaries():
    """Check flat package modules without executing their imports."""
    violations = {}
    for path in sorted(PACKAGE.glob('*.py')):
        module = f'autorepeater.{path.stem}'
        errors = forbidden_imports(path.read_text(encoding='utf-8'), module)
        if errors:
            violations[path.name] = errors
    assert not violations


@pytest.mark.parametrize('module', sorted(STRATEGIES))
def test_event_methods_do_not_own_subscriptions_or_skip_reporting(module):
    """Event decisions stay usable by one shared consumer without opening child streams."""
    tree = ast.parse((PACKAGE / f'{module.rsplit(".", 1)[-1]}.py').read_text(encoding='utf-8'))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert 'events' not in methods
    for name in ('event_accounts', 'should_rebalance'):
        for node in ast.walk(methods[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    'get_portfolio', 'find_instruments', 'get_instrument', 'get_last_prices',
                    'position_events', 'print_skipped_strategy_event',
                }
