"""Select and validate application inputs before content reads or preparation.

The checkout must remain private and stable during a build. Repeated lstat checks
reject unsafe trees; they do not provide isolation from a concurrent writer.
"""
from collections.abc import Iterator
from dataclasses import dataclass
import os
import logging
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time


EXPLICIT_FILES = ('main.py', 'handler.py', 'requirements.txt', 'requirements-runtime.txt')


@dataclass(frozen=True)
class ApplicationFile:
    """Checked source identity and the immutable bytes read for this build."""
    name: str
    path: Path
    data: bytes


@dataclass(frozen=True)
class SourceFacts:
    """Complete raw Git provenance, or explicitly unavailable local facts."""
    head: str | None = None
    dirty: bool | None = None
    git_state: str = 'unavailable'


@dataclass(frozen=True)
class ApplicationSnapshot:
    """A complete immutable application inventory owned by one build."""
    files: tuple[ApplicationFile, ...]
    source: SourceFacts = SourceFacts()


class InputError(ValueError):
    """A fixed safe category that never exposes external paths or file contents."""


def safe_member_name(name: str) -> bool:
    """Accept only portable relative file names shared with the ZIP checker."""
    parts = name.split('/')
    return bool(name) and ':' not in name and '\\' not in name and all(
        p and p not in ('.', '..', '.idea', '__pycache__', '.venv')
        and not p.startswith('.env') for p in parts) and not any(
            ord(c) < 32 or ord(c) == 127 for c in name)


def project_path(project_root: str | Path) -> Path:
    """Make a lexical absolute root without first following symbolic links."""
    path = Path(project_root)
    if '..' in path.parts:
        raise InputError('unsafe application root')
    return Path(os.path.abspath(path))


def validate_path(path: Path, root: Path, *, directory: bool = False) -> None:
    """Check every ancestor, final type and component-aware resolved containment."""
    try:
        relative = path.relative_to(root)
        if relative.parts and not safe_member_name(relative.as_posix()):
            raise InputError('unsafe application member name')
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            mode = current.lstat().st_mode
            expected_directory = current != path or directory
            if not (stat.S_ISDIR(mode) if expected_directory else stat.S_ISREG(mode)):
                raise InputError('unsafe application path type')
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError('unsafe application path unavailable') from None


def _children(directory: Path, root: Path) -> list[Path]:
    validate_path(directory, root, directory=True)
    try:
        return sorted(directory.iterdir())
    except OSError:
        raise InputError('unsafe application directory unavailable') from None


def _config_files(directory: Path, root: Path) -> Iterator[Path]:
    for path in _children(directory, root):
        if path.name.startswith('.'):
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            raise InputError('unsafe application path unavailable') from None
        if stat.S_ISDIR(mode):
            validate_path(path, root, directory=True)
            yield from _config_files(path, root)
        else:
            if not stat.S_ISREG(mode):
                raise InputError('unsafe application path type')
            if path.suffix == '.json':
                validate_path(path, root)
                yield path


def archive_files(project_root: str | Path) -> list[Path]:
    """Validate the complete allowlist before returning any application paths."""
    root = project_path(project_root)
    for directory in (root, root / 'autorepeater', root / 'autorepeater/configs'):
        validate_path(directory, root, directory=True)
    files = [root / name for name in EXPLICIT_FILES]
    for path in files:
        validate_path(path, root)
    for path in _children(root / 'autorepeater', root):
        if not path.name.startswith('.') and path.suffix == '.py':
            validate_path(path, root)
            files.append(path)
    files.extend(_config_files(root / 'autorepeater/configs', root))
    return files


def read_file(path: Path, root: Path) -> bytes:
    """Repeat validation immediately before reading an ordinary selected file."""
    validate_path(path, root)
    try:
        return path.read_bytes()
    except OSError:
        raise InputError('unsafe application read failed') from None


def snapshot(project_root: str | Path, *, release: bool = False,
             expected_sha: str = '') -> ApplicationSnapshot:
    """Validate the entire selection before reading each selected file exactly once."""
    root = project_path(project_root)
    paths = archive_files(root)
    files = tuple(ApplicationFile(path.relative_to(root).as_posix(), path,
                                  read_file(path, root)) for path in paths)
    return ApplicationSnapshot(files, source_facts(root, files, release=release,
                                                   expected_sha=expected_sha))


class GitFactsError(ValueError):
    """Internal safe failure; subprocess diagnostics never leave this boundary."""


GIT_LIMIT = 1024 * 1024
GIT_TIMEOUT = 10
SHA = re.compile(r'[0-9a-fA-F]{40}')


def git_environment() -> dict[str, str]:
    """Inherit only OS discovery essentials, never external Git overrides."""
    environment = {key: os.environ[key] for key in ('PATH', 'SystemRoot') if key in os.environ}
    environment.update(LANG='C', LC_ALL='C', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                       GIT_OPTIONAL_LOCKS='0')
    return environment


def _capture(process: subprocess.Popen, data: bytes | None) -> tuple[bytes, bytes]:
    """Drain both pipes with independent hard bounds and a wall-clock deadline."""
    buffers = [bytearray(), bytearray()]
    deadline = time.monotonic() + GIT_TIMEOUT
    if data is not None:
        process.stdin.write(data)
        process.stdin.close()
    with selectors.DefaultSelector() as selector:
        for index, stream in enumerate((process.stdout, process.stderr)):
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitFactsError('Git facts timeout')
            for key, _event in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk)
                if len(buffers[key.data]) > GIT_LIMIT:
                    raise GitFactsError('Git facts output limit')
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    return bytes(buffers[0]), bytes(buffers[1])


def git_output(root: Path, arguments: list[str], *, data: bytes | None = None,
               absent: bool = False) -> bytes:
    """Execute only argument-list Git commands, bounded and without config effects."""
    command = ['git', '--no-replace-objects', '-c', 'core.fsmonitor=false',
               '-c', 'core.untrackedCache=false',
               '-C', str(root), *arguments]
    try:
        with subprocess.Popen(command, env=git_environment(), stdin=(
                subprocess.PIPE if data is not None else subprocess.DEVNULL),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                output, _error = _capture(process, data)
                if process.returncode != 0 and not (absent and process.returncode == 1):
                    raise GitFactsError('Git facts command failed')
                return output
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except (OSError, subprocess.SubprocessError):
        raise GitFactsError('Git facts unavailable') from None


def _records(output: bytes, *, index: bool) -> dict[str, tuple[str, str]]:
    records = {}
    for record in output.split(b'\0')[:-1]:
        metadata, name = record.split(b'\t', 1)
        mode, identity, kind = metadata.decode('ascii').split(' ')
        if not index:
            identity, kind = kind, identity
        valid_kind = kind == '0' if index else kind in ('blob', 'commit')
        if (not valid_kind
                or mode not in ('100644', '100755', '120000')
                or not SHA.fullmatch(identity)):
            raise GitFactsError('Git facts unsupported index')
        relative = os.fsdecode(name)
        if not safe_member_name(relative) or relative in records:
            raise GitFactsError('Git facts unsafe tracked name')
        records[relative] = (mode, identity)
    if output and not output.endswith(b'\0'):
        raise GitFactsError('Git facts invalid records')
    return records


def _index(root: Path) -> dict[str, tuple[str, str]]:
    """Inspect original index metadata before asking Git to read tracked bytes."""
    for setting in ('core.sparseCheckout', 'index.sparse'):
        value = git_output(root, ['config', '--bool', '--get', setting], absent=True).strip()
        if value not in (b'', b'false'):
            raise GitFactsError('Git facts sparse checkout unsupported')
    records = _records(git_output(root, ['ls-files', '--stage', '-z']), index=True)
    flags = git_output(root, ['ls-files', '-v', '-z']).split(b'\0')[:-1]
    if len(flags) != len(records) or any(record[:2] != b'H ' for record in flags):
        raise GitFactsError('Git facts index flags unsupported')
    debug = git_output(root, ['ls-files', '--debug', '-z'])
    remaining = debug
    count = 0
    while remaining:
        _name, metadata = remaining.split(b'\0', 1)
        match = re.match(rb'  ctime: [^\n]+\n  mtime: [^\n]+\n  dev: [^\n]+\n'
                         rb'  uid: [^\n]+\n  size: [^\n]+\tflags: ([0-9a-f]+)\n', metadata)
        if match is None or int(match[1], 16) != 0:
            raise GitFactsError('Git facts index metadata unsupported')
        remaining = metadata[match.end():]
        count += 1
    if count != len(records):
        raise GitFactsError('Git facts incomplete index')
    return records


def _tracked_type(root: Path, name: str, expected: str) -> tuple[bool, bytes | None, bool]:
    """Validate ancestors before raw hashing; a symlink target is never opened."""
    path = root / name
    try:
        validate_path(path.parent, root, directory=True)
        mode = path.lstat().st_mode
        if expected == '120000':
            if stat.S_ISLNK(mode):
                return False, os.readlink(os.fsencode(path)), False
            return True, None, False
        if not stat.S_ISREG(mode):
            return True, None, False
        actual = '100755' if mode & stat.S_IXUSR else '100644'
        return actual != expected, None, True
    except (OSError, InputError):
        return True, None, False


def _raw_dirty(root: Path, records: dict[str, tuple[str, str]]) -> bool:
    dirty = False
    ordinary = []
    for name, (mode, identity) in records.items():
        changed, target, regular = _tracked_type(root, name, mode)
        dirty |= changed
        if target is not None:
            actual = git_output(root, ['hash-object', '--no-filters', '--stdin'],
                                data=target).strip().decode('ascii')
            if not SHA.fullmatch(actual):
                raise GitFactsError('Git facts invalid symlink hash')
            dirty |= actual != identity
        elif regular:
            ordinary.append(name)
    for offset in range(0, len(ordinary), 64):
        batch = ordinary[offset:offset + 64]
        hashes = git_output(root, ['hash-object', '--no-filters', '--', *batch]).splitlines()
        if len(hashes) != len(batch) or any(not SHA.fullmatch(h.decode('ascii')) for h in hashes):
            raise GitFactsError('Git facts incomplete hashes')
        dirty |= any(identity.decode('ascii') != records[name][1]
                     for name, identity in zip(batch, hashes))
    return dirty


def _known_facts(root: Path, files: tuple[ApplicationFile, ...]) -> SourceFacts:
    if os.fsdecode(git_output(root, ['rev-parse', '--show-toplevel']).rstrip(b'\n')) != str(root):
        raise GitFactsError('Git facts root mismatch')
    head = git_output(root, ['rev-parse', '--verify', 'HEAD']).strip().decode('ascii')
    if not SHA.fullmatch(head):
        raise GitFactsError('Git facts invalid HEAD')
    index = _index(root)
    tree = _records(git_output(root, ['ls-tree', '-r', '-z', '--full-tree', 'HEAD']), index=False)
    dirty = _raw_dirty(root, tree) or index != tree
    for file in files:
        if file.name not in tree:
            dirty = True
        else:
            blob = git_output(root, ['cat-file', 'blob', tree[file.name][1]])
            dirty |= blob != file.data
    return SourceFacts(head, dirty, 'known')


def source_facts(root: Path, files: tuple[ApplicationFile, ...], *, release: bool = False,
                 expected_sha: str = '') -> SourceFacts:
    """Return complete local facts or require verified clean release identity."""
    if expected_sha and not SHA.fullmatch(expected_sha):
        raise InputError('invalid expected source SHA')
    try:
        facts = _known_facts(root, files)
    except ValueError:
        if release or expected_sha:
            raise InputError('source provenance unavailable') from None
        logging.warning('cloud archive source provenance unavailable')
        return SourceFacts()
    if expected_sha and facts.head != expected_sha.lower():
        raise InputError('source HEAD mismatch')
    if release and facts.dirty:
        raise InputError('release source is dirty')
    return facts


def recheck(root: Path, original: ApplicationSnapshot, *, expected_sha: str = '') -> None:
    """Revalidate raw release state and the original input bytes before publication."""
    current = snapshot(root, release=True, expected_sha=expected_sha)
    if current != original:
        raise InputError('release source drift')
