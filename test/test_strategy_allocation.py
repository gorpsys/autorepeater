"""Profiles and virtual ownership use snapshots, never future rounded targets."""
from dataclasses import replace
from decimal import Decimal as D, getcontext, localcontext, ROUND_UP
from types import SimpleNamespace
from test.test_strategy_plan import position

import pytest

from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.composite_strategy import CompositeStrategy, CompositeSnapshot
from autorepeater.index_strategy import IndexQuote, IndexStrategy, _index_price
from autorepeater.strategy_allocation import (
    attribute_positions, build_marks, combine_profiles, normalize_profile,
    proportional_split, recovered_capital, position_value,
)
from autorepeater.strategy_plan import AllocationProfile, exact_sum
from autorepeater.strategy_data import InstrumentType


def profile(exposures, reserve='0', prices=None):
    """Convenient neutral profile, retaining zero coefficients."""
    return AllocationProfile({uid: D(value) for uid, value in exposures.items()},
                             prices or {uid: D(100) for uid in exposures}, D(reserve))


def test_shared_uid_fractional_attribution_and_warning(caplog):
    """Claims .30/.10 give 7.5/2.5, with one destination mark."""
    profiles = (profile({'X': '.5'}), profile({'X': '.25'}))
    positions = (position(),)
    marks = build_marks(positions, profiles)
    result = attribute_positions(positions, profiles, (D('.6'), D('.4')), marks, path=(2,))
    assert tuple(child[0].quantity for child in result.children) == (D('7.5'), D('2.5'))
    assert not result.unassigned
    assert position_value(result.children[0], marks) == 750
    assert recovered_capital(result.children[0], profiles[0], marks) == 1500
    assert 'X' in caplog.text and '(2, 0)' in caplog.text and '7.5' in caplog.text
    assert all(record.levelname == 'WARNING' for record in caplog.records)


def test_nested_parent_quantity_single_and_unassigned():
    """A nested node can only divide the quantity its parent received."""
    profiles = (profile({'X': '1'}), profile({'X': '0', 'Y': '.5'}))
    parent = attribute_positions(
        (position(), position('Y', '2'), position('old', '3')), profiles,
        (D('.1'), D('.2')), {'X': D(100), 'Y': D(100), 'old': D(100)})
    nested = attribute_positions(parent.children[0], (profile({'X': '1'}),) * 2,
                                (D('.5'),) * 2, {'X': D(100)}, path=(0,))
    assert exact_sum(p[0].quantity for p in nested.children) == 10
    assert parent.children[1][0].uid == 'Y'
    assert parent.unassigned == {'old': D(3)}
    zeros = attribute_positions((position(),), (profile({'X': '0'}),), (D(1),), {'X': D(100)})
    assert zeros.unassigned == {'X': D(10)}


def test_marks_destination_precedence_and_profile_maximum():
    """Distinct UIDs remain distinct even when source metadata looks alike."""
    profiles = (profile({'X': '.5', 'Y': '.5'}, prices={'X': D(200), 'Y': D(7)}),
                profile({'X': '1', 'Z': '0'}, prices={'X': D(300), 'Z': D(9)}))
    assert build_marks((position(),), profiles) == {'X': D(100), 'Y': D(7), 'Z': D(9)}
    assert build_marks((), profiles)['X'] == 300


@pytest.mark.parametrize('values,total', [
    ([D(1)] * 3, D(10)), ([D(1)] * 100, D('1e-80')),
    ([D('1e80'), D('1e-80'), D(3)], D('123.4567890123456789012345678')),
    ([D(1), D(1), D('1e-80')], D(1)),
])
def test_exact_split(values, total):
    """The assigned residual conserves quantities even across extreme magnitudes."""
    parts = proportional_split(total, values)
    assert exact_sum(parts) == total
    assert all(part >= 0 for part in parts)
    assert getcontext().prec == 28


def test_negative_residual_recomputed():
    """A tiny winning coefficient may not absorb a negative normalization residual."""
    values = [D('1.000000000000000000000000001')] + [D(1)] * 200
    parts = proportional_split(D('1e-90'), values)
    assert exact_sum(parts) == D('1e-90')
    assert min(parts) >= 0
    with localcontext() as decimal_context:
        decimal_context.Emin = -10
        decimal_context.rounding = ROUND_UP
        subnormal_parts = proportional_split(D('1e-37'), [D(1)] * 3)
        assert exact_sum(subnormal_parts) == D('1e-37')
        assert min(subnormal_parts) >= 0


@pytest.mark.parametrize('total,values', [
    (D(-1), [D(1)]), (D('NaN'), [D(1)]), (D(1), []),
    (D(1), [D(0)]), (D(1), [D(-1)]), (D(1), [1]),
])
def test_invalid_split(total, values):
    """Invalid input is rejected, never repaired by rounding."""
    with pytest.raises(ValueError):
        proportional_split(total, values)


@pytest.mark.parametrize('positions', [
    (position(quantity='-1'),), (position(price='0'),),
    (position(price='NaN'),), (position(), position()), (object(),),
])
def test_invalid_destination(positions):
    """Long-only validation is independent from the old target API."""
    with pytest.raises(ValueError):
        build_marks(positions, ())


def test_capital_requires_positive_profile_and_shared_valid_marks():
    """Zero previous capital is valid; a zero invested fraction is not."""
    assert recovered_capital((), profile({'X': '.99'}, '.01'), {'X': D(100)}) == 0
    with pytest.raises(ValueError, match='fraction'):
        recovered_capital((), profile({'X': '0'}), {'X': D(100)})
    for marks in ({}, {'X': D(101)}, {'X': D('NaN')}):
        with pytest.raises(ValueError):
            position_value((position(),), marks)


def test_account_nano_profile_and_currency_exclusion():
    """The source weights preserve nano valuation without changing target arithmetic."""
    config = SimpleNamespace(source_account_id='001', reserve=D('.01'))
    strategy = AccountStrategy(PreparedAccountSource('001', config))
    a, b = position('A', '1', '.0000000014'), position('B', '1', '.0000000024')
    cash = replace(position('rub'), instrument_type=InstrumentType.CURRENCY)
    snapshot = ({'A': a, 'B': b, 'rub': cash}, D('.000000003'))
    result = strategy.allocation_profile(snapshot)
    assert result.exposures == {'A': D('.33'), 'B': D('.66')}
    assert result.reserve_fraction == D('.01')
    assert result.prices == {'A': a.current_price, 'B': b.current_price}
    with pytest.raises(ValueError):
        strategy.allocation_profile(({'A': replace(a, quantity=D(-1))}, D(1)))
    with pytest.raises(ValueError):
        strategy.allocation_profile(({}, D(0)))


def test_index_current_capitalizations_full_profile_before_tail():
    """Dynamic capitalization replaces reference percentages and ignores lot cuts."""
    items = tuple(SimpleNamespace(ticker=ticker, reference_index_capitalization=D(100),
                                  reference_price=D(10)) for ticker in ('A', 'B'))
    strategy = IndexStrategy(SimpleNamespace(instruments=items, reserve=D('.1')))
    snapshot = {'A': IndexQuote('A', D(20), 100), 'B': IndexQuote('B', D(10), 100)}
    result = strategy.allocation_profile(snapshot)
    assert result.exposures == {'A': D('.6'), 'B': D('.3')}
    assert result.reserve_fraction == D('.1')
    with pytest.raises(ValueError, match='invalid index price'):
        _index_price('10', 'A')


def test_composite_opaque_profiles_reserves_once_and_unallocated():
    """A composition uses only the child's neutral method, including nested reserves."""
    leaf = profile({'X': '.99', 'zero': '0'}, '.01')
    inner = combine_profiles((leaf,), (D('.5'),))
    assert inner.exposures == {'X': D('.495'), 'zero': D(0)}
    assert inner.reserve_fraction == D('.005')
    strategy = object.__new__(CompositeStrategy)
    strategy.source = SimpleNamespace(components=(SimpleNamespace(weight=D('.6')),
                                                  SimpleNamespace(weight=D('.3'))))
    calls = []
    def child_profile(snapshot):
        calls.append(snapshot)
        return inner
    strategy.children = (SimpleNamespace(allocation_profile=child_profile),) * 2
    result = strategy.allocation_profile(CompositeSnapshot(('first', 'second')))
    assert calls == ['first', 'second']
    assert result.exposures == {'X': D('.4455'), 'zero': D(0)}
    assert result.reserve_fraction == D('.0045')
    assert exact_sum(result.exposures.values()) + result.reserve_fraction == D('.45')
    with pytest.raises(ValueError, match='snapshot'):
        strategy.allocation_profile(CompositeSnapshot(('first',)))


def test_profile_normalization_tie_uses_uid_and_zero_prices():
    """Normalization is deterministic and retains complete profile metadata."""
    result = normalize_profile({'Z': D(1), 'A': D(1), 'zero': D(0)},
                               {'Z': D(1), 'A': D(2), 'zero': D(3)}, D('.01'))
    assert exact_sum(result.exposures.values()) == D('.99')
    assert result.exposures['zero'] == 0
    assert result.prices['zero'] == 3


@pytest.mark.parametrize('profiles,weights', [
    ((), ()), ((profile({'X': '1'}),), ()),
    ([profile({'X': '1'})], (D(1),)),
    ((profile({'X': '1'}),), [D(1)]),
    ((profile({'X': '1'}),) * 2, (D('.6'),) * 2),
    ((profile({'X': '1'}),), (D(-1),)),
    ((object(),), (D(1),)),
])
def test_bad_component_interfaces(profiles, weights):
    """Wrong lengths, types and excessive pools fail before attribution."""
    with pytest.raises(ValueError):
        combine_profiles(profiles, weights)


def test_no_invested_leaf_fraction():
    """A neutral zero profile cannot serve as a working leaf investment profile."""
    with pytest.raises(ValueError, match='fraction'):
        normalize_profile({'X': D(1)}, {'X': D(1)}, D(1))


@pytest.mark.parametrize('snapshot', [None, (), ({},), ([], D(1)),
                                       ({'X': object()}, D(1)),
                                       ({'wrong': position()}, D(1000)),
                                       ({'X': position()}, D(999))])
def test_bad_account_snapshot(snapshot):
    """Profile validation rejects corrupted neutral snapshots, not only bad numbers."""
    config = SimpleNamespace(source_account_id='001', reserve=D('.01'))
    strategy = AccountStrategy(PreparedAccountSource('001', config))
    with pytest.raises(ValueError):
        strategy.allocation_profile(snapshot)


@pytest.mark.parametrize('snapshot', [None, [], {'A': object()}])
def test_bad_index_snapshot(snapshot):
    """A malformed price interface raises a diagnostic data error."""
    item = SimpleNamespace(ticker='A', reference_index_capitalization=D(100),
                           reference_price=D(10))
    strategy = IndexStrategy(SimpleNamespace(instruments=(item,), reserve=D('.01')))
    with pytest.raises(ValueError):
        strategy.allocation_profile(snapshot)


def test_exact_quantity_and_value_conservation_with_unassigned(caplog):
    """All scopes preserve marked value, which differs from original nano totals."""
    positions = (position('X', '1.123456789012345678901234567', '1.234567891234567891'),
                 position('old', '2.987654321098765432109876543', '.0000000014'))
    profiles = (profile({'X': '1'}),) * 3
    marks = build_marks(positions, profiles)
    attributed = attribute_positions(positions, profiles, (D('.3'),) * 3, marks)
    assert exact_sum(child[0].quantity for child in attributed.children) == positions[0].quantity
    unassigned = (replace(positions[1], quantity=attributed.unassigned['old']),)
    assert exact_sum([position_value(child, marks) for child in attributed.children]
                     + [position_value(unassigned, marks)]) == position_value(positions, marks)
    nano_total = sum((entry.quantity * entry.current_price).quantize(D('.000000001'))
                     for entry in positions)
    assert nano_total != position_value(positions, marks)
    assert 'old' not in caplog.text


@pytest.mark.parametrize('values', [None, [], {'X': '1'}, {'X': D('NaN')}])
def test_normalization_interface(values):
    """The leaf normalization helper rejects malformed inputs explicitly."""
    with pytest.raises(ValueError):
        normalize_profile(values, {'X': D(100)}, D('.01'))


def test_zero_position_marks_and_invalid_profile_collection():
    """Zero holdings can carry zero prices; malformed collections cannot hide data."""
    assert build_marks((position(quantity='0', price='0'),), ()) == {'X': D(0)}
    with pytest.raises(ValueError):
        build_marks((), None)


def test_profile_decimal_residual_receiver_is_deterministic():
    """UID ties use lexical order, while attribution ties use component order."""
    prices = {uid: D(1) for uid in ('Z', 'A', 'M')}
    result = normalize_profile(dict.fromkeys(prices, D(1)), prices, D(0))
    third = D(1) / D(3)
    assert result.exposures['Z'] == result.exposures['M'] == third
    assert result.exposures['A'] == exact_sum((D(1), third.copy_negate(), third.copy_negate()))
    result = attribute_positions((position(quantity='1'),), (profile({'X': '1'}),) * 3,
                                (D('.3'),) * 3, {'X': D(100)})
    assert result.children[1][0].quantity == result.children[2][0].quantity == third
    assert result.children[0][0].quantity > third


@pytest.mark.parametrize('cap', ['0.3333333333333333333333333333',
                                '0.3333333333333333333333333334'])
def test_bounded_split_preserves_deficits_at_exact_capacity(cap):
    """Correction must not exceed a region's monetary deficit, even by a tail."""
    caps = (D(cap),) * 3
    total = exact_sum(caps)
    parts = proportional_split(total, caps, ceilings=caps)
    assert parts == caps
    assert exact_sum(parts) == total


def test_bounded_split_infeasible_proportions():
    """Enough total capacity cannot repair an individually excessive proportion."""
    with pytest.raises(ValueError, match='proportion'):
        proportional_split(D(1), (D(3), D(1)), ceilings=(D('.5'), D('.5')))


@pytest.mark.parametrize('ceilings', [(), (D(-1),), (D('.9'),), (None,), (D('NaN'),)])
def test_bounded_split_rejects_bad_caps(ceilings):
    """Bad upper bounds and insufficient capacity cannot be corrected into validity."""
    with pytest.raises(ValueError):
        proportional_split(D(1), (D(1),), ceilings=ceilings)
