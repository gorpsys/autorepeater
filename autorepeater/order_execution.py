"""SDK submission boundary; BESTPRICE enums are converted only here."""
from collections.abc import Callable
from uuid import UUID, uuid4

from t_tech.invest import (
    OrderDirection, OrderExecutionReportStatus, OrderState, OrderType, PostOrderResponse,
)
from t_tech.invest.exceptions import RequestError
from t_tech.invest.services import Services

from autorepeater.execution import ExecutionReceipt, OrderExecutionError
from autorepeater.logging_config import logger as LOGGER
from autorepeater.order_plan import OrderIntent
from autorepeater.purchase_plan import validate_intent
from autorepeater.tinvest_requests import call_api


def _receipt(response: PostOrderResponse | OrderState, *,
             request_id: str | None = None) -> ExecutionReceipt:
    direction = getattr(response, 'direction', None)
    sides = {OrderDirection.ORDER_DIRECTION_BUY: 'BUY',
             OrderDirection.ORDER_DIRECTION_SELL: 'SELL'}
    if not isinstance(direction, OrderDirection) or direction not in sides:
        raise OrderExecutionError('invalid order direction; stopping pass', request_id=request_id)
    status = getattr(response, 'execution_report_status', None)
    statuses = {
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL: 'FILL',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: 'REJECTED',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: 'CANCELLED',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW: 'NEW',
        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL: 'PARTIALLYFILL',
    }
    if not isinstance(status, OrderExecutionReportStatus) or status not in statuses:
        raise OrderExecutionError('invalid order status; stopping pass', request_id=request_id)
    uid = getattr(response, 'instrument_uid', None)
    order_id = getattr(response, 'order_id', None)
    if not isinstance(uid, str) or not uid or not isinstance(order_id, str) or not order_id:
        raise OrderExecutionError('invalid order identity; stopping pass', request_id=request_id)
    requested = getattr(response, 'lots_requested', None)
    executed = getattr(response, 'lots_executed', None)
    if (any(isinstance(value, bool) or not isinstance(value, int)
            for value in (requested, executed))
            or not 0 <= executed <= requested):
        raise OrderExecutionError('invalid order lot counts; stopping pass', request_id=request_id)
    return ExecutionReceipt(uid, sides[direction], order_id, statuses[status],
                            requested, executed, {})


class TInvestOrderExecutor:  # pylint: disable=too-few-public-methods
    """Uses the Runner's Client with its installed finite unary interceptor."""

    def __init__(self, client: Services, *,
                 order_id_factory: Callable[[], UUID | str] | None = None) -> None:
        self.client = client
        self.order_id_factory = uuid4 if order_id_factory is None else order_id_factory

    def submit_order(self, account_id: str, intent: OrderIntent) -> ExecutionReceipt:
        """Convert enums here; BESTPRICE never supplies an execution price."""
        validate_intent(intent)
        request_id = str(self.order_id_factory())
        try:
            response = call_api(
                self.client.orders.post_order,
                account_id=account_id, instrument_id=intent.uid, quantity=intent.lots,
                direction=(OrderDirection.ORDER_DIRECTION_BUY if intent.side == 'BUY'
                           else OrderDirection.ORDER_DIRECTION_SELL),
                order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=request_id)
        except RequestError as error:
            LOGGER.error('Order result unknown: request_id=%s uid=%s side=%s lots=%d '
                         'reason=transport failure',
                         request_id, intent.uid, intent.side, intent.lots)
            raise OrderExecutionError('order submission failed; stopping pass',
                                      request_id=request_id) from error
        try:
            receipt = _receipt(response, request_id=request_id)
        except OrderExecutionError:
            LOGGER.error('Order result unknown: request_id=%s uid=%s side=%s lots=%d '
                         'reason=malformed response',
                         request_id, intent.uid, intent.side, intent.lots)
            raise
        LOGGER.info('Order response: request_id=%s broker_order_id=%s uid=%s side=%s '
                    'status=%s requested=%d executed=%d', request_id, receipt.order_id,
                    receipt.uid, receipt.side, receipt.status,
                    receipt.lots_requested, receipt.lots_executed)
        return receipt

    def get_order_state(self, account_id: str, order_id: str) -> ExecutionReceipt:
        """Poll the broker's order identifier without creating another order."""
        try:
            response = call_api(self.client.orders.get_order_state,
                                account_id=account_id, order_id=order_id)
        except RequestError as error:
            LOGGER.error('Sale status read failed: order_id=%s', order_id)
            raise OrderExecutionError('order status read failed; stopping pass') from error
        try:
            return _receipt(response)
        except OrderExecutionError:
            LOGGER.error('Sale status read failed: order_id=%s reason=malformed response', order_id)
            raise
