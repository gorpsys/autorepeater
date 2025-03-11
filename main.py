"""main for start server variant"""

import os
import argparse

from autorepeater.autorepeater import RunnerParams
from autorepeater.autorepeater import Runner

def main():
    """main function"""
    parser = argparse.ArgumentParser(description="autorepeater")

    parser.add_argument("--debug", action='store_true', help="режим отладки")
    parser.add_argument("-s", "--src", type=str, help="id счёта источника")
    parser.add_argument("-d", "--dst", type=str, help="id счёта назначения")
    parser.add_argument("-t", "--threshold", type=float, help="порог стоимости, ниже "
                        "которого не выполняется синхронизация - доля стоимости счёта"
                        " назначения. По умолчанию 0.001")
    parser.add_argument("-r", "--reserve", type=float, help="резев на счёте назначения"
                        " для округлений и комиссий. Доля стоимости счёта назначения. "
                        "По умолчания 0.005")
    args = parser.parse_args()

    invest_token = os.environ["INVEST_TOKEN"]

    runer = Runner(
        token=invest_token,
        src=args.src,
        dst=args.dst,
        params=RunnerParams(
            debug=args.debug,
            threshold=args.threshold,
            reserve=args.reserve))
    runer.run()

if __name__ == "__main__":
    main()
