"""Validate the release declared by a PR and publish its immutable annotated tag."""
import argparse
import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import subprocess
import sys


VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'


class ReleaseError(ValueError):
    """A release cannot be checked or published safely."""


@dataclass(frozen=True)
class Release:
    """A numbered CHANGELOG section, shared by validation and tag annotation."""
    version: tuple[int, int, int]
    date: str
    notes: str

    @property
    def name(self) -> str:
        """The release identifier, without the Git tag prefix."""
        return '.'.join(map(str, self.version))


def parse_version(value: str) -> tuple[int, int, int]:
    """Compare numeric components, never lexicographically."""
    match = re.fullmatch(VERSION, value)
    if match is None:
        raise ReleaseError('version must be X.Y.Z without leading zeros or suffixes')
    major, minor, patch = map(int, match.groups())
    return major, minor, patch


def read_release(document: str, *, required: bool = True) -> Release | None:
    """The first numbered section is the candidate; code examples are not releases."""
    document = re.sub(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$', '', document)
    sections = re.split(r'^## (.+)$', document, flags=re.MULTILINE)
    for title, body in zip(sections[1::2], sections[2::2]):
        if title.strip() == 'Не выпущено':
            continue
        heading = re.fullmatch(r'\[([^\]]+)\]\([^)]+\) - ([0-9]{4}-[0-9]{2}-[0-9]{2})', title)
        if heading is None:
            raise ReleaseError('first release must use ## [X.Y.Z](link) - YYYY-MM-DD')
        version = parse_version(heading[1])
        try:
            date.fromisoformat(heading[2])
        except ValueError as error:
            raise ReleaseError('invalid release date') from error
        if not any(line.startswith('- ') for line in body.splitlines()):
            raise ReleaseError('release notes must contain at least one change')
        return Release(version, heading[2], body.strip())
    if required:
        raise ReleaseError('declare a numbered release in CHANGELOG.md')
    return None


def git(root: Path, *arguments: str, input_text: str | None = None) -> str:
    """Bound Git calls and avoid exposing command output or authentication details."""
    try:
        result = subprocess.run(['git', *arguments], cwd=root, input=input_text,
                                text=True, capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError('git command failed: ' + arguments[0]) from error
    return result.stdout.strip()


def application_version(root: Path) -> tuple[int, int, int]:
    """Read a literal version without importing the application or its SDK."""
    tree = ast.parse((root / 'autorepeater/__init__.py').read_text(encoding='utf-8'))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == '__version__'
                      for target in node.targets)]
    if (len(values) != 1 or not isinstance(values[0], ast.Constant)
            or not isinstance(values[0].value, str)):
        raise ReleaseError('autorepeater.__version__ must be one literal X.Y.Z string')
    return parse_version(values[0].value)


def base_version(root: Path, base: str) -> tuple[int, int, int] | None:
    """A merged release protects against a delayed or failed tag publication."""
    if re.fullmatch(r'[0-9a-f]{40}', base) is None:
        raise ReleaseError('base must be a full commit SHA')
    git(root, 'rev-parse', '--verify', base + '^{commit}')
    if not git(root, 'ls-tree', '--name-only', base, '--', 'CHANGELOG.md'):
        return None
    previous = read_release(git(root, 'show', base + ':CHANGELOG.md'), required=False)
    return None if previous is None else previous.version


def tag_versions(root: Path, tags: list[str], sha: str, *,
                 allow_descendants: bool) -> list[tuple[int, int, int]]:
    """A delayed master job must not lose its tag because a later merge ran first."""
    versions = []
    for tag in tags:
        if not tag.startswith('v') or re.fullmatch(VERSION, tag[1:]) is None:
            continue
        if allow_descendants:
            target = git(root, 'rev-parse', tag + '^{commit}')
            if target != sha and git(root, 'merge-base', sha, target) == sha:
                continue
        versions.append(parse_version(tag[1:]))
    return versions


def validate(root: Path, *, base: str | None = None,
             allow_existing: bool = False) -> tuple[Release, str, bool]:
    """PRs advance versions; master retries may reuse a tag on exactly their SHA."""
    candidate = read_release((root / 'CHANGELOG.md').read_text(encoding='utf-8'))
    assert candidate is not None
    if application_version(root) != candidate.version:
        raise ReleaseError('CLI version and first CHANGELOG release must match')
    sha = git(root, 'rev-parse', 'HEAD')
    tags = git(root, 'for-each-ref', '--format=%(refname:strip=2)', 'refs/tags').splitlines()
    name = 'v' + candidate.name
    if allow_existing and name in tags:
        if git(root, 'rev-parse', name + '^{commit}') != sha:
            raise ReleaseError('version tag already belongs to a different commit')
        return candidate, sha, True
    versions = tag_versions(root, tags, sha, allow_descendants=allow_existing)
    previous = base_version(root, base) if base else None
    if previous is not None:
        versions.append(previous)
    if versions and candidate.version <= max(versions):
        raise ReleaseError('release version must be greater than existing tags and base version')
    return candidate, sha, False


def publish(root: Path) -> Release:
    """Normal Git push refuses conflicting tags; a retry never moves an existing tag."""
    parent = git(root, 'rev-parse', 'HEAD^')
    candidate, sha, exists = validate(root, base=parent, allow_existing=True)
    if not exists:
        message = f'autorepeater {candidate.name}\n\n{candidate.date}\n\n{candidate.notes}\n'
        git(root, '-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'tag', '-a', 'v' + candidate.name, sha, '-F', '-', input_text=message)
    git(root, 'push', 'origin', 'refs/tags/v' + candidate.name)
    return candidate


def main(argv: list[str] | None = None) -> int:
    """Work without application dependencies, tokens or a user Git profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'tag'])
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--base')
    parser.add_argument('--allow-existing', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'tag':
            candidate = publish(args.project_root)
        else:
            candidate, _, _ = validate(args.project_root, base=args.base,
                                       allow_existing=args.allow_existing)
        print(f'Release {candidate.name}: {args.command} OK')
        return 0
    except (ReleaseError, OSError, SyntaxError) as error:
        reason = (str(error) if isinstance(error, ReleaseError)
                  else 'release files unavailable or invalid')
        print(f'Release rejected: {reason}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
