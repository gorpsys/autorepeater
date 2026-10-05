"""SDK submission boundary; BESTPRICE enums are converted only here."""
from uuid import uuid4

from t_tech.invest import OrderDirection, OrderExecutionReportStatus, OrderType
from t_tech.invest.exceptions import RequestError

from autorepeater.execution import ExecutionReceipt, OrderExecutionError
from autorepeater.logging_config import logger as LOGGER
from autorepeater.purchase_plan import validate_intent


class TInvestOrderExecutor:  # pylint: disable=too-few-public-methods
    """Uses the Runner's Client with its installed finite unary interceptor."""

    def __init__(self, client):
        self.client = client

    def submit_order(self, account_id, intent):
        """Convert enums here; BESTPRICE never supplies an execution price."""
        validate_intent(intent)
        try:
            response = self.client.orders.post_order(
                account_id=account_id, instrument_id=intent.uid, quantity=intent.lots,
                direction=(OrderDirection.ORDER_DIRECTION_BUY if intent.side == 'BUY'
                           else OrderDirection.ORDER_DIRECTION_SELL),
                order_type=OrderType.ORDER_TYPE_BESTPRICE, order_id=str(uuid4()))
        except RequestError as error:
            LOGGER.error('Order result unknown: UID %s side %s lots %s',
                         intent.uid, intent.side, intent.lots)
            raise OrderExecutionError('order submission failed; stopping pass') from error
        status = getattr(response, 'execution_report_status', None)
        name = getattr(status, 'name', 'UNSPECIFIED')
        if status == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL:
            name = 'FILL'
        else:
            name = name.removeprefix('EXECUTION_REPORT_STATUS_')
        return ExecutionReceipt(intent.uid, intent.side, getattr(response, 'order_id', None),
                                name, getattr(response, 'lots_requested', None),
                                getattr(response, 'lots_executed', None), {})
