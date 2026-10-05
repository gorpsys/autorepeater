"""main for start server variant"""

import os
import argparse

from autorepeater.runner import RunnerParams
from autorepeater.runner import Runner
from autorepeater.strategies import prepare_strategy

def main():
    """main function"""
    parser = argparse.ArgumentParser(description="autorepeater")

    parser.add_argument("--debug", action='store_true', help="режим отладки")
    parser.add_argument("--algoritm", required=True, help="имя алгоритма")
    parser.add_argument("-s", "--src", type=str, required=True,
                        help="источник выбранного алгоритма")
    parser.add_argument("-d", "--dst", type=str, help="id счёта назначения")
    args = parser.parse_args()

    prepared = prepare_strategy(args.algoritm, args.src)
    invest_token = os.environ["INVEST_TOKEN"]

    runer = Runner(
        token=invest_token,
        prepared_strategy=prepared,
        dst=args.dst,
        params=RunnerParams(
            debug=args.debug))
    runer.run()

if __name__ == "__main__":
    main()
