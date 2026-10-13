"""Explicit reviewed-seed lock update using pip's supported JSON resolver report."""
import argparse
import json
import subprocess
import sys
import io
import zipfile
from dataclasses import replace
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from scripts import runtime_lock as lock
from scripts.sdk_wheel import (
    NAME, DIST_INFO, acquire_sdk, load_vendored_wheel, ValidatedWheel,
    check_approval, validate_wheel,
)


def _report_package(raw: object, root: Path,
                    inventory: dict[str, dict[str, Any]],
                    official: dict[str, Any] | None) -> dict[str, Any]:
    item = lock.record(raw)
    metadata = lock.record(item.get('metadata'))
    name = lock.normalized_name(metadata.get('name'))
    lock.require(name in inventory, 'resolver closure drift')
    seed = inventory[name]
    lock.require(metadata.get('version') == seed['version'], 'resolver version drift')
    download = lock.record(item.get('download_info'))
    hashes = lock.record(lock.record(download.get('archive_info')).get('hashes'))
    artifacts = lock.approved_artifacts(root, seed)
    if name == NAME and official is not None:
        artifacts.append(official)
    matches = [a for a in artifacts if a['sha256'] == hashes.get('sha256')]
    lock.require(len(matches) == 1, 'resolver artifact hash drift')
    artifact = matches[0]
    requirements = sorted(set(lock.strings(metadata.get('requires_dist', []))))
    url = download.get('url')
    if name == NAME:
        lock.require(isinstance(url, str) and url.startswith('file:///')
                     and url.endswith('/' + artifact['filename']), 'resolver SDK source drift')
        lock.require(requirements == sdk_requirements(load_vendored_wheel(root)),
                     'resolver SDK metadata drift')
    else:
        lock.require(url == artifact['url'], 'resolver source drift')
    return {'name': name, 'version': seed['version'], 'requires': sorted(seed['requires']),
            'requires_dist': requirements,
            'artifacts': artifacts if name == NAME else [artifact]}


def sdk_requirements(wheel: ValidatedWheel) -> list[str]:
    """Read opaque Requires-Dist only from previously validated immutable SDK bytes."""
    with zipfile.ZipFile(io.BytesIO(wheel.data)) as archive:
        metadata = BytesParser().parsebytes(archive.read(DIST_INFO + 'METADATA'))
    return sorted(set(metadata.get_all('Requires-Dist', [])))


def _official_artifact(root: Path, sdk: ValidatedWheel | None) -> dict[str, Any] | None:
    if sdk is None:
        return None
    reserve = load_vendored_wheel(root)
    check_approval(reserve.manifest, sdk.manifest.sha256)
    validate_wheel(sdk.data, replace(reserve.manifest, size=len(sdk.data),
                                     sha256=sdk.manifest.sha256), reserve.license_bytes)
    lock.require(sdk_requirements(sdk) == sdk_requirements(reserve),
                 'approved SDK dependency metadata differs; review runtime policy separately')
    if reserve.manifest.sha256 == sdk.manifest.sha256:
        return None
    return {'filename': sdk.manifest.identity.filename, 'sha256': sdk.manifest.sha256,
            'tag': sdk.manifest.identity.tag, 'size': len(sdk.data),
            'url': 'tbank:sha256:' + sdk.manifest.sha256}


def lock_from_report(value: object, root: Path, *,
                     sdk: ValidatedWheel | None = None) -> dict[str, Any]:
    """Reject missing/new/version/artifact surprises before writing any policy.

    Requires-Dist stays opaque: pip resolves PEP 508 and evaluates environment
    markers on real CPython 3.12. Reviewed inventory edges are the working seed;
    the report must confirm its complete set under exact artifact constraints.
    """
    report = lock.record(value)
    lock.require(report.get('version') == '1', 'unsupported pip report')
    lock.require(isinstance(report.get('pip_version'), str) and bool(report['pip_version']),
                 'invalid pip report version')
    environment = lock.record(report.get('environment'))
    lock.require(all(environment.get(k) == v for k, v in lock.MARKER_ENVIRONMENT.items())
                 and environment.get('os_name') == 'posix'
                 and environment.get('platform_python_implementation') == 'CPython'
                 and isinstance(environment.get('python_full_version'), str)
                 and environment['python_full_version'].startswith('3.12.')
                 and isinstance(environment.get('implementation_version'), str)
                 and environment['implementation_version'].startswith('3.12.'),
                 'resolver target marker drift')
    lock.require(isinstance(report.get('install'), list), 'invalid resolver report')
    inventory = lock.runtime_inventory(root)
    official = _official_artifact(root, sdk)
    packages: dict[str, dict[str, Any]] = {}
    for raw in report['install']:
        package = _report_package(raw, root, inventory, official)
        lock.require(package['name'] not in packages, 'resolver closure drift')
        packages[package['name']] = package
    lock.require(set(packages) == set(inventory), 'resolver missing/extra runtime closure')
    # Hash only public, portable report facts; pip's local SDK URI is deliberately absent.
    packages_sorted = sorted(packages.values(), key=lambda p: p['name'])
    return lock.validate_lock({'schema_version': 1, 'target': lock.TARGET,
                               'sources': lock.SOURCES, 'roots': lock.ROOTS,
                               'packages': packages_sorted,
                               'report_sha256': lock.report_digest(packages_sorted)}, root)


def run_resolver(root: Path, directory: Path, sdk_path: Path, *,
                 run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Path:
    """Resolve only two runtime roots under exact reviewed artifact constraints."""
    inventory = lock.runtime_inventory(root)
    constraints = directory / 'seed-constraints.txt'
    constraints.write_text(''.join(
        f'{name} @ {p["artifacts"][0]["url"]}#sha256={p["artifacts"][0]["sha256"]}\n'
        for name, p in sorted(inventory.items()) if name != NAME), encoding='utf-8')
    report_path = directory / 'pip-report.json'
    command = [sys.executable, '-m', 'pip', 'install', '--dry-run', '--ignore-installed',
               '--report', str(report_path), '--only-binary=:all:', '--index-url',
               lock.SOURCES['pypi'], '--timeout', '15', '--retries', '0',
               '--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_17_x86_64',
               '--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312',
               '--abi', 'abi3', '--abi', 'none', '-c', str(constraints),
               str(sdk_path), 'python-json-logger==' + inventory['python-json-logger']['version']]
    try:
        result = run(command, env=lock.pip_environment(), capture_output=True,
                     text=True, timeout=300, check=False)
        lock.require(result.returncode == 0, 'pip resolver failed; no lock written')
    except (OSError, subprocess.TimeoutExpired):
        raise lock.LockError('pip resolver unavailable/timed out; no lock written') from None
    return report_path


def update(root: Path, *, report: Path | None = None, vendored: bool = False) -> dict[str, Any]:
    """Acquire immutable verified SDK bytes; generation never edits approvals."""
    lock.check_host()
    if report is not None:
        return lock_from_report(lock.read_json(report), root)
    selection = acquire_sdk(root, 'vendored' if vendored else 'auto')
    reserve = load_vendored_wheel(root)
    with TemporaryDirectory(prefix='runtime-lock-') as temporary:
        directory = Path(temporary)
        sdk = directory / reserve.manifest.identity.filename
        sdk.write_bytes(selection.wheel.data)
        path = run_resolver(root, directory, sdk)
        return lock_from_report(lock.read_json(path), root, sdk=selection.wheel)


def main(arguments: list[str] | None = None) -> int:
    """Explicit update; output is a reviewable policy change, not an installed receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report', type=Path)
    parser.add_argument('--vendored-sdk', action='store_true')
    args = parser.parse_args(arguments)
    try:
        document = update(args.project_root, report=args.report, vendored=args.vendored_sdk)
        (args.project_root / 'runtime-lock.json').write_text(
            json.dumps(document, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        for name, text in lock.render_requirements(document).items():
            (args.project_root / name).write_text(text, encoding='utf-8')
        print('updated runtime lock: ' + lock.canonical_digest(document))
        return 0
    except (lock.LockError, OSError, ValueError):
        print('runtime lock update failed; review resolver/source/target facts', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
