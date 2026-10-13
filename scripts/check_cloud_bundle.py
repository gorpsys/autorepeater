"""Build and smoke ready bundles with package networking, without broker or cloud calls.

Vendored installation uses PyPI and a verified local SDK; only SDK acquisition probes
T-Bank's index. Offline ZIP verification runs no installer or network acquisition.
The shared import socket guard is Python-level, not isolation of native syscalls.
"""
import argparse
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import BadZipFile, ZipFile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import build_yandex_archive as builder
from scripts import cloud_dependencies as cloud
from scripts import install_dependencies as installer
from scripts import runtime_lock, sdk_wheel
from scripts.package_wheels import PackageError
from scripts.sdk_worker import WorkerError
from scripts.vendor_inventory import InventoryError


MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 20000


class BundleError(ValueError):
    """Fixed safe bundle error without child output or credentials."""


def require(condition: bool, reason: str) -> None:
    """Reject invalid ready artifacts before installation or import."""
    if not condition:
        raise BundleError(reason)


def read_bundle(path: Path) -> dict[str, bytes]:
    """Bound decompression and reject unsafe members without using extractall."""
    require(not path.is_symlink() and all(not p.is_symlink() for p in path.parents)
            and path.is_file() and path.stat().st_size <= MAX_BYTES, 'invalid bundle file')
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            names = [m.filename for m in members]
            require(len(names) <= MAX_ENTRIES and len(set(names)) == len(names)
                    and sum(m.file_size for m in members) <= MAX_BYTES, 'bundle ZIP limits')
            require(all(m.filename == m.orig_filename and cloud.safe_path(m.orig_filename)
                        and not m.is_dir()
                        and m.filename.split('/')[0] != 'scripts'
                        and not m.flag_bits & 1
                        and stat.S_IFMT(m.external_attr >> 16) in (0, stat.S_IFREG)
                        for m in members), 'unsafe bundle member')
            require(not any('/'.join(n.split('/')[:i]) in names
                            for n in names for i in range(1, len(n.split('/')))),
                    'bundle path collision')
            return {m.filename: archive.read(m) for m in members}
    except (BadZipFile, RuntimeError, NotImplementedError):
        raise BundleError('invalid bundle ZIP or CRC') from None


def application_files(root: Path) -> dict[str, bytes]:
    """Compare the shipped application/configuration with this exact checkout."""
    paths = [p for p in builder.archive_files(root) if not p.name.startswith('requirements')]
    require(all(not p.is_symlink() and all(not parent.is_symlink() for parent in p.parents)
                for p in paths), 'unsafe application file')
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in paths}


def _vendored(document: dict[str, Any], root: Path, files: dict[str, bytes],
              application: dict[str, bytes]) -> None:
    wheel = sdk_wheel.load_vendored_wheel(root)
    package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    installer.check_wheel_metadata(package, wheel.data)
    prefix = 'vendor/t-tech-investments/'
    expected = {**application, **builder.vendored_files(document, wheel,
                runtime_lock.read_json(root / prefix / 'manifest.json'))}
    require(files == expected, 'vendored bundle requirements/content drift')


def _offline(document: dict[str, Any], root: Path, files: dict[str, bytes],
             application: dict[str, bytes]) -> None:
    require(cloud.MANIFEST in files and not any(n.endswith('.whl') or n.split('/')[0].startswith(
        'requirements') or n.startswith('vendor/') for n in files),
        'offline bundle contains installer inputs or lacks manifest')
    manifest = json.loads(files[cloud.MANIFEST])
    require(isinstance(manifest, dict), 'invalid payload manifest')
    payload = {n: data for n, data in files.items() if n not in application
               and n != cloud.MANIFEST and not n.startswith('THIRD_PARTY_NOTICES/')}
    cloud.check_paths(payload, application)
    cloud.verify_graph(document, payload)
    notices, packages = cloud.payload_notices(document, root, payload)
    require({n: data for n, data in files.items() if n.startswith('THIRD_PARTY_NOTICES/')}
            == notices, 'payload notice drift')
    hashes = manifest.get('artifact_hashes')
    require(isinstance(hashes, dict) and all(runtime_lock.digest(h) for h in hashes.values()),
            'invalid payload artifact hashes')
    source = manifest.get('sdk_source')
    require(isinstance(source, str), 'invalid payload SDK source')
    facts = installer.receipt(document, source, hashes)
    require(cloud.package_wheels.file_hashes(payload) ==
            cloud.package_wheels.locked_files(root, document, hashes),
            'payload file lock content/membership drift')
    require(manifest.get('payload_files') == cloud.package_wheels.file_hashes(payload),
            'payload file content drift')
    for package in packages:
        locked = next(p for p in document['packages'] if p['name'] == package['name'])
        package['artifact'] = next(dict(a) for a in locked['artifacts']
                                   if a['sha256'] == hashes[package['name']])
    require(runtime_lock.canonical_bytes(manifest) == runtime_lock.canonical_bytes({
        **facts, 'sdk_artifact_sha256': hashes[sdk_wheel.NAME], 'packages': packages,
        'payload_files': cloud.package_wheels.file_hashes(payload)}),
        'payload manifest/lock drift')


def verify_install_report(document: dict[str, Any], path: Path, unpacked: Path) -> None:
    """Check pip's recorded artifacts, sources and closure against the canonical lock."""
    report = runtime_lock.read_json(path)
    require(report.get('version') == '1' and isinstance(report.get('install'), list),
            'invalid bundle install report')
    hashes: dict[str, str] = {}
    for item in report['install']:
        metadata = runtime_lock.record(runtime_lock.record(item).get('metadata'))
        name = runtime_lock.normalized_name(metadata.get('name'))
        require(name not in hashes, 'duplicate installed bundle artifact')
        info = runtime_lock.record(item.get('download_info'))
        archive = runtime_lock.record(info.get('archive_info'))
        digest = runtime_lock.record(archive.get('hashes')).get('sha256')
        require(isinstance(digest, str) and runtime_lock.digest(digest),
                'invalid bundle install artifact hash')
        assert isinstance(digest, str)
        package = next((p for p in document['packages'] if p['name'] == name), None)
        require(package is not None and any(
            a['sha256'] == digest and info.get('url') == (
                (unpacked / a['url'][5:]).as_uri() if name == sdk_wheel.NAME else a['url'])
            for a in package['artifacts']), 'bundle install artifact/source drift')
        hashes[name] = digest
    runtime_lock.check_installed_graph(document, {
        'version': '1', 'environment': report.get('environment'),
        'installed': report['install']}, profile='cloud', artifact_hashes=hashes)


def _write_files(target: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def check_bundle(root: Path, path: Path, mode: str) -> None:
    """Validate a ready ZIP; the offline branch never invokes pip or source acquisition."""
    require(mode in ('vendored', 'offline'), 'unsupported bundle mode')
    cloud.check_platform()
    document = runtime_lock.load_lock(root)
    application = application_files(root)
    files = read_bundle(path)
    require(all(files.get(n) == data for n, data in application.items()),
            'bundle application drift')
    with TemporaryDirectory(prefix='cloud-bundle-check-') as directory:
        staged = Path(directory)
        target = staged / 'payload'
        target.mkdir()
        if mode == 'offline':
            _offline(document, root, files, application)
            _write_files(target, files)
            cloud.smoke_payload(target, timeout=60)
        else:
            _vendored(document, root, files, application)
            unpacked = staged / 'unpacked'
            unpacked.mkdir()
            _write_files(unpacked, files)
            budget = installer.Budget(300)
            report = staged / 'install-report.json'
            installer.pip(['install', '--index-url', document['sources']['pypi'],
                           '--target', str(target), '--ignore-installed', '--no-compile',
                           '--no-cache-dir', '--require-hashes', '--only-binary=:all:',
                           '--timeout', '15', '--retries', '0', '--report', str(report),
                           '-r', 'requirements.txt'], unpacked, budget)
            verify_install_report(document, report, unpacked)
            payload = cloud.read_target(target)
            cloud.verify_graph(document, payload)
            cloud.check_paths(payload, application)
            _write_files(target, application)
            cloud.smoke_payload(target, timeout=min(60, budget.remaining()))
            budget.remaining()


def check_packages(root: Path, expected_sha: str = '') -> dict[str, str]:
    """Build both reserved SDK modes in an owned folder and report the tested identity."""
    cloud.check_platform()
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
                            text=True, timeout=10, check=False)
    sha = result.stdout.strip()
    require(result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', sha) is not None
            and (not expected_sha or expected_sha == sha), 'package checkout SHA mismatch')
    document = runtime_lock.load_lock(root)
    installer.check_inputs(root, document)
    with TemporaryDirectory(prefix='cloud-package-smoke-') as directory:
        for mode in ('vendored', 'offline'):
            path = Path(directory) / (mode + '.zip')
            builder.build_archive(root, path, use_vendored_sdk=True,
                                  offline_archive=mode == 'offline')
            check_bundle(root, path, mode)
    return {'status': 'ok', 'checkout_sha': sha,
            'lock_sha256': runtime_lock.canonical_digest(document)}


def main(arguments: list[str] | None = None) -> int:
    """Make and both workflows share this package-only entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--expected-sha', default='')
    args = parser.parse_args(arguments)
    try:
        facts = check_packages(args.project_root.resolve(), args.expected_sha)
        print(json.dumps(facts, sort_keys=True))
        return 0
    except (BundleError, cloud.PayloadError, installer.InstallError, runtime_lock.LockError,
            sdk_wheel.WheelError, WorkerError, PackageError, InventoryError,
            OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        reason = 'local files/process unavailable' if isinstance(error, (
            OSError, subprocess.TimeoutExpired, json.JSONDecodeError)) else str(error)
        print('cloud package smoke failed: ' + reason, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
