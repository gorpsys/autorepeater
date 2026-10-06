"""Pure profile composition and fractional attribution of parent-owned positions."""
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
import logging

from autorepeater.strategy_plan import (
    AllocationProfile, exact_product, exact_sum, finite_decimal, validate_path,
    validate_map, validate_positions, validate_profile,
)
from autorepeater.strategy_data import PortfolioEntry


LOGGER = logging.getLogger('tinkoffBot')


@dataclass(frozen=True)
class PositionAttribution:
    """Ordered child occurrences and quantities without a positive claimant."""
    children: tuple[tuple[PortfolioEntry, ...], ...]
    unassigned: dict[str, Decimal]


def proportional_split(total, coefficients, *, ceilings=None):
    """Preserve total exactly; the largest first coefficient receives the residual."""
    finite_decimal(total, 'split total')
    coefficients = tuple(coefficients)
    for value in coefficients:
        finite_decimal(value, 'split coefficient')
    denominator = exact_sum(coefficients)
    if denominator <= 0:
        raise ValueError('split requires a positive coefficient sum')
    if ceilings is None:
        ceilings = (total,) * len(coefficients)
    else:
        ceilings = tuple(ceilings)
        if len(ceilings) != len(coefficients):
            raise ValueError('split ceilings must match coefficients')
        for ceiling in ceilings:
            finite_decimal(ceiling, 'split ceiling')
        if exact_sum(ceilings) < total:
            raise ValueError('split ceilings cannot cover total')
        # Caps constrain this proportional split, not a different allocation algorithm.
        if any(exact_product(total, value) > exact_product(ceiling, denominator)
               for value, ceiling in zip(coefficients, ceilings)):
            raise ValueError('split proportion exceeds ceiling')
    receiver = max(range(len(coefficients)), key=coefficients.__getitem__)
    precision = 28
    while True:
        with localcontext() as context:
            context.prec = precision
            parts = [total * value / denominator for value in coefficients]
        others = exact_sum(value for index, value in enumerate(parts) if index != receiver)
        parts[receiver] = exact_sum((total, others.copy_negate()))
        if all(0 <= value <= ceiling for value, ceiling in zip(parts, ceilings)):
            return tuple(parts)
        # A rounded sum of the other parts can overshoot for very many candidates.
        precision *= 2


def normalize_profile(values, prices, reserve):
    """Leaf exposures exhaust the invested fraction, including zero-coefficient UIDs."""
    validate_map(values, 'profile values')
    validate_profile(AllocationProfile(dict.fromkeys(values, Decimal(0)), prices, reserve))
    if reserve >= 1:
        raise ValueError('profile invested fraction must be positive')
    uids = sorted(values)
    parts = proportional_split(exact_sum((Decimal(1), reserve.copy_negate())),
                               [values[uid] for uid in uids])
    result = AllocationProfile(dict(zip(uids, parts)), dict(prices), reserve)
    validate_profile(result)
    return result


def component_weights(profiles, weights):
    """Validate neutral components and correct the schema's rounded weight pool."""
    if (not isinstance(profiles, tuple) or not isinstance(weights, tuple)
            or len(profiles) != len(weights) or not profiles):
        raise ValueError('components: expected matching nonempty profile/weight tuples')
    for profile in profiles:
        validate_profile(profile)
    for weight in weights:
        finite_decimal(weight, 'component weight', positive=True)
    with localcontext() as context:
        context.prec = 28
        total = sum(weights, Decimal(0))
    if total > 1:
        raise ValueError('component weight sum must not exceed 1')
    # Schema permits a rounded sum of one. Preserve that pool by correcting its split.
    return proportional_split(total, weights)


def combine_profiles(profiles, weights):
    """Compose neutral exposures, keeping unallocated capital separate from reserves."""
    weights = component_weights(profiles, weights)
    exposures = {}
    reserves = []
    prices = {}
    for profile, weight in zip(profiles, weights):
        uids = sorted(profile.exposures)
        used = exact_sum((*profile.exposures.values(), profile.reserve_fraction))
        fractions = [profile.exposures[uid] for uid in uids] + [
            profile.reserve_fraction, exact_sum((Decimal(1), used.copy_negate()))]
        parts = proportional_split(weight, fractions)
        for uid, part in zip(uids, parts):
            exposures[uid] = exact_sum((exposures.get(uid, Decimal(0)), part))
        reserves.append(parts[-2])
        for uid, price in profile.prices.items():
            prices[uid] = max(prices.get(uid, price), price)
    result = AllocationProfile(exposures, prices, exact_sum(reserves))
    validate_profile(result)
    return result


def build_marks(positions, profiles):
    """Destination prices take precedence; otherwise use the maximum profile estimate."""
    validate_positions(positions)
    if not isinstance(profiles, tuple):
        raise ValueError('marks profiles: expected a tuple')
    marks = {}
    for profile in profiles:
        validate_profile(profile)
        for uid, price in profile.prices.items():
            marks[uid] = max(marks.get(uid, price), price)
    marks.update((entry.uid, entry.current_price) for entry in positions)
    return marks


def position_value(positions, marks):
    """Value virtual holdings without introducing per-child nano rounding."""
    validate_positions(positions, marks)
    return exact_sum(exact_product(entry.quantity, marks[entry.uid]) for entry in positions)


def recovered_capital(positions, profile, marks):
    """Recover full capital C=V/f, allowing zero old capital but never zero f."""
    validate_profile(profile)
    fraction = exact_sum(profile.exposures.values())
    if fraction <= 0:
        raise ValueError('profile invested fraction must be positive')
    return position_value(positions, marks) / fraction


def attribute_positions(positions, profiles, weights, marks, path=()):
    """Split only this node's received quantities, without rounding virtual lots."""
    validate_path(path)
    validate_positions(positions, marks)
    weights = component_weights(profiles, weights)
    children = [[] for _ in profiles]
    unassigned = {}
    for entry in positions:
        claims = [weight * profile.exposures.get(entry.uid, Decimal(0))
                  for profile, weight in zip(profiles, weights)]
        candidates = [index for index, claim in enumerate(claims) if claim > 0]
        if not candidates:
            unassigned[entry.uid] = entry.quantity
            continue
        parts = proportional_split(entry.quantity, [claims[index] for index in candidates])
        for index, quantity in zip(candidates, parts):
            children[index].append(replace(entry, quantity=quantity))
        if len(candidates) > 1:
            details = tuple((path + (index,), claims[index], quantity)
                            for index, quantity in zip(candidates, parts))
            LOGGER.warning('Ambiguous ownership UID %s: (path, coefficient, quantity) %s',
                           entry.uid, details)
    return PositionAttribution(tuple(tuple(child) for child in children), unassigned)
