"""Reproducible offline policy acceptance; public quotes and synthetic accounts only.

Run from the repository with python -m scripts.check_rebalance_policy.
No SDK, environment credentials, network, or real order submission is used.
"""
import argparse
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random

from autorepeater.account_config import AccountConfig
from autorepeater.account_strategy import AccountStrategy, PreparedAccountSource
from autorepeater.composite_strategy import (
    CompositeSnapshot, CompositeStrategy, PreparedCompositeComponent, PreparedCompositeSource,
)
from autorepeater.execution import ExecutionReceipt, execute_plan
from autorepeater.execution_data import ExecutionSnapshot, TradeRules
from autorepeater.index_config import (
    AllocationDriftRange, IndexConfig, IndexInstrument, load_index_config,
)
from autorepeater.index_strategy import IndexQuote, IndexStrategy
from autorepeater.order_plan import build_order_plan
from autorepeater.portfolio import TargetPortfolio
from autorepeater.rebalance_policy import allocation_drift
from autorepeater.repeater import destination_positions, plan_ownership
from autorepeater.strategy_allocation import build_marks, recovered_capital
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.strategy_data import InstrumentType, PortfolioEntry, PortfolioSnapshot
from autorepeater.strategy_plan import StrategyContext, TradeMode, exact_sum


ROOT = Path(__file__).resolve().parents[1]
D = Decimal
ZERO = D(0)


def load_inputs(capture_path=None):
    """Verify frozen capture bytes before decoding its public market data."""
    manifest = json.loads((ROOT / 'test/data/rebalance_scenarios.json').read_text(encoding='utf-8'))
    path = capture_path or ROOT / 'test/data' / manifest['capture']
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest['capture_sha256']:
        raise ValueError('capture SHA-256 differs from the frozen scenario manifest')
    return manifest, json.loads(payload)


def entry(uid, quantity, price):
    """Anonymous security, valued in RUB per piece."""
    return PortfolioEntry(uid, InstrumentType.SHARE, 'rub', price, quantity, '')


def account_strategy(reserve, limit):
    """A model config has a dummy source ID and is never used for data access."""
    config = AccountConfig('MODEL', '00000', reserve, limit)
    return AccountStrategy(PreparedAccountSource('00000', config))


def account_snapshot(quantities, prices):
    """Use the source's established per-position nano arithmetic."""
    positions = {uid: entry(uid, quantity, prices[uid]) for uid, quantity in quantities.items()}
    total = sum((item.quantity * item.current_price).quantize(D('1e-9'))
                for item in positions.values())
    return positions, total


def legacy_orders(current, target, prices, lots, budget):
    """Frozen offline oracle: nearest lot; price * lots; strict old volume gate."""
    orders = []
    volumes = {'SELL': ZERO, 'BUY': ZERO}
    for uid in sorted(current.keys() | target.keys()):
        delta = target.get(uid, ZERO) - current.get(uid, ZERO)
        count = round(abs(delta) / lots[uid])
        if count:
            side = 'BUY' if delta > 0 else 'SELL'
            orders.append((uid, side, count, lots[uid]))
            volumes[side] += prices[uid] * count
    return orders, max(volumes.values()) > budget * D('0.004')


def metrics(orders, prices, rate):
    """Actual piece turnover at fixed marks; commission is an explicit estimate."""
    turnover = sum((prices[uid] * count * lot for uid, _, count, lot in orders), ZERO)
    return {'turnover': turnover, 'order_count': len(orders),
            'estimated_commission': turnover * rate}


def _account_cases(manifest, capture, repetitions):  # pylint: disable=too-many-locals
    """Independent seed reproduces historical holdout, using captured base weights."""
    rng = random.Random(manifest['validation_seed'])
    ranked = sorted(capture['config']['instruments'],
                    key=lambda item: -D(item['reference_index_capitalization']))
    baskets = [ranked[:12], ranked, ranked]
    strategy = account_strategy(D(manifest['reserve']), D(manifest['account_limit']))
    for basket_index, basket in enumerate(baskets):
        weights = {item['ticker']: D(1) if basket_index == 2
                   else D(item['reference_index_capitalization']) for item in basket}
        weight_sum = sum(weights.values())
        for nominal in manifest['budgets']:
            for amplitude in manifest['amplitudes_per_mille']:
                for _ in range(repetitions):
                    prices = {uid: D(capture['snapshot'][uid]['price'])
                              * (1 + D(rng.randint(*manifest['price_change_per_mille'])) / 1000)
                              for uid in weights}
                    lots = {uid: capture['snapshot'][uid]['lot'] for uid in weights}
                    source = {uid: D(max(1, round(D(10000000) * weight / weight_sum
                                                / prices[uid] / lots[uid]))) * lots[uid]
                              for uid, weight in weights.items()}
                    initial = strategy.build_target(account_snapshot(source, prices), D(nominal))
                    current = {uid: D(round(initial.quantities[uid] / lots[uid])) * lots[uid]
                               for uid in weights}
                    source = {uid: D(max(1, round(
                        quantity / lots[uid]
                        * (1 + D(rng.randint(-amplitude, amplitude)) / 1000))))
                              * lots[uid] for uid, quantity in source.items()}
                    snapshot = account_snapshot(source, prices)
                    invested = sum((quantity * prices[uid]
                                    for uid, quantity in current.items()), ZERO)
                    budget = invested / (1 - strategy.config.reserve)
                    target = strategy.build_target(snapshot, budget)
                    control = strategy.build_target(snapshot, invested)
                    positions = tuple(entry(uid, quantity, prices[uid])
                                      for uid, quantity in current.items())
                    drift = allocation_drift(positions, control, prices)
                    yield current, target.quantities, prices, lots, budget, drift


def account_validation(manifest, capture, repetitions=None):  # pylint: disable=too-many-locals
    """Assess the accepted limit without training or selecting another threshold.

    Turnover here compares conditional lot intentions, not funded real fills.
    The separate sequential index replay uses the actual bounded executor.
    """
    counts = dict.fromkeys(('tp', 'tn', 'fp', 'fn'), 0)
    totals = {name: metrics([], {}, ZERO) for name in ('old', 'new')}
    tracking = {'old': ZERO, 'new': ZERO}
    count = 0
    limit, rate = D(manifest['account_limit']), D(manifest['commission_rate'])
    for current, target, prices, lots, budget, drift in _account_cases(
            manifest, capture, repetitions or manifest['repetitions']):
        orders, gate = legacy_orders(current, target, prices, lots, budget)
        sellable = any(side == 'SELL' for _, side, _, _ in orders)
        old, new = sellable and gate, sellable and drift > limit
        counts['tp' if old and new else 'fn' if old else 'fp' if new else 'tn'] += 1
        for name, permitted in (('old', gate), ('new', new)):
            selected = [order for order in orders
                        if permitted or name == 'new' and order[1] == 'BUY']
            result = metrics(selected, prices, rate)
            for key, value in result.items():
                totals[name][key] += value
            # Half-L1 against the main goal after conditional marked lot changes.
            held = dict(current)
            for uid, side, number, lot in selected:
                held[uid] += D(number * lot) * (1 if side == 'BUY' else -1)
            target_value = sum((quantity * prices[uid] for uid, quantity in target.items()), ZERO)
            held_value = sum((quantity * prices[uid] for uid, quantity in held.items()), ZERO)
            tracking[name] += sum((abs(held[uid] * prices[uid] / held_value
                                       - target[uid] * prices[uid] / target_value)
                                   for uid in target), ZERO) / 2
        count += 1
    for name in totals:
        totals[name]['mean_tracking_error'] = tracking[name] / count
    return {'count': count, 'counts': counts, 'limit': str(limit),
            'agreement': str(D(counts['tp'] + counts['tn']) / count),
            'model_accounts': True, 'real_account_snapshot_available': False,
            'public_quote_count': len(capture['snapshot']),
            'metrics_basis': 'conditional nearest-lot intentions; not funded executions',
            **totals}


def context(strategy, snapshot, budget, positions=()):
    """Neutral API input; one mark per UID, no inferred parent force at root."""
    profile = strategy.allocation_profile(snapshot)
    marks = build_marks(positions, (profile,))
    return StrategyContext((), budget, recovered_capital(positions, profile, marks),
                           False, positions, marks)


class ModelExecution:
    """Offline marked fills for sequential acceptance; never models real settlement."""

    def __init__(self, quantities, prices, lots, cash, rate=ZERO):
        self.quantities = dict(quantities)
        self.prices, self.lots, self.cash, self.rate = prices, lots, cash, rate
        self.orders = []

    def get_destination(self, _account_id):
        """Ready model state; money and quotes are deterministic and unblocked."""
        positions = tuple(entry(uid, quantity, self.prices[uid])
                          for uid, quantity in self.quantities.items() if quantity)
        value = exact_sum(item.quantity * item.current_price for item in positions)
        return ExecutionSnapshot(PortfolioSnapshot(positions), exact_sum((value, self.cash)),
                                 dict(self.quantities), dict(self.prices), {'rub': self.cash},
                                 dict(self.quantities), (), True)

    def get_trade_rules(self, _account_id, uids):
        """Own money caps bound every purchase; the model is explicitly RUB-only."""
        return {uid: TradeRules(self.lots[uid], 'rub', True, True, self.cash,
                                10**9, 10**9) for uid in uids}

    def submit_order(self, _account_id, intent):
        """Apply a fully filled mark-price model order and its estimated commission."""
        quantity = D(intent.lots * intent.lot_size)
        value = quantity * self.prices[intent.uid]
        sign = 1 if intent.side == 'BUY' else -1
        self.quantities[intent.uid] = self.quantities.get(intent.uid, ZERO) + sign * quantity
        self.cash -= sign * value + value * self.rate
        self.orders.append((intent.uid, intent.side, intent.lots, intent.lot_size))
        return ExecutionReceipt(intent.uid, intent.side, str(len(self.orders)), 'FILL',
                                intent.lots, intent.lots,
                                {'rub': value * (1 - self.rate)} if intent.side == 'SELL' else {})


def model_plan(strategy, snapshot, model):
    """Build a real policy and bounded lot plan on an offline model state."""
    state = model.get_destination('MODEL')
    positions = destination_positions(state)
    profile = strategy.allocation_profile(snapshot)
    marks = build_marks(positions, (profile,))
    recognized = tuple(item for item in positions if profile.exposures.get(item.uid, ZERO) > 0)
    unassigned = {item.uid: item.quantity for item in positions
                  if profile.exposures.get(item.uid, ZERO) == 0}
    inputs = StrategyContext((), state.budget, recovered_capital(recognized, profile, marks),
                             False, recognized, marks)
    plan = strategy.build_plan(snapshot, inputs)
    plan = replace(plan, unassigned={**plan.unassigned, **unassigned})
    ownership = plan_ownership(plan)
    rules = model.get_trade_rules('MODEL', sorted(inputs.marks))
    return build_order_plan(plan, state, ownership, inputs.marks, rules)


def _index_replay(strategy, snapshot, budget, manifest, rng):  # pylint: disable=too-many-locals
    """Monthly deposits and random price changes use actual bounded plan execution."""
    prices = {quote.uid: quote.price for quote in snapshot.values()}
    lots = {quote.uid: quote.lot for quote in snapshot.values()}
    target = strategy.build_target(snapshot, budget)
    cost = sum((quantity * prices[uid] for uid, quantity in target.quantities.items()), ZERO)
    model = ModelExecution(target.quantities, prices, lots, budget - cost,
                           D(manifest['commission_rate']))
    if not target.quantities:
        try:
            model_plan(strategy, snapshot, model)
        except ValueError as error:
            return {'budget': budget, 'main_error': str(error), 'months': 0,
                    'extrapolated': True, 'off_grid': budget % 1000 != 0}
    first = model_plan(strategy, snapshot, model)
    repeated = model_plan(strategy, snapshot, model)
    for _ in range(manifest['months']):
        model.cash += D(manifest['monthly_deposit'])
        execute_plan('MODEL', model_plan(strategy, snapshot, model), model, model)
    tail_added = bool(set(model.quantities) - set(target.quantities))
    after_trade = model_plan(strategy, snapshot, model)
    monthly_metrics = metrics(model.orders, prices, model.rate)
    monthly_metrics['tracking_error'] = allocation_drift(
        model.get_destination('MODEL').portfolio.positions,
        after_trade.strategy.target, prices)  # pylint: disable=no-member
    repeat_orders = len(after_trade.sells) + len(after_trade.buys)
    changed = {ticker: replace(quote, price=quote.price * (1 + D(rng.randint(-100, 100)) / 1000))
               for ticker, quote in snapshot.items()}
    model.prices = {quote.uid: quote.price for quote in changed.values()}
    price_plan = model_plan(strategy, changed, model)
    return {'budget': budget, 'repeat_equal': first == repeated,
            'post_trade_repeat_orders': repeat_orders,
            'monthly_deposit': D(manifest['monthly_deposit']), 'months': manifest['months'],
            'cash_nonnegative': model.cash >= 0, 'tail_added': tail_added,
            'price_sales': len(price_plan.sells),
            'price_metric': price_plan.strategy.decision.metric,  # pylint: disable=no-member
            'extrapolated': budget < 50000 or budget >= 1000000,
            'off_grid': budget % 1000 != 0, 'monthly_metrics': monthly_metrics}


def index_acceptance(manifest, capture):
    """Saved public quotes exercise real IMOEX plans at frozen/off-grid budgets."""
    strategy = IndexStrategy(load_index_config(ROOT / 'autorepeater/configs/imoex.json'))
    snapshot = {ticker: IndexQuote(ticker, D(raw['price']), raw['lot'], 'rub')
                for ticker, raw in capture['snapshot'].items()}
    rng = random.Random(manifest['price_seed'])
    return [_index_replay(strategy, snapshot, D(budget), manifest, rng)
            for budget in manifest['index_budgets']]


def table_acceptance(_capture):
    """Every builtin covers zero/null; IMOEX preserves original limits times 1.1."""
    history = json.loads((ROOT / 'docs/plans/20261002-imoex-drift-envelopes-table.json')
                         .read_text(encoding='utf-8'))
    result = {}
    for path in sorted((ROOT / 'autorepeater/configs').glob('*.json')):
        config = load_index_config(path)
        ranges = config.allocation_drift_limits
        boundaries = all(left.budget_to == right.budget_from and not left.upper_inclusive
                         for left, right in zip(ranges, ranges[1:]))
        # Exercise the actual strategy interval selector at every exact boundary.
        snapshot = {item.ticker: IndexQuote(item.ticker, item.reference_price, 1, 'rub')
                    for item in config.instruments}
        strategy = IndexStrategy(config)
        for row in ranges:
            budget = max(D(50000), row.budget_from)
            plan = strategy.build_plan(snapshot, context(strategy, snapshot, budget))
            assert plan.decision.limit == row.limit
        result[config.name] = {'ranges': len(ranges), 'starts_at_zero': ranges[0].budget_from == 0,
                               'ends_at_null': ranges[-1].budget_to is None,
                               'boundaries_checked': boundaries}
        if config.name == 'IMOEX':
            result[config.name]['historical_limits_equal'] = [row.limit for row in ranges] == [
                D(row['max_deviation']) * D('1.1') for row in history['ranges']]
    return result


def model_leaf(uids=('a',), reserve='0', limit='0'):
    """Small explicit models isolate reserve, drift and attribution semantics."""
    config = IndexConfig('MODEL', D('.05'), [
        IndexInstrument(uid, D(1), D(1), D(1), D(10), D(100) / len(uids), D(100))
        for uid in uids], D(reserve), (AllocationDriftRange(ZERO, None, False, D(limit)),))
    return IndexStrategy(config), {uid: IndexQuote(uid, D(10), 1, 'rub') for uid in uids}


def compose(children, weights):
    """Prepared opaque children follow production factories, without a registry mutation."""
    components = tuple(PreparedCompositeComponent(
        'MODEL', str(index), D(weight), PreparedStrategy(lambda child: child, child, 'MODEL'))
        for index, (child, weight) in enumerate(zip(children, weights)))
    return CompositeStrategy(PreparedCompositeSource('MODEL', components, D('.2')))


def protected_shared_sales(plan):
    """Seven and a half permitted pieces cannot sell a protected owner's lot."""
    children = tuple(replace(child,
                             target=TargetPortfolio({uid: D(1)}, {uid: D(10)}),
                             decision=replace(child.decision,
                                              mode=mode, redistribution_allowed=False))
                     for child, uid, mode in zip(plan.children, ('a', 'b'),
                                                (TradeMode.REBALANCE, TradeMode.BUY_ONLY)))
    plan = replace(plan, children=children,
                   target=TargetPortfolio({'a': D(1), 'b': D(1)}, {'a': D(10), 'b': D(10)}))
    model = ModelExecution({'x': D(10)}, {'x': D(10), 'a': D(10), 'b': D(10)},
                           {'x': 10, 'a': 1, 'b': 1}, ZERO)
    ownership = {child.path: {item.uid: item.quantity for item in child.positions}
                 for child in children}
    result = build_order_plan(plan, model.get_destination('MODEL'), ownership, model.prices,
                              model.get_trade_rules('MODEL', model.prices))
    return len(result.sells)


def composite_acceptance():  # pylint: disable=too-many-locals
    """Explicit model scenarios; attribution is an estimate, not historical ownership."""
    first, snap_a = model_leaf(('a',))
    second, snap_b = model_leaf(('b',))
    root, snapshot = compose((first, second), ('.6', '.3')), CompositeSnapshot((snap_a, snap_b))
    plans = [root.build_plan(snapshot, context(root, snapshot, D(100000), (
        entry('a', D(left), D(10)), entry('b', D(right), D(10)))))
        for left, right in ((8000, 2000), (6700, 3300))]
    reserve_leaf, reserve_snapshot = model_leaf(reserve='.006')
    reserve_snapshot['a'].price = D('10.5')
    model = ModelExecution({'a': D(9940)}, {'a': D('10.5')}, {'a': 1}, D(600))
    reserve_sales = 0
    for _ in range(3):
        plan = model_plan(reserve_leaf, reserve_snapshot, model)
        reserve_sales += len(plan.sells)
        execute_plan('MODEL', plan, model, model)
    model.cash += D(2000)
    execute_plan('MODEL', model_plan(reserve_leaf, reserve_snapshot, model), model, model)
    funded_plan = model_plan(reserve_leaf, reserve_snapshot, model)
    floor = funded_plan.strategy.cash_floor  # pylint: disable=no-member
    child, child_snapshot = model_leaf(reserve='.1')
    inner = compose((child, child), ('.6', '.3'))
    nested = compose((inner, child), ('.8', '.1'))
    nested_snapshot = CompositeSnapshot((CompositeSnapshot((child_snapshot, child_snapshot)),
                                         child_snapshot))
    nested_plan = nested.build_plan(nested_snapshot, context(nested, nested_snapshot, D(1000)))
    unassigned_model = ModelExecution({'unknown': D(100)},
                                     {'a': D(10), 'unknown': D(10)}, {'a': 1, 'unknown': 1}, ZERO)
    unfunded = model_plan(first, snap_a, unassigned_model)
    owner_a, owner_snapshot_a = model_leaf(('x', 'a'), limit='.99')
    owner_b, owner_snapshot_b = model_leaf(('x', 'b'), limit='.99')
    # X exposures 1/2 and 1/4 give claims .30/.10 at parent weights .6/.4.
    owner_b.config.instruments[1] = replace(owner_b.config.instruments[1],
                                           reference_index_capitalization=D(300))
    shared = compose((owner_a, owner_b), ('.6', '.4'))
    shared_snapshot = CompositeSnapshot((owner_snapshot_a, owner_snapshot_b))
    positions = (entry('x', D(10), D(10)),)
    shared_plan = shared.build_plan(
        shared_snapshot, context(shared, shared_snapshot, D(1000), positions))
    pieces = [next(item.quantity for item in child_plan.positions if item.uid == 'x')
              for child_plan in shared_plan.children]
    owner_snapshot_b['b'].price = D(20)
    changed = shared.build_plan(
        shared_snapshot, context(shared, shared_snapshot, D(1000), positions))
    return {'drift_budgets': [str(child.budget.quantize(D(1))) for child in plans[0].children],
            'capital_shortage_budgets': [str(child.budget.quantize(D(1)))
                                        for child in plans[1].children],
            'reserve_only_repeated_sales': reserve_sales,
            'reserve_restored_with_deposit': model.cash >= floor,
            'reserve_after_deposit_cash': model.cash,
            'reserve_after_deposit_floor': floor,
            'unassigned_unfunded_buys': len(unfunded.buys),
            'nested_cash_floor': nested_plan.cash_floor, 'shared_pieces': pieces,
            'protected_owner_sales': protected_shared_sales(shared_plan),
            'shared_component_metric': shared_plan.decision.metric,
            'shared_child_metrics': [child.decision.metric for child in shared_plan.children],
            'shared_price_reattribution_changes': (
                shared_plan.children[0].positions != changed.children[0].positions),
            'ownership_history_unknown': True}


def make_report(repetitions=None):
    """Deterministic report with hashes of inputs and current calculation modules."""
    manifest, capture = load_inputs()
    paths = [ROOT / 'test/data/rebalance_scenarios.json', ROOT / 'test/data/imoex_snapshot.json',
             ROOT / 'docs/plans/20261002-imoex-drift-envelopes-table.json',
             ROOT / 'scripts/check_rebalance_policy.py']
    paths.extend(sorted((ROOT / 'autorepeater').glob('*.py')))
    paths.extend(sorted((ROOT / 'autorepeater/configs').rglob('*.json')))
    return {'scope': 'offline local acceptance; synthetic accounts; no live sandbox or GitHub CI',
            'assumptions': 'fixed mark fills, RUB, estimated commission; no annual saving forecast',
            'seeds': {'account': manifest['validation_seed'], 'prices': manifest['price_seed']},
            'hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in paths},
            'account': account_validation(manifest, capture, repetitions),
            'index': index_acceptance(manifest, capture), 'tables': table_acceptance(capture),
            'composite': composite_acceptance()}


def main(argv=None):
    """Print JSON; an explicit output path writes a new report, never historical evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.repetitions is not None and args.repetitions <= 0:
        parser.error('repetitions must be positive')
    payload = json.dumps(make_report(args.repetitions), default=str,
                         indent=2, sort_keys=True) + '\n'
    if args.output is not None:
        args.output.write_text(payload, encoding='utf-8')
    print(payload, end='')


if __name__ == '__main__':
    main()
