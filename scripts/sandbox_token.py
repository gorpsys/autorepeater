"""Launch a command with a sandbox token from CI, a local file or hidden input."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings


def _validate_token(value: str) -> str:
    token = value.strip()
    if not token or any(character.isspace() or character == '\0' for character in token):
        raise ValueError('sandbox credentials must contain a single token')
    return token


def load_token(path: Path) -> str:
    """Read credentials without echoing them or changing the parent's environment."""
    token = os.environ.get('SANDBOX_TOKEN', '').strip()
    if token:
        return _validate_token(token)
    try:
        token = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        token = ''
    except (OSError, UnicodeError) as error:
        raise ValueError('cannot read sandbox token file') from error
    if token:
        return _validate_token(token)
    if not sys.stdin.isatty():
        raise ValueError('SANDBOX_TOKEN is required without an interactive terminal')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Sandbox token: ')
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning) as error:
        raise ValueError('cannot securely read sandbox token from terminal') from error
    return _validate_token(token)


def main(argv: list[str] | None = None) -> int:
    """Replace this launcher so the child keeps its exit status and signal handling."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, default=Path('.sandbox_token'))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('a command after -- is required')
    try:
        token = load_token(args.token_file)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    environment = dict(os.environ, SANDBOX_TOKEN=token)
    try:
        os.execvpe(command[0], command, environment)
    except OSError:
        print('cannot start sandbox command', file=sys.stderr)
        return 127
    return 0


if __name__ == '__main__':
    sys.exit(main())
