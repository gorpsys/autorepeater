"""Physical order intent and its exact allocation across owners."""
from dataclasses import dataclass
from decimal import Decimal

from autorepeater.strategy_plan import difference, exact_sum, finite_decimal, validate_path


@dataclass(frozen=True)
class OrderIntent:
    """Only the aggregate is integral; occurrence contributions are pieces."""
    uid: str
    side: str
    lots: int
    lot_size: int
    pieces: dict[tuple[int, ...], Decimal]


def validate_intent(intent: object) -> None:
    """Reject malformed physical orders before crossing the SDK boundary."""
    if not isinstance(intent, OrderIntent) or not isinstance(intent.uid, str) or not intent.uid:
        raise ValueError('intent requires a UID')
    if intent.side not in ('BUY', 'SELL'):
        raise ValueError('intent side must be BUY or SELL')
    for value in (intent.lots, intent.lot_size):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError('intent lots and lot_size must be positive integers')
    if not isinstance(intent.pieces, dict) or not intent.pieces:
        raise ValueError('intent pieces must contain ownership')
    for path, quantity in intent.pieces.items():
        validate_path(path)
        finite_decimal(quantity, 'intent pieces', positive=True)
    if exact_sum(intent.pieces.values()) != Decimal(intent.lots * intent.lot_size):
        raise ValueError('intent ownership sum must equal lots * lot_size')


def take_pieces(amount: Decimal, capacities: dict[tuple[int, ...], Decimal]
                ) -> dict[tuple[int, ...], Decimal]:
    """Fill an aggregate in path order, never exceeding an owner's cap."""
    result = {}
    for path in sorted(capacities):
        quantity = min(amount, capacities[path])
        if quantity:
            result[path] = quantity
            amount = difference(amount, quantity)
    if amount:
        raise ValueError('ownership cannot cover physical lot')
    return result
