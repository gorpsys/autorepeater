"""Compare index allocations on one public market snapshot, without trading."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import os
from pathlib import Path

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX

from autorepeater import reporting
from autorepeater.constants import IMPORTANT
from autorepeater.index_config import select_index_config
from autorepeater.index_strategy import IndexStrategy, calculate_index_target
from autorepeater.strategy_budget import available_budget
from autorepeater.tinvest_strategy_data import TInvestStrategyData


def _decimal(value):
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError('expected a decimal number') from error
    if not result.is_finite():
        raise argparse.ArgumentTypeError('expected a finite decimal number')
    return result


def parse_args(argv=None):
    """Validate comparison inputs before opening an SDK client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', help='name from an index config; required if several exist')
    parser.add_argument('--sandbox', action='store_true',
                        help='use the sandbox API endpoint (default: production)')
    parser.add_argument('--gross-values', nargs='+', type=_decimal,
                        default=list(map(Decimal, ['100000', '154000', '300000'])))
    parser.add_argument('--budgets', nargs='+', type=_decimal, default=[Decimal('154000')])
    parser.add_argument('--thresholds', nargs='+', type=_decimal,
                        default=list(map(Decimal, ['0.03', '0.05', '0.1'])))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if any(value <= 0 for value in args.gross_values + args.budgets):
        parser.error('gross values and budgets must be positive')
    if any(not 0 <= value < 1 for value in args.thresholds):
        parser.error('thresholds must be in [0, 1)')
    return parser, args


def _comparison_rows(calculation, snapshot, budget):
    last = {}
    for number, allocations in enumerate(calculation.passes, 1):
        last.update({ticker: (number, allocation) for ticker, allocation in allocations.items()})
    total = sum(calculation.capitalizations.values(), Decimal(0))
    rows = []
    for rank, ticker in enumerate(
            sorted(calculation.capitalizations,
                   key=lambda item: (-calculation.capitalizations[item], item)), 1):
        quote = snapshot[ticker]
        quantity = calculation.target.quantities.get(quote.uid, Decimal(0))
        full_weight = calculation.capitalizations[ticker] / total
        actual_weight = quantity * quote.price / budget
        number, allocation = last.get(ticker, (None, None))
        rows.append({
            'ticker': ticker, 'rank': rank, 'full_weight': full_weight,
            'full_target_value': budget * full_weight,
            'capitalization': calculation.capitalizations[ticker],
            'allocation_pass': number,
            'last_allocation': asdict(allocation) if allocation is not None else None,
            'exclusion_reason': ('min_position_value' if ticker in calculation.min_exclusions
                                 else allocation.exclusion_reason),
            'final_lots': int(quantity / quote.lot), 'quantity': quantity,
            'actual_weight': actual_weight,
            'full_weight_error': abs(actual_weight - full_weight) / full_weight,
        })
    return rows


def compare_target(config, snapshot, scenario, threshold):
    """Describe final holdings and each ticker's last allocation or exclusion."""
    budget = scenario['budget']
    calculation = calculate_index_target(
        replace(config, max_lot_weight_error=threshold), snapshot, budget)
    target = calculation.target
    rows = _comparison_rows(calculation, snapshot, budget)
    cost = sum((quantity * target.prices[uid]
                for uid, quantity in target.quantities.items()), Decimal(0))
    return {
        **scenario, 'max_lot_weight_error': threshold,
        'min_position_value': config.min_position_value,
        'initial_prefix': [row['ticker'] for row in rows
                           if row['exclusion_reason'] != 'min_position_value'],
        'selected_count': len(target.quantities),
        'selected_ranks': [row['rank'] for row in rows if row['final_lots']],
        'cost': cost, 'cash': budget - cost, 'rows': rows,
    }


def main(argv=None):
    """Read the explicitly supplied token only when invoked; never query accounts."""
    parser, args = parse_args(argv)
    token = os.environ.get('READ_ONLY_INVEST_TOKEN')
    if not token:
        parser.error('READ_ONLY_INVEST_TOKEN is required')
    try:
        config = select_index_config(args.src)
    except ValueError as error:
        parser.error(f'--src must name a configured index strategy: {error}')
    strategy = IndexStrategy(config)
    started = datetime.now(timezone.utc)
    with Client(token, target=(
            INVEST_GRPC_API_SANDBOX if args.sandbox else INVEST_GRPC_API)) as client:
        snapshot = strategy.load_snapshot(TInvestStrategyData(client))
    received = datetime.now(timezone.utc)
    reserve = config.reserve
    scenarios = [
        {'gross_value': value, 'reserve': reserve, 'budget': available_budget(value, reserve)}
        for value in args.gross_values
    ] + [{'gross_value': None, 'reserve': Decimal(0), 'budget': value} for value in args.budgets]
    report = {
        'snapshot_started_at': started, 'snapshot_received_at': received,
        'config': asdict(strategy.config),
        'snapshot': {ticker: asdict(quote) for ticker, quote in snapshot.items()},
        'comparisons': [compare_target(strategy.config, snapshot, scenario, threshold)
                        for scenario in scenarios for threshold in args.thresholds],
    }
    payload = reporting.format_index_calibration(report)
    if args.output is not None:
        args.output.write_text(payload + '\n', encoding='utf-8')
    logging.basicConfig(level=IMPORTANT, format='%(message)s')
    reporting.print_index_calibration(payload)


if __name__ == '__main__':
    main()
