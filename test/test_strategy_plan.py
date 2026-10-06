"""Independent validation of the future financial contract."""
from dataclasses import replace
from decimal import Decimal as D, getcontext

import pytest

from autorepeater.portfolio import TargetPortfolio, validate_target
from autorepeater.strategy_data import InstrumentType, PortfolioEntry
from autorepeater.strategy_plan import (
    AllocationProfile, StrategyContext, StrategyDecision, StrategyPlan, TradeMode,
    exact_sum, validate_context, validate_decision, validate_plan, validate_profile,
)


def position(uid='X', quantity='10', price='100'):
    """A neutral long security position."""
    return PortfolioEntry(uid, InstrumentType.SHARE, 'rub', D(price), D(quantity), '')


def context():
    """Zero old capital is valid for initialization."""
    return StrategyContext((), D(1000), D(0), False, (), {'X': D(100)})


def plan():
    """One valid future leaf plan."""
    return StrategyPlan((), D(1000), TargetPortfolio({'X': D(1)}, {'X': D(100)}),
                        D(10), StrategyDecision(TradeMode.BUY_ONLY, False, 'initial',
                                                None, None), (), {})


def test_initial_context_and_plan():
    """Models accept the empty destination without weakening target validation."""
    validate_context(context())
    validate_plan(plan(), context())
    validate_profile(AllocationProfile({}, {}, D(0)))
    validate_target(TargetPortfolio({'X': D(-1)}, {'X': D(0)}))


@pytest.mark.parametrize('bad', [None, 1, '1', D('NaN'), D('Infinity'), D('-1')])
@pytest.mark.parametrize('field', ['budget', 'previous_budget'])
def test_bad_context_numbers(field, bad):
    """Financial inputs are finite Decimal values without coercion."""
    with pytest.raises(ValueError, match=field):
        validate_context(replace(context(), **{field: bad}))


@pytest.mark.parametrize('changes', [
    {'budget': D(0)}, {'path': []}, {'path': (True,)}, {'path': (-1,)},
    {'budget_reduction_requires_rebalance': 1}, {'budget_reduction_requires_rebalance': True},
    {'positions': []}, {'positions': (object(),)}, {'marks': []},
    {'positions': (position(quantity='-1'),)}, {'positions': (position(price='0'),)},
    {'positions': (position(),), 'marks': {}},
    {'positions': (position(),), 'marks': {'X': D(101)}},
    {'positions': (position(), position())},
])
def test_bad_context_interfaces(changes):
    """Bad paths, positions and mismatched marks fail before future trading."""
    with pytest.raises(ValueError):
        validate_context(replace(context(), **changes))


@pytest.mark.parametrize('changes', [
    {'exposures': []}, {'exposures': {'': D(1)}}, {'exposures': {1: D(1)}},
    {'exposures': {'X': D(-1)}}, {'exposures': {'X': 1}},
    {'exposures': {'X': D('NaN')}}, {'exposures': {'X': D('Infinity')}},
    {'prices': []}, {'prices': {}}, {'prices': {'X': '1'}},
    {'prices': {'X': D('NaN')}}, {'prices': {'X': D(0)}},
    {'reserve_fraction': D(-1)}, {'reserve_fraction': '0'},
    {'reserve_fraction': D('NaN')}, {'reserve_fraction': D(1)},
])
def test_bad_profile(changes):
    """All recognized UIDs, including zeros, require valid prices."""
    with pytest.raises(ValueError):
        valid = AllocationProfile({'X': D('.9')}, {'X': D(10)}, D('.1'))
        validate_profile(replace(valid, **changes))


def test_profile_sum_is_exact():
    """A tiny invalid excess must not disappear in the default context."""
    with pytest.raises(ValueError, match='sum'):
        validate_profile(AllocationProfile({'X': D(1), 'Y': D('1e-80')},
                                          {'X': D(1), 'Y': D(1)}, D(0)))
    assert exact_sum([D('1e80'), D('1e-80'), -D('1e80')]) == D('1e-80')
    assert exact_sum([]) == 0
    assert getcontext().prec == 28


@pytest.mark.parametrize('changes', [
    {'mode': 'BUY_ONLY'}, {'redistribution_allowed': 1}, {'reason': ''}, {'reason': None},
    {'metric': D(-1)}, {'metric': D('NaN')}, {'limit': '0'}, {'limit': D('Infinity')},
    {'redistribution_allowed': True},
])
def test_bad_decision(changes):
    """Permissions are typed fields and cannot be inferred from a reason."""
    with pytest.raises(ValueError):
        validate_decision(replace(plan().decision, **changes))


@pytest.mark.parametrize('changes', [
    {'path': (0,)}, {'budget': D(999)}, {'cash_floor': D(-1)}, {'cash_floor': D(1001)},
    {'cash_floor': None}, {'decision': None}, {'children': []}, {'children': (object(),)},
    {'unassigned': []}, {'unassigned': {'X': D(-1)}},
    {'target': None}, {'target': TargetPortfolio({}, {})},
    {'target': TargetPortfolio({'X': D(0)}, {'X': D(100)})},
    {'target': TargetPortfolio({'X': D(-1)}, {'X': D(100)})},
    {'target': TargetPortfolio({'X': D(1)}, {'X': D(0)})},
])
def test_bad_plan(changes):
    """New restrictions live only on the financial plan."""
    with pytest.raises(ValueError):
        validate_plan(replace(plan(), **changes), context())


def test_reduction_flag_requires_rebalance():
    """Reserve-only reductions may stay buy-only; forced reductions may not."""
    reduced = replace(context(), previous_budget=D(2000), budget_reduction_requires_rebalance=True)
    with pytest.raises(ValueError, match='REBALANCE'):
        validate_plan(plan(), reduced)
    rebalancing = replace(plan(), decision=replace(plan().decision, mode=TradeMode.REBALANCE))
    validate_plan(rebalancing, reduced)
    validate_plan(plan(), replace(reduced, budget_reduction_requires_rebalance=False))


def test_ordered_child_paths_and_budgets():
    """Repeated sources remain separate occurrences by index, not by name."""
    children = tuple(replace(plan(), path=(i,), budget=D(400), cash_floor=D(4)) for i in range(2))
    validate_plan(replace(plan(), children=children), context())
    with pytest.raises(ValueError, match='path'):
        validate_plan(replace(plan(), children=(children[0], children[0])), context())
    with pytest.raises(ValueError, match='budget'):
        excessive = tuple(replace(child, budget=D(600)) for child in children)
        validate_plan(replace(plan(), children=excessive), context())


@pytest.mark.parametrize('validator', [validate_context, validate_profile, validate_decision,
                                       validate_plan])
def test_model_type_is_required(validator):
    """Foreign DTO interfaces do not silently become valid financial models."""
    with pytest.raises(ValueError):
        validator(object())


@pytest.mark.parametrize('value', [D('NaN'), D('Infinity'), 1, None])
def test_exact_sum_rejects_invalid_operands(value):
    """The exact arithmetic helper does not hide damaged inputs."""
    with pytest.raises(ValueError):
        exact_sum([value])


@pytest.mark.parametrize('instrument_type', [InstrumentType.CURRENCY, 'share', None])
def test_context_securities_only(instrument_type):
    """Cash is budget, not a duplicated virtual security holding."""
    with pytest.raises(ValueError, match='security type'):
        validate_context(replace(context(), positions=(replace(position(),
                                                               instrument_type=instrument_type),)))


def test_zero_quantity_price_does_not_impose_active_position_constraints():
    """Only nonzero financial positions require a positive valuation."""
    zero = position(quantity='0', price='0')
    validate_context(replace(context(), positions=(zero,), marks={'X': D(0)}))
    validate_profile(AllocationProfile({'X': D(0)}, {'X': D(0)}, D(0)))
