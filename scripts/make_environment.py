"""Fail before tools or credential access when the selected Python lacks dependencies."""
import argparse
import importlib.util
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
    if not missing:
        return 0
    print(f'{args.target}: Python {sys.executable} lacks modules: {", ".join(missing)}.\n'
          'Run make setup to prepare .venv, or install into your selected Python:\n'
          f'  {shlex.quote(sys.executable)} -m pip install -r requirements-dev.txt',
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
