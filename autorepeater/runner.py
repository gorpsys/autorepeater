"""Application runner and SDK client wiring."""
import dataclasses

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API

from autorepeater.logging_config import configure_local_logging
from autorepeater.repeater import AutoRepeater
from autorepeater.reporting import print_all_portfolio
from autorepeater.strategies import create_strategy


@dataclasses.dataclass
class RunnerParams:
    """params for init Runner class"""
    debug: bool
    threshold: float
    reserve: float


class Runner:
    """wrapper for launch autorwpeater"""

    def __init__(self,
                 token,
                 src,
                 dst,
                 params=RunnerParams(debug=False,
                                     threshold=None,
                                     reserve=None)):
        self.token = token
        self.params = params
        self.strategy = create_strategy(src)
        self.dst = dst
        configure_local_logging()

    def run(self):
        """run mainflow for server variant"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            print_all_portfolio(client)
            autorepeater = self._create_repeater(client)
            if self.dst:
                autorepeater.mainflow(self.dst)

    def run_sync(self):
        """run one sync for serverless varian"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = self._create_repeater(client)
            if self.dst:
                autorepeater.sync_accounts(self.dst)

    def _create_repeater(self, client):
        """Apply the same risk parameters in both launch modes."""
        autorepeater = AutoRepeater(client, self.strategy)
        autorepeater.set_debug(self.params.debug)
        autorepeater.set_threshold(self.params.threshold)
        autorepeater.set_reserve(self.params.reserve)
        return autorepeater
