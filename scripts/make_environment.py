"""Fail before tools or credential access when the selected Python lacks dependencies."""
import argparse
import importlib.util
from pathlib import Path
import shlex
import sys


RUNTIME_MODULES = ('t_tech', 'pythonjsonlogger')
MODULES = {
    'lint': (*RUNTIME_MODULES, 'pylint', 'flake8', 'mypy'),
    'typecheck': (*RUNTIME_MODULES, 'mypy'),
    'test': (*RUNTIME_MODULES, 'pytest', 'mypy'),
    'coverage': (*RUNTIME_MODULES, 'pytest', 'pytest_cov', 'coverage', 'diff_cover', 'mypy'),
    'e2e': (*RUNTIME_MODULES, 'pytest'),
}


def main(argv: list[str] | None = None) -> int:
    """Check dependencies in this interpreter without importing application or SDK code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=MODULES)
    args = parser.parse_args(argv)
    missing = [module for module in MODULES[args.target]
               if importlib.util.find_spec(module) is None]
    failure = f'lacks modules: {", ".join(missing)}'
    if not missing:
        # Direct script entrypoints do not include the checkout root in sys.path.
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from scripts.install_dependencies import check_environment  # pylint: disable=import-outside-toplevel
        try:
            check_environment(root)
            return 0
        except (ValueError, OSError):
            failure = 'requires a current runtime receipt and locked dependency graph'
    print(f'{args.target}: Python {sys.executable} {failure}.\n'
          'Run make setup to prepare .venv, or run from the project root:\n'
          f'  {shlex.quote(sys.executable)} -m scripts.install_dependencies --development',
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
