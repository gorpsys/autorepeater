"""Application runner and SDK client wiring."""
import dataclasses

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API

from autorepeater.logging_config import configure_local_logging
from autorepeater.repeater import AutoRepeater
from autorepeater.reporting import print_all_portfolio
from autorepeater.strategies import create_strategy
from autorepeater.tinvest_strategy_data import TInvestStrategyData


@dataclasses.dataclass
class RunnerParams:
    """params for init Runner class"""
    debug: bool
    threshold: float


class Runner:
    """wrapper for launch autorwpeater"""

    def __init__(self,
                 token,
                 prepared_strategy,
                 dst,
                 params=RunnerParams(debug=False,
                                     threshold=None)):
        self.token = token
        self.params = params
        self.strategy = create_strategy(prepared_strategy)
        self.dst = dst
        configure_local_logging()

    def run(self):
        """run mainflow for server variant"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            print_all_portfolio(client)
            autorepeater = self._create_repeater(client, TInvestStrategyData(client))
            if self.dst:
                autorepeater.mainflow(self.dst)

    def run_sync(self):
        """run one sync for serverless varian"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = self._create_repeater(client, TInvestStrategyData(client))
            if self.dst:
                autorepeater.sync_accounts(self.dst)

    def _create_repeater(self, client, data):
        """Apply the same risk parameters in both launch modes."""
        autorepeater = AutoRepeater(client, self.strategy, data)
        autorepeater.set_debug(self.params.debug)
        autorepeater.set_threshold(self.params.threshold)
        return autorepeater
