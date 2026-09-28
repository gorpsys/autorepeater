"""Application runner and SDK client wiring."""
import dataclasses

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API

from autorepeater.logging_config import configure_local_logging
from autorepeater.repeater import AutoRepeater


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
        self.src = src
        self.dst = dst
        configure_local_logging()

    def run(self):
        """run mainflow for server variant"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = AutoRepeater(client)
            autorepeater.print_all_portfolio()
            autorepeater.set_debug(self.params.debug)
            autorepeater.set_threshold(self.params.threshold)
            autorepeater.set_reserve(self.params.reserve)
            if self.src and self.dst:
                autorepeater.mainflow(self.src, self.dst)

    def run_sync(self):
        """run one sync for serverless varian"""
        with Client(token=self.token, target=INVEST_GRPC_API) as client:
            autorepeater = AutoRepeater(client)
            autorepeater.set_debug(self.params.debug)
            autorepeater.set_threshold(self.params.threshold)
            autorepeater.set_reserve(self.params.reserve)
            if self.src and self.dst:
                autorepeater.sync_accounts(self.src, self.dst)
