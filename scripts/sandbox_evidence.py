"""Typed, allowlisted sandbox evidence; dictionaries are only JSON at the boundary."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class MoneyEvidence:
    """Exact decimal amount in the broker's reported currency."""
    currency: str
    amount: str


@dataclass(frozen=True, slots=True)
class QuoteEvidence:
    """Instrument and quote used to construct a live scenario."""
    uid: str
    lot: int
    price: str
    time: str | None


@dataclass(frozen=True, slots=True)
class OrderEvidence:
    """Only returned broker facts, not arbitrary SDK representations."""
    order_id: str
    uid: str
    direction: str
    status: str
    requested: int
    executed: int


@dataclass(frozen=True, slots=True)
class TradeEvidence:
    """One trade from an operation, with exact units/nano price."""
    trade_id: str
    quantity: int
    time: str
    price: MoneyEvidence


@dataclass(frozen=True, slots=True)
class OperationEvidence:  # pylint: disable=too-many-instance-attributes
    """Operation used by independent cash and quantity reconciliation."""
    id: str
    uid: str
    figi: str
    type: str
    state: str
    quantity: int
    remaining: int
    time: str
    payment: MoneyEvidence
    price: MoneyEvidence
    trades: tuple[TradeEvidence, ...]


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    """A freshly read disposable account, suitable for reconstruction."""
    budget: str
    ready: bool
    positions: dict[str, str]
    marks: dict[str, str]
    cash: dict[str, str]
    orders: tuple[OrderEvidence, ...]
    operations: tuple[OperationEvidence, ...]


@dataclass(frozen=True, slots=True)
class IdentifierEvidence:  # pylint: disable=too-many-instance-attributes
    """Identifier probe results for one owned disposable account."""
    open_account_id: str
    owned_name: str
    primary_id: str
    legacy_id: str
    portfolio_account_id: str
    positions_account_id: str
    primary_numeric: bool
    legacy_numeric: bool


def evidence_json(value: object) -> dict[str, object]:
    """Serialize our DTOs only; never fall back to str/repr of arbitrary SDK data."""
    if isinstance(value, (MoneyEvidence, QuoteEvidence, OrderEvidence, TradeEvidence,
                          OperationEvidence, AccountEvidence, IdentifierEvidence)):
        return asdict(value)
    raise TypeError('unsupported sandbox evidence type')
