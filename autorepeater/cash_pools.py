"""Pass-local native money ownership, independent of RUB security spending caps."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, localcontext
from fractions import Fraction
from types import MappingProxyType

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.fx import FxSnapshot
from autorepeater.purchase_plan import ZERO, difference, leaves, nodes
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import (
    StrategyPlan, exact_product, exact_sum, finite_decimal, validate_path,
)

Path = tuple[int, ...]


def split_cash(total: Decimal, coefficients: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Retain finite owner money exactly; repeating ratios keep the usual residual rule."""
    rounded = proportional_split(total, coefficients)
    denominator = exact_sum(coefficients)
    parts = []
    for value in coefficients:
        part = _finite_cash_share(Fraction(total) * Fraction(value) / Fraction(denominator))
        if part is None:
            return rounded
        parts.append(part)
    return tuple(parts)


def _finite_cash_share(ratio: Fraction) -> Decimal | None:
    denominator = ratio.denominator
    powers = []
    for factor in (2, 5):
        power = 0
        while denominator % factor == 0:
            denominator //= factor
            power += 1
        powers.append(power)
    if denominator != 1:
        return None
    scale = max(powers)
    units = ratio.numerator * 2 ** (scale - powers[0]) * 5 ** (scale - powers[1])
    return Decimal(f'{units}e-{scale}')


def native_allowance(amount: Decimal, currency: str, fx: FxSnapshot) -> Decimal:
    """Round a RUB allowance down in native units; never manufacture extra reserve."""
    if currency == 'rub':
        return amount
    with localcontext(prec=28, rounding=ROUND_FLOOR):
        return amount / fx.rate(currency)


def freeze_money(values: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    """Copy and validate native units without an implicit FX conversion."""
    result = {}
    for currency, amount in values.items():
        code = currency_code(currency)
        if code in result:
            raise ValueError('duplicate cash currency')
        result[code] = finite_decimal(amount, 'native cash')
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class CashPools:
    """Actual authorized balances only; projected FX credits use a separate instance."""
    balances: Mapping[Path, Mapping[str, Decimal]]
    protected: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        copied = {}
        for path, money in self.balances.items():
            validate_path(path)
            copied[path] = freeze_money(money)
        object.__setattr__(self, 'balances', MappingProxyType(copied))
        object.__setattr__(self, 'protected', freeze_money(self.protected))

    def amount(self, path: Path, currency: str) -> Decimal:
        """An account-wide balance never grants another path access."""
        return self.balances.get(path, {}).get(currency_code(currency), ZERO)

    def total(self, currency: str) -> Decimal:
        """Physical upper bound includes protected/unallocated funds."""
        code = currency_code(currency)
        return exact_sum((self.protected.get(code, ZERO), *(
            money.get(code, ZERO) for money in self.balances.values())))


def cash_headroom(strategy: StrategyPlan, ownership: Mapping[Path, Mapping[str, Decimal]],
                  marks: Mapping[str, Decimal]) -> dict[Path, Decimal]:
    """Full leaf money allowance includes its reserve, never the parent's floor."""
    return {path: max(ZERO, difference(node.budget, exact_sum(
        exact_product(quantity, marks[uid]) for uid, quantity in ownership[path].items())))
            for path, node in leaves(strategy).items()}


def initial_cash_pools(strategy: StrategyPlan, ownership: Mapping[Path, Mapping[str, Decimal]],
                       marks: Mapping[str, Decimal], available: Mapping[str, Decimal],
                       fx: FxSnapshot, *, preferences: CashPools | None = None) -> CashPools:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Assign existing money once; retain unallocated COMPOSITE capital separately."""
    limits = cash_headroom(strategy, ownership, marks)
    by_path = {path: {} for path in limits}
    remaining = dict(freeze_money(available))
    if preferences is not None:
        _assign_cash(by_path, limits, remaining, fx, preferences)
    _assign_cash(by_path, limits, remaining, fx, None)
    return CashPools(by_path, remaining)


def _assign_cash(by_path, limits, remaining, fx, preferences):
    currencies = sorted(remaining, key=lambda currency: currency == 'rub')
    for currency in currencies:
        capacities = [native_allowance(
            min(limits[path], preferences.amount(path, currency))
            if preferences is not None else limits[path], currency, fx)
                      for path in limits]
        if not any(capacities):
            continue
        parts = proportional_split(min(remaining[currency], exact_sum(capacities)), capacities,
                                   ceilings=capacities)
        for path, part in zip(limits, parts):
            by_path[path][currency] = exact_sum((by_path[path].get(currency, ZERO), part))
            limits[path] = max(ZERO, difference(limits[path], fx.to_rub(
                MoneyAmount(currency, part)).amount))
        remaining[currency] = difference(remaining[currency], exact_sum(parts))


def sale_paths(origin: Path, by_path: Mapping[Path, StrategyPlan],
               limits: Mapping[Path, Decimal]) -> set[Path]:
    """The established shared/scoped proceeds permissions, including nested barriers."""
    domain = origin
    while domain and by_path[domain[:-1]].decision.redistribution_allowed:
        domain = domain[:-1]
    result = set()
    for path in limits:
        if path[:len(domain)] != domain:
            continue
        crossing = [path[:depth] for depth in range(len(domain), len(path))]
        if path == origin or all(
                by_path[prefix].decision.redistribution_allowed
                or prefix == origin and bool(by_path[origin].unassigned)
                for prefix in crossing):
            result.add(path)
    return result


@dataclass(frozen=True, slots=True)
class CashProceeds:
    """Confirmed attribution or explicit unknown cash; never a quote/balance delta."""
    pieces: Mapping[Path, Decimal]
    cash: Mapping[str, Decimal] | None

    def __post_init__(self) -> None:
        for path, quantity in self.pieces.items():
            validate_path(path)
            finite_decimal(quantity, 'proceeds ownership', positive=True)
        if not self.pieces:
            raise ValueError('proceeds require ownership')
        object.__setattr__(self, 'pieces', MappingProxyType(dict(self.pieces)))
        if self.cash is not None:
            object.__setattr__(self, 'cash', freeze_money(self.cash))


def credit_cash_pools(strategy: StrategyPlan, initial: CashPools,
                      available: Mapping[str, Decimal], headroom: Mapping[Path, Decimal],
                      fx: FxSnapshot, proceeds: Sequence[CashProceeds]) -> CashPools:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Fresh cash constrains attribution; unknown isolated proceeds remain deferred (#9)."""
    available = freeze_money(available)
    if any(initial.total(currency) > amount for currency, amount in available.items()) or any(
            initial.total(currency) and currency not in available
            for money in (*initial.balances.values(), initial.protected) for currency in money):
        raise ValueError('initial pools exceed fresh cash; debit the original owner first')
    balances = {path: dict(money) for path, money in initial.balances.items()}
    by_path = {node.path: node for node in nodes(strategy)}
    limits = dict(headroom)
    for path in limits:
        limits[path] = max(ZERO, difference(limits[path], exact_sum(
            fx.to_rub(MoneyAmount(currency, amount)).amount
            for currency, amount in balances.get(path, {}).items())))
    unknown_shared = bool(proceeds)
    for credit in proceeds:
        origins = sorted(credit.pieces)
        if any(origin not in by_path for origin in origins):
            raise ValueError('unknown proceeds owner')
        scopes = {origin: sale_paths(origin, by_path, limits) for origin in origins}
        unknown_shared = unknown_shared and all(scope == set(limits) for scope in scopes.values())
        for currency, amount in (credit.cash or {}).items():
            parts = proportional_split(amount, [credit.pieces[origin] for origin in origins])
            for origin, part in zip(origins, parts):
                _credit(balances, initial.protected, available, limits, fx,
                        MoneyAmount(currency, part), scopes[origin])
    if unknown_shared and any(not credit.cash for credit in proceeds):
        for currency, amount in available.items():
            _credit(balances, initial.protected, available, limits, fx,
                    MoneyAmount(currency, amount), set(limits))
    return CashPools(balances, initial.protected)


def _credit(balances, protected, available, limits, fx, money, scope):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    remaining = max(ZERO, difference(available.get(money.currency, ZERO), exact_sum((
        protected.get(money.currency, ZERO), *(
            values.get(money.currency, ZERO) for values in balances.values())))))
    paths = sorted(scope)
    caps = [native_allowance(limits[path], money.currency, fx) for path in paths]
    if not any(caps):
        return
    parts = proportional_split(min(money.amount, remaining, exact_sum(caps)), caps, ceilings=caps)
    for path, amount in zip(paths, parts):
        values = balances.setdefault(path, {})
        values[money.currency] = exact_sum((values.get(money.currency, ZERO), amount))
        limits[path] = max(ZERO, difference(limits[path], fx.to_rub(
            MoneyAmount(money.currency, amount)).amount))
