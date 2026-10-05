"""SDK submission boundary; BESTPRICE enums are converted only here."""
from uuid import uuid4

from t_tech.invest import OrderDirection, OrderExecutionReportStatus, OrderType
from t_tech.invest.exceptions import RequestError

from autorepeater.execution import ExecutionReceipt, OrderExecutionError
from autorepeater.logging_config import logger as LOGGER
from autorepeater.purchase_plan import validate_intent
from autorepeater.tinvest_requests import call_api


def _receipt(response, uid, side):
    status = getattr(response, 'execution_report_status', None)
    name = getattr(status, 'name', 'UNSPECIFIED').removeprefix('EXECUTION_REPORT_STATUS_')
    if status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL:
        name = 'FILL'
    return ExecutionReceipt(uid, side, getattr(response, 'order_id', None),
                            name, getattr(response, 'lots_requested', None),
                            getattr(response, 'lots_executed', None), {})


class TInvestOrderExecutor:  # pylint: disable=too-few-public-methods
    """Uses the Runner's Client with its installed finite unary interceptor."""

    def __init__(self, client):
        self.client = client

    def submit_order(self, account_id, intent):
        """Convert enums here; BESTPRICE never supplies an execution price."""
        validate_intent(intent)
        try:
            response = call_api(self.client.orders.post_order,
                account_id=account_id, instrument_id=intent.uid, quantity=intent.lots,
                direction=(OrderDirection.ORDER_DIRECTION_BUY if intent.side == 'BUY'
                           else OrderDirection.ORDER_DIRECTION_SELL),
                order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=str(uuid4()))
        except RequestError as error:
            LOGGER.error('Order result unknown: UID %s side %s lots %s',
                         intent.uid, intent.side, intent.lots)
            raise OrderExecutionError('order submission failed; stopping pass') from error
        return _receipt(response, intent.uid, intent.side)

    def get_order_state(self, account_id, order_id):
        """Poll the broker's order identifier without creating another order."""
        try:
            response = call_api(self.client.orders.get_order_state,
                                account_id=account_id, order_id=order_id)
        except RequestError as error:
            LOGGER.error('Sale status read failed: order_id=%s', order_id)
            raise OrderExecutionError('order status read failed; stopping pass') from error
        direction = getattr(response, 'direction', None)
        sides = {OrderDirection.ORDER_DIRECTION_BUY: 'BUY',
                 OrderDirection.ORDER_DIRECTION_SELL: 'SELL'}
        if not isinstance(direction, OrderDirection) or direction not in sides:
            LOGGER.error('Sale status has invalid direction: order_id=%s', order_id)
            raise OrderExecutionError('invalid order direction; stopping pass')
        return _receipt(response, getattr(response, 'instrument_uid', None), sides[direction])
