"""Pure conditional purchase funding; no SDK effects or future broker-cap guesses."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, localcontext
from math import gcd, lcm
from types import MappingProxyType

from autorepeater.cash_facts import MoneyAmount, currency_code
from autorepeater.cash_pools import (
    CashPools, Path, cash_headroom, freeze_money, initial_cash_pools, native_allowance,
)
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.fx import FxSnapshot
from autorepeater.fx_data import FxLotQuote, fx_ratio
from autorepeater.purchase_plan import (
    OrderIntent, ZERO, allocate_floor, difference, funding_candidates, leaves,
    take_pieces, validate_intent,
)
from autorepeater.strategy_allocation import proportional_split
from autorepeater.strategy_plan import StrategyPlan, exact_product, exact_sum


def _freeze_costs(values):
    return MappingProxyType({uid: freeze_money(money) for uid, money in values.items()})


@dataclass(frozen=True, slots=True)
class FundingInputs:
    """Explicit native midpoint/fee estimates and current FX rules, all pass-local."""
    native_prices: Mapping[str, MoneyAmount] = field(default_factory=dict)
    fx_rules: Mapping[str, TradeRules] = field(default_factory=dict)
    cash: CashPools | None = None
    fees: Mapping[str, Mapping[str, Decimal]] = field(default_factory=dict)
    fx_fees: Mapping[str, Mapping[str, Decimal]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(not isinstance(price, MoneyAmount) or price.amount <= 0
               for price in self.native_prices.values()):
            raise ValueError('native security midpoint must be positive money')
        object.__setattr__(self, 'native_prices', MappingProxyType(dict(self.native_prices)))
        object.__setattr__(self, 'fx_rules', MappingProxyType(dict(self.fx_rules)))
        object.__setattr__(self, 'fees', _freeze_costs(self.fees))
        object.__setattr__(self, 'fx_fees', _freeze_costs(self.fx_fees))


@dataclass(frozen=True, slots=True)
class FxIntent:  # pylint: disable=too-many-instance-attributes
    """Estimated direct exchange with exact owner contributions, not an order receipt."""
    uid: str
    side: str
    lots: int
    lot_size: int
    base_contributions: Mapping[Path, Decimal]
    debits: Mapping[Path, Mapping[str, Decimal]]
    credits: Mapping[Path, Mapping[str, Decimal]]
    dependent_uids: tuple[str, ...]
    estimated_fees: Mapping[Path, Mapping[str, Decimal]]
    debit_limits: Mapping[Path, Mapping[str, Decimal]]

    def __post_init__(self) -> None:
        validate_intent(OrderIntent(self.uid, self.side, self.lots, self.lot_size,
                                    dict(self.base_contributions)))
        object.__setattr__(self, 'base_contributions', MappingProxyType(dict(
            self.base_contributions)))
        object.__setattr__(self, 'debits', CashPools(self.debits).balances)
        object.__setattr__(self, 'credits', CashPools(self.credits).balances)
        object.__setattr__(self, 'estimated_fees', CashPools(self.estimated_fees).balances)
        object.__setattr__(self, 'debit_limits', CashPools(self.debit_limits).balances)


@dataclass(frozen=True, slots=True)
class FundedPurchase:
    """Conditional lots need fresh readiness/money/GetMaxLots after their exchanges."""
    intent: OrderIntent
    native_amount: MoneyAmount
    executable_now: bool
    fx_dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundingPlan:  # pylint: disable=too-many-instance-attributes
    """Actual cash and RUB caps remain distinct from all projected exchange balances."""
    cash: CashPools
    spending_limits: Mapping[Path, Decimal]
    reserve_shares: Mapping[Path, Mapping[str, Decimal]]
    demands: tuple[OrderIntent, ...]
    purchases: tuple[FundedPurchase, ...]
    exchanges: tuple[FxIntent, ...]
    projected_cash: CashPools
    security_debits: CashPools
    estimated_security_fees: CashPools
    native_prices: Mapping[str, MoneyAmount]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'spending_limits', MappingProxyType(dict(self.spending_limits)))
        object.__setattr__(self, 'reserve_shares', CashPools(self.reserve_shares).balances)
        object.__setattr__(self, 'native_prices', MappingProxyType(dict(self.native_prices)))


def reserve_shares(strategy: StrategyPlan, marks: Mapping[str, Decimal],
                   rules: Mapping[str, TradeRules], fx: FxSnapshot) -> CashPools:
    """Split only each leaf's existing reserve by target RUB value, once per currency."""
    result = {}
    for path, node in leaves(strategy).items():
        weights = {}
        for uid, quantity in node.target.quantities.items():
            if quantity:
                if uid not in rules:
                    raise ValueError('missing settlement currency for target')
                currency = currency_code(rules[uid].currency)
                weights[currency] = exact_sum((weights.get(currency, ZERO), exact_product(
                    quantity, marks[uid])))
        currencies = sorted(weights)
        parts = proportional_split(node.cash_floor, [weights[code] for code in currencies])
        result[path] = {code: native_allowance(part, code, fx)
                        for code, part in zip(currencies, parts)}
    return CashPools(result)


@dataclass(frozen=True)
class _Context:  # pylint: disable=too-many-instance-attributes
    fx: FxSnapshot
    inputs: FundingInputs
    cash: CashPools
    reserves: CashPools
    headroom: Mapping[Path, Decimal]
    prices: Mapping[str, MoneyAmount]
    rules: Mapping[str, TradeRules]
    native_claims: CashPools


def _add(values, path, currency, amount):
    money = values.setdefault(path, {})
    money[currency] = exact_sum((money.get(currency, ZERO), amount))


def _purchase_debits(context: _Context, orders: tuple[OrderIntent, ...]
                     ) -> tuple[CashPools, CashPools]:
    principal, fees = {}, {}
    for intent in orders:
        price = context.prices[intent.uid]
        for path, pieces in intent.pieces.items():
            _add(principal, path, price.currency, exact_product(pieces, price.amount))
        for currency, amount in context.inputs.fees.get(intent.uid, {}).items():
            paths = sorted(intent.pieces)
            parts = proportional_split(exact_product(amount, Decimal(intent.lots)),
                                       [intent.pieces[path] for path in paths])
            for path, part in zip(paths, parts):
                _add(fees, path, currency, part)
    return CashPools(principal), CashPools(fees)


def _combined(principal: CashPools, fees: CashPools) -> dict[Path, dict[str, Decimal]]:
    result = {path: dict(money) for path, money in principal.balances.items()}
    for path, money in fees.balances.items():
        for currency, amount in money.items():
            _add(result, path, currency, amount)
    return result


def _ceil_lots(amount: Decimal, size: Decimal) -> int:
    lots = allocate_floor(amount, size)
    return lots + (exact_product(Decimal(lots), size) < amount)


def _relative_weights(values):
    """Cancel common Decimal units exactly before the established precision28 split."""
    ratios = [value.as_integer_ratio() for value in values]
    denominator = lcm(*(part[1] for part in ratios))
    integers = [numerator * (denominator // divisor) for numerator, divisor in ratios]
    common = gcd(*integers)
    return [Decimal(value // common) for value in integers]


def _exchange(context: _Context, currency: str, side: str, needs: Mapping[Path, Decimal],
              capacities: Mapping[Path, Decimal], orders: tuple[OrderIntent, ...]
              ) -> FxIntent | None:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    quote = context.fx.quotes[currency].lot_quote
    fees = context.inputs.fx_fees.get(quote.uid, {})
    size = difference(Decimal(quote.lot_size), fees.get(currency, ZERO)) if side == 'BUY' else (
        Decimal(quote.lot_size))
    if size <= 0:
        return None
    lots = _ceil_lots(exact_sum(needs.values()), size)
    rules = context.inputs.fx_rules.get(quote.uid)
    pair = context.fx.quotes[currency].pair
    permitted = pair.buy_available if side == 'BUY' else pair.sell_available
    available = {'rub': exact_sum(context.cash.amount(path, 'rub') for path in needs),
                 currency: exact_sum(capacities.values())}
    if (rules is None or not pair.api_trade_available or not permitted
            or not quote.can_trade(side, lots, rules, available, ready=True)):
        return None
    if side == 'BUY' and exact_sum((quote.quote_amount(side, lots).amount,
                                   exact_product(fees.get('rub', ZERO), Decimal(lots)))) > (
            rules.buy_money_amount):
        return None
    paths = sorted(needs)
    extra = difference(Decimal(lots * quote.lot_size), exact_sum(needs.values()))
    spare = [max(ZERO, difference(capacities[path], needs[path])) for path in paths]
    if extra > exact_sum(spare):
        return None
    total = Decimal(lots * quote.lot_size)
    if all(exact_product(total, needs[path]) <= exact_product(
            capacities[path], exact_sum(needs.values())) for path in paths):
        parts = proportional_split(total, _relative_weights([needs[path] for path in paths]),
                                   ceilings=[capacities[path] for path in paths])
        contributions = dict(zip(paths, parts))
    else:
        additions = (proportional_split(extra, spare, ceilings=spare)
                     if extra else [ZERO] * len(paths))
        contributions = {path: exact_sum((needs[path], amount))
                         for path, amount in zip(paths, additions)}
    return _exchange_amounts(context, quote, side, contributions, lots, orders)


def _exchange_amounts(context, quote: FxLotQuote, side, contributions, lots, orders):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    weights = [contributions[path] for path in sorted(contributions)]
    debits, credits_by_path, estimated_fees = {}, {}, {}
    for path, rub in zip(sorted(contributions), proportional_split(
            quote.quote_amount(side, lots).amount, weights)):
        debits[path] = ({'rub': rub} if side == 'BUY'
                        else {quote.base_currency: contributions[path]})
        credits_by_path[path] = ({quote.base_currency: contributions[path]} if side == 'BUY'
                                 else {'rub': rub})
    for currency, fee in context.inputs.fx_fees.get(quote.uid, {}).items():
        for path, part in zip(sorted(contributions), proportional_split(
                exact_product(fee, Decimal(lots)), weights)):
            _add(debits, path, currency, part)
            _add(estimated_fees, path, currency, part)
    return FxIntent(quote.uid, side, lots, quote.lot_size, contributions, debits, credits_by_path,
                    tuple(sorted({intent.uid for intent in orders
                                  if set(intent.pieces) & set(contributions)
                                  and context.prices[intent.uid].currency == (
                                      quote.base_currency if side == 'BUY' else 'rub')})),
                    estimated_fees, {
                        path: {currency: exact_sum((context.cash.amount(path, currency),
                                                   credits_by_path[path].get(currency, ZERO)))
                               for currency in money} for path, money in debits.items()})


def _apply_exchange(balances, intent: FxIntent) -> bool:
    for path, money in intent.debits.items():
        if any(amount > exact_sum((balances.get(path, {}).get(currency, ZERO),
                                   intent.credits.get(path, {}).get(currency, ZERO)))
               for currency, amount in money.items()):
            return False
    for path, money in intent.credits.items():
        for currency, amount in money.items():
            _add(balances, path, currency, amount)
    for path, money in intent.debits.items():
        for currency, amount in money.items():
            balances[path][currency] = difference(balances[path].get(currency, ZERO), amount)
    return True


def _forward(context: _Context, principal: CashPools, debits: CashPools, orders,
             balances, exchanges) -> bool:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    currencies = sorted({code for money in debits.balances.values() for code in money} - {'rub'})
    for currency in currencies:
        needs, capacities = {}, {}
        quote = context.fx.quotes[currency].lot_quote
        for path in debits.balances:
            cash = context.cash.amount(path, currency)
            cost = debits.amount(path, currency)
            if cost <= cash:
                continue
            if not principal.amount(path, currency):
                return False
            needs[path] = max(difference(cost, cash), difference(exact_sum((
                principal.amount(path, currency), context.reserves.amount(path, currency))), cash))
            # Original RUB only: reverse FX credits can never finance another currency.
            rub = max(ZERO, difference(balances[path].get('rub', ZERO), debits.amount(path, 'rub')))
            capacities[path] = _base_affordable(quote, rub, context.inputs.fx_fees)
            if needs[path] > capacities[path]:
                cost = exact_sum((quote.quote_amount('BUY', 1).amount,
                                  context.inputs.fx_fees.get(quote.uid, {}).get('rub', ZERO)))
                if exact_product(needs[path], cost) > exact_product(rub, Decimal(quote.lot_size)):
                    return False
                # Correct only our divided allowance using an exact dimensional bound.
                capacities[path] = needs[path]
        if needs:
            intent = _exchange(context, currency, 'BUY', needs, capacities, orders)
            if intent is None or not _apply_exchange(balances, intent):
                return False
            exchanges.append(intent)
    return True


def _base_affordable(quote: FxLotQuote, rub: Decimal, fees) -> Decimal:
    """A combined lot may have fractional owner contributions; debit checks remain exact."""
    cost = exact_sum((quote.quote_amount('BUY', 1).amount,
                      fees.get(quote.uid, {}).get('rub', ZERO)))
    return fx_ratio(exact_product(rub, Decimal(quote.lot_size)), cost)


def _reverse(context: _Context, principal: CashPools, debits: CashPools, orders,
             balances, exchanges) -> bool:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    for currency in sorted(context.fx.quotes):
        quote = context.fx.quotes[currency].lot_quote
        needs, capacities = {}, {}
        for path in debits.balances:
            rub = balances[path].get('rub', ZERO)
            deficit = max(ZERO, difference(debits.amount(path, 'rub'), rub))
            if not deficit:
                continue
            if principal.amount(path, 'rub') <= rub:
                continue  # RUB fees alone never justify a reverse exchange.
            surplus = max(ZERO, difference(context.cash.amount(path, currency), max(
                debits.amount(path, currency), context.native_claims.amount(path, currency))))
            if not surplus:
                continue
            # Bid proceeds, not a midpoint estimate, finance the RUB security.
            fees = context.inputs.fx_fees.get(quote.uid, {})
            net = difference(quote.quote_amount('SELL', 1).amount, fees.get('rub', ZERO))
            if net <= 0:
                continue
            required = fx_ratio(exact_product(deficit, Decimal(quote.lot_size)), net)
            capacity = fx_ratio(exact_product(surplus, Decimal(quote.lot_size)), exact_sum((
                Decimal(quote.lot_size), fees.get(currency, ZERO))))
            needs[path] = min(required, capacity)
            capacities[path] = capacity
        if needs:
            intent = _exchange(context, currency, 'SELL', needs, capacities, orders)
            if intent is None or not _apply_exchange(balances, intent):
                return False
            exchanges.append(intent)
    return True


def _evaluate(context: _Context, orders: tuple[OrderIntent, ...]):
    principal, fees = _purchase_debits(context, orders)
    debits = CashPools(_combined(principal, fees))
    balances = {path: dict(money) for path, money in context.cash.balances.items()}
    exchanges = []
    if not _forward(context, principal, debits, orders, balances, exchanges):
        return None
    if not _reverse(context, principal, debits, orders, balances, exchanges):
        return None
    for path, money in debits.balances.items():
        if any(amount > balances[path].get(currency, ZERO) for currency, amount in money.items()):
            return None
        for currency, amount in money.items():
            balances[path][currency] = difference(balances[path].get(currency, ZERO), amount)
    for path in context.headroom:
        expenses = exact_sum(context.fx.to_rub(MoneyAmount(currency, amount)).amount
                             for currency, amount in debits.balances.get(path, {}).items())
        fx_cost = exact_sum(context.fx.to_rub(MoneyAmount(currency, amount)).amount
                            for intent in exchanges
                            for currency, amount in intent.debits.get(path, {}).items())
        fx_credit = exact_sum(context.fx.to_rub(MoneyAmount(currency, amount)).amount
                              for intent in exchanges
                              for currency, amount in intent.credits.get(path, {}).items())
        if exact_sum((expenses, fx_cost, fx_credit.copy_negate())) > context.headroom[path]:
            return None
    if not _current_caps(context, orders, exchanges):
        return None
    return tuple(exchanges), CashPools(balances, context.cash.protected), debits, fees


def _current_caps(context, orders, exchanges):
    spent = {}
    for intent in orders:
        rule = context.rules[intent.uid]
        price = context.prices[intent.uid]
        amount = exact_product(price.amount, Decimal(intent.lots * intent.lot_size))
        fee = exact_product(context.inputs.fees.get(intent.uid, {}).get(price.currency, ZERO),
                            Decimal(intent.lots))
        spent[price.currency] = exact_sum((spent.get(price.currency, ZERO), amount, fee))
        dependent = any(intent.uid in exchange.dependent_uids for exchange in exchanges)
        if not dependent and (intent.lots > rule.buy_max_lots
                              or spent[price.currency] > rule.buy_money_amount):
            return False
    return True


def _prices(inputs, rules, marks, fx):
    result = {}
    for uid, rule in rules.items():
        if uid not in marks:
            continue
        currency = currency_code(rule.currency)
        price = inputs.native_prices.get(uid)
        if price is None:
            if currency != 'rub':
                raise ValueError('missing native security midpoint')
            price = MoneyAmount('rub', marks[uid])
        if price.currency != currency or fx.to_rub(price).amount != marks[uid]:
            raise ValueError('native security midpoint and RUB mark disagree')
        result[uid] = price
    return result


def _funding_context(strategy, snapshot, ownership, marks, rules, inputs):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    fx = snapshot.fx_snapshot
    if fx is None:
        raise ValueError('funding requires an explicit FX snapshot')
    headroom = cash_headroom(strategy, ownership, marks)
    reserves = reserve_shares(strategy, marks, rules, fx)
    cash = inputs.cash if inputs.cash is not None else initial_cash_pools(
        strategy, ownership, marks, snapshot.available_cash, fx,
        preferences=_cash_preferences(strategy, ownership, marks, rules, reserves, fx))
    if set(cash.balances) - set(headroom):
        raise ValueError('cash pools require leaf owners')
    for currency in {code for money in (*cash.balances.values(), cash.protected) for code in money}:
        if cash.total(currency) > snapshot.available_cash.get(currency, ZERO):
            raise ValueError('cash pools exceed actual availability')
        fx.rate(currency)
    for path, money in cash.balances.items():
        if exact_sum(fx.to_rub(MoneyAmount(code, amount)).amount
                     for code, amount in money.items()) > headroom[path]:
            raise ValueError('owner cash exceeds assigned budget')
    return _Context(fx, inputs, cash, reserves, headroom, _prices(inputs, rules, marks, fx),
                    rules, CashPools({}))


def _cash_preferences(strategy, ownership, marks, rules, reserves, fx):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    result = {path: {code: fx.to_rub(MoneyAmount(code, amount)).amount
                     for code, amount in money.items()}
              for path, money in reserves.balances.items()}
    for path, node in leaves(strategy).items():
        for uid, quantity in node.target.quantities.items():
            if not quantity:
                continue
            gap = max(ZERO, difference(quantity, ownership[path].get(uid, ZERO)))
            _add(result, path, currency_code(rules[uid].currency), exact_product(gap, marks[uid]))
    return CashPools(result)


def build_funding_plan(strategy: StrategyPlan, snapshot: ExecutionSnapshot, ownership,
                       marks, rules, limits, inputs: FundingInputs) -> FundingPlan:
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """Use the existing priority/capacity rule, retaining future-funded demand separately."""
    context = _funding_context(strategy, snapshot, ownership, marks, rules, inputs)
    ownership = {path: dict(values) for path, values in ownership.items()}
    quantities = dict(snapshot.quantities)
    money = dict(limits)
    demands = funding_candidates(strategy, ownership, quantities, marks, rules, money)
    principal, fees = _purchase_debits(context, demands)
    context = _Context(context.fx, inputs, context.cash, context.reserves, context.headroom,
                       context.prices, rules, CashPools(_combined(principal, fees)))
    chosen = ()
    evaluation = ((), context.cash, CashPools({}), CashPools({}))
    if snapshot.limits_ready and not snapshot.active_orders:
        for demand in demands:
            candidates = funding_candidates(strategy, ownership, quantities, marks, rules, money)
            candidate = next((item for item in candidates if item.uid == demand.uid), None)
            if candidate is None:
                continue
            order, result = _fund_candidate(context, chosen, candidate)
            if order is None:
                continue
            chosen += (order,)
            evaluation = result
            for path, pieces in order.pieces.items():
                ownership[path][order.uid] = exact_sum((
                    ownership[path].get(order.uid, ZERO), pieces))
                money[path] = difference(money[path], exact_product(pieces, marks[order.uid]))
            quantities[order.uid] = exact_sum((quantities.get(order.uid, ZERO),
                                               Decimal(order.lots * order.lot_size)))
    exchanges, projected, debits, fees = evaluation
    purchases = tuple(_funded_purchase(context, intent, exchanges) for intent in chosen)
    return FundingPlan(context.cash, limits, context.reserves.balances, demands, purchases,
                       exchanges, projected, debits, fees, context.prices)


def _funded_purchase(context, intent, exchanges):
    price = context.prices[intent.uid]
    dependencies = tuple(exchange.uid for exchange in exchanges
                         if intent.uid in exchange.dependent_uids)
    return FundedPurchase(intent, MoneyAmount(price.currency, exact_product(
        price.amount, Decimal(intent.lots * intent.lot_size))), not dependencies, dependencies)


def _fundable_pieces(context, candidate):
    """An upper bound from owned cash only, before exact FX/fee/lot feasibility checks."""
    price = context.prices[candidate.uid]
    result = {}
    for path, capacity in candidate.pieces.items():
        amount = context.cash.amount(path, price.currency)
        if price.currency != 'rub':
            quote = context.fx.quotes[price.currency].lot_quote
            if quote.uid in context.inputs.fx_rules:
                amount = exact_sum((amount, _base_affordable(
                    quote, context.cash.amount(path, 'rub'), context.inputs.fx_fees)))
        else:
            for currency, observation in context.fx.quotes.items():
                if observation.pair.uid in context.inputs.fx_rules:
                    surplus = max(ZERO, difference(context.cash.amount(path, currency),
                                                   context.native_claims.amount(path, currency)))
                    amount = exact_sum((amount, fx_ratio(exact_product(
                        surplus, observation.bid), observation.pair.nominal)))
        with localcontext(prec=28, rounding=ROUND_FLOOR):
            result[path] = min(capacity, amount / price.amount)
    return result


def _fund_candidate(context, chosen, candidate):
    capacities = _fundable_pieces(context, candidate)
    high = min(candidate.lots, allocate_floor(exact_sum(capacities.values()),
                                              Decimal(candidate.lot_size)))

    def evaluate(lots):
        trial = OrderIntent(candidate.uid, 'BUY', lots, candidate.lot_size,
                            take_pieces(Decimal(lots * candidate.lot_size), capacities))
        return trial, _evaluate(context, chosen + (trial,))

    points = _activation_points(context, chosen, candidate, capacities, high)
    for lower, upper in reversed(tuple(zip(points, points[1:]))):
        order, result = _search_batch(evaluate, lower, upper - 1)
        if order is not None:
            return order, result
    return None, None


def _activation_points(context, chosen, candidate, capacities, maximum):
    """Funding can become possible when an owner joins or native money runs out."""
    principal, fees = _purchase_debits(context, chosen)
    spent = CashPools(_combined(principal, fees))
    points = {1, maximum + 1}
    prefix = ZERO
    for path, pieces in capacities.items():
        for amount in _cash_boundaries(context, candidate, spent, path, prefix):
            start = allocate_floor(amount, Decimal(candidate.lot_size)) + 1
            points.update(point for point in (start - 1, start, start + 1)
                          if 1 <= point <= maximum + 1)
        prefix = exact_sum((prefix, pieces))
    return sorted(points)


def _cash_boundaries(context, candidate, spent, path, prefix):
    price = context.prices[candidate.uid]
    principal = exact_product(price.amount, Decimal(candidate.lot_size))
    costs = dict(context.inputs.fees.get(candidate.uid, {}))
    costs[price.currency] = exact_sum((costs.get(price.currency, ZERO), principal))
    boundaries = [prefix]
    for currency, cost in (*costs.items(), (price.currency, principal)):
        if cost:
            cash = max(ZERO, difference(context.cash.amount(path, currency),
                                        spent.amount(path, currency)))
            boundaries.append(exact_sum((prefix, fx_ratio(exact_product(
                cash, Decimal(candidate.lot_size)), cost))))
    return boundaries


def _search_batch(evaluate, lower, upper):
    trial, evaluated = evaluate(upper)
    if evaluated is not None:
        return trial, evaluated
    low, high = lower - 1, upper - 1
    order, result = None, None
    while low < high:
        lots = (low + high + 1) // 2
        trial, evaluated = evaluate(lots)
        if evaluated is None:
            high = lots - 1
        else:
            low = lots
            order, result = trial, evaluated
    return order, result
