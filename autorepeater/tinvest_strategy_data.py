"""T-Invest read-side adapter for strategy data."""
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal, InvalidOperation

from t_tech.invest import InstrumentIdType, RequestError

from autorepeater.strategy_data import DataAccessError
from autorepeater.strategy_data import InstrumentInfo
from autorepeater.strategy_data import InstrumentMatch
from autorepeater.strategy_data import InstrumentType
from autorepeater.strategy_data import MoneyBlocking
from autorepeater.strategy_data import PortfolioEntry
from autorepeater.strategy_data import PortfolioSnapshot
from autorepeater.strategy_data import PositionEvent
from autorepeater.strategy_data import PriceQuote
from autorepeater.strategy_data import SecurityBlocking


NANO_FACTOR = Decimal('1000000000')


def _instrument_type(value):
    """Map SDK strings to the stable categories exposed to strategies."""
    return {
        'share': InstrumentType.SHARE,
        'etf': InstrumentType.ETF,
        'currency': InstrumentType.CURRENCY,
    }.get(value, InstrumentType.OTHER)


def _decimal_value(value, context):
    """Convert an SDK units/nano value and identify malformed source fields."""
    if value is None:
        raise ValueError(f'{context} is missing')

    units = value.units
    nano = value.nano
    converted = []
    for field_name, part in (('units', units), ('nano', nano)):
        if part is None or isinstance(part, bool):
            raise ValueError(f'{context}.{field_name} is invalid')
        try:
            decimal_part = Decimal(part)
        except (TypeError, ValueError, InvalidOperation) as error:
            raise ValueError(f'{context}.{field_name} is invalid') from error
        if not decimal_part.is_finite():
            raise ValueError(f'{context}.{field_name} is not finite')
        converted.append(decimal_part)

    result = converted[0] + converted[1] / NANO_FACTOR
    if not result.is_finite():
        raise ValueError(f'{context} is not finite')
    return result


def _transport_error(error):
    """Create the SDK-independent transport error while retaining its cause."""
    return DataAccessError(str(error))


class TInvestStrategyData:
    """Translate T-Invest read services into the strategy data contract."""

    def __init__(self, client):
        self._client = client

    def get_portfolio(self, account_id: str) -> PortfolioSnapshot:
        """Return an ordered portfolio snapshot for one account."""
        try:
            response = self._client.operations.get_portfolio(
                account_id=account_id)
        except RequestError as error:
            raise _transport_error(error) from error

        return PortfolioSnapshot(positions=tuple(
            self._portfolio_entry(position) for position in response.positions))

    def find_instruments(self, query: str) -> list[InstrumentMatch]:
        """Return ordered short metadata without loading full instruments."""
        try:
            response = self._client.instruments.find_instrument(query=query)
        except RequestError as error:
            raise _transport_error(error) from error

        return [
            InstrumentMatch(
                uid=instrument.uid,
                ticker=instrument.ticker,
                name=instrument.name,
                instrument_type=_instrument_type(instrument.instrument_type),
                class_code=instrument.class_code,
            )
            for instrument in response.instruments
        ]

    def get_instrument(self, uid: str) -> InstrumentInfo:
        """Return full metadata for one UID."""
        try:
            response = self._client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=uid,
            )
        except RequestError as error:
            raise _transport_error(error) from error

        instrument = response.instrument
        if instrument is None:
            raise ValueError(f'instrument {uid}: instrument is missing')
        return InstrumentInfo(
            uid=instrument.uid,
            ticker=instrument.ticker,
            name=instrument.name,
            instrument_type=_instrument_type(instrument.instrument_type),
            class_code=instrument.class_code,
            lot=instrument.lot,
            currency=instrument.currency,
        )

    def get_last_prices(self, uids: Sequence[str]) -> list[PriceQuote]:
        """Return SDK quote records in their original order."""
        try:
            response = self._client.market_data.get_last_prices(
                instrument_id=list(uids))
        except RequestError as error:
            raise _transport_error(error) from error

        result = []
        for quotation in response.last_prices:
            uid = quotation.instrument_uid
            if quotation.price is None:
                raise ValueError(f'instrument {uid}: price is missing')
            result.append(PriceQuote(
                uid=uid,
                price=_decimal_value(
                    quotation.price, f'instrument {uid} price'),
                time=quotation.time,
            ))
        return result

    def position_events(
            self, account_ids: Sequence[str]) -> Iterable[PositionEvent]:
        """Open a lazy stream and translate transport failures at either stage."""
        try:
            responses = self._client.operations_stream.positions_stream(
                accounts=list(account_ids))
            iterator = iter(responses)
        except RequestError as error:
            raise _transport_error(error) from error
        return self._position_events(iterator)

    @staticmethod
    def _portfolio_entry(position):
        uid = position.instrument_uid
        return PortfolioEntry(
            uid=uid,
            instrument_type=_instrument_type(position.instrument_type),
            current_price=_decimal_value(
                position.current_price,
                f'portfolio position {uid} current_price'),
            currency=position.current_price.currency,
            quantity=_decimal_value(
                position.quantity,
                f'portfolio position {uid} quantity'),
            diagnostic_text=str(position),
        )

    @classmethod
    def _position_event(cls, response):
        position = response.position
        if position is None:
            return PositionEvent(
                has_position=False,
                account_id='',
                securities=(),
                money=(),
                diagnostic_text=str(response),
            )

        account_id = position.account_id
        return PositionEvent(
            has_position=True,
            account_id=account_id,
            securities=tuple(
                SecurityBlocking(blocked=item.blocked)
                for item in position.securities),
            money=tuple(
                MoneyBlocking(blocked_value=_decimal_value(
                    item.blocked_value,
                    f'position event {account_id} money[{index}].blocked_value'))
                for index, item in enumerate(position.money)),
            diagnostic_text=str(response),
        )

    @classmethod
    def _position_events(cls, iterator: Iterator) -> Iterator[PositionEvent]:
        while True:
            try:
                response = next(iterator)
            except StopIteration:
                return
            except RequestError as error:
                raise _transport_error(error) from error
            yield cls._position_event(response)
