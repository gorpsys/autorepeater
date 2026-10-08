"""Application runner and SDK client wiring."""
import dataclasses

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.sandbox.client import SandboxClient
from t_tech.invest.services import Services

from autorepeater.logging_config import configure_local_logging
from autorepeater.grpc_deadline import UnaryDeadlineInterceptor
from autorepeater.repeater import AutoRepeater
from autorepeater.reporting import print_all_portfolio
from autorepeater.reporting import print_missing_destination
from autorepeater.strategies import create_strategy
from autorepeater.strategy_contract import PreparedStrategy
from autorepeater.tinvest_strategy_data import TInvestStrategyData
from autorepeater.tinvest_execution_data import TInvestExecutionData
from autorepeater.order_execution import TInvestOrderExecutor


@dataclasses.dataclass
class RunnerParams:
    """params for init Runner class"""
    debug: bool


class Runner:
    """wrapper for launch autorwpeater"""

    def __init__(self,
                 token: str,
                 prepared_strategy: PreparedStrategy,
                 dst: str | None,
                 params: RunnerParams | None = None,
                 *, sandbox: bool = False) -> None:
        if not isinstance(sandbox, bool):
            raise TypeError('sandbox must be bool')
        self.token = token
        self.sandbox = sandbox
        self.params = params if params is not None else RunnerParams(debug=False)
        self.strategy = create_strategy(prepared_strategy)
        self.dst = dst
        configure_local_logging()

    def run(self) -> None:
        """run mainflow for server variant"""
        with self._create_client() as client:
            print_all_portfolio(client)
            autorepeater = self._create_repeater(client)
            if self.dst:
                autorepeater.mainflow(self.dst)
            else:
                print_missing_destination('run')

    def run_sync(self) -> None:
        """Run one finite sync with the explicitly selected SDK endpoint."""
        with self._create_client() as client:
            autorepeater = self._create_repeater(client)
            if self.dst:
                autorepeater.sync_accounts(self.dst)
            else:
                print_missing_destination('run_sync')

    def _create_client(self) -> Client:
        """Keep every service on one endpoint with finite unary deadlines."""
        client_class = SandboxClient if self.sandbox else Client
        target = INVEST_GRPC_API_SANDBOX if self.sandbox else INVEST_GRPC_API
        return client_class(token=self.token, target=target,
                            interceptors=[UnaryDeadlineInterceptor()])

    def _create_repeater(self, client: Services) -> AutoRepeater:
        """Apply the same risk parameters in both launch modes."""
        data = TInvestStrategyData(client)
        autorepeater = AutoRepeater(self.strategy, data, TInvestExecutionData(client, data),
                                    TInvestOrderExecutor(client))
        autorepeater.set_debug(self.params.debug)
        return autorepeater
