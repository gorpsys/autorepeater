"""Verified installation ordering, receipts, interpreter and bounded pip calls."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import create_autospec

import pytest

from scripts import install_dependencies as installer
from scripts.runtime_lock import canonical_digest, load_lock
from scripts import sdk_wheel


ROOT = Path(__file__).resolve().parents[1]


def test_managed_pip_argv_environment_and_deadline(tmp_path, monkeypatch):
    """Selected Python ignores injected indexes/config and hides stderr credentials."""
    run = create_autospec(subprocess.run, spec_set=True,
                          return_value=subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setenv('PIP_EXTRA_INDEX_URL', 'https://secret@evil.test')
    monkeypatch.setenv('PIP_TRUSTED_HOST', 'evil.test')
    budget = installer.Budget(300)
    installer.pip(['check'], tmp_path, budget)
    args, = run.call_args.args
    assert args[:4] == [sys.executable, '-m', 'pip', '--isolated']
    assert args[-1] == 'check'
    kwargs = run.call_args.kwargs
    assert 0 < kwargs['timeout'] <= 300 and kwargs['cwd'] == tmp_path
    assert kwargs['env']['PIP_CONFIG_FILE'] == '/dev/null'
    assert 'PIP_EXTRA_INDEX_URL' not in kwargs['env']
    assert 'PIP_TRUSTED_HOST' not in kwargs['env']


@pytest.mark.parametrize('failure', ['install', 'timeout', 'os'])
def test_pip_failures_are_safe_and_fatal(tmp_path, monkeypatch, failure):
    """No raw stderr, fallback or receipt follows a solver/install failure."""
    run = create_autospec(subprocess.run, spec_set=True)
    if failure == 'install':
        run.return_value = subprocess.CompletedProcess([], 1, '', 'https://secret@evil.test')
    else:
        run.side_effect = (subprocess.TimeoutExpired([], 1) if failure == 'timeout'
                           else OSError('https://secret@evil.test'))
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(installer.InstallError, match='pip') as error:
        installer.pip(['install'], tmp_path, installer.Budget(300))
    assert 'secret' not in str(error.value)


def test_receipt_requires_exact_lock_source_hash_and_graph(tmp_path):
    """A receipt verifies acquisition identity, rather than just the SDK version."""
    document = load_lock(ROOT)
    facts = installer.receipt(document, 'vendored', {
        p['name']: p['artifacts'][0]['sha256'] for p in document['packages']})
    assert facts['lock_sha256'] == canonical_digest(document)
    path = tmp_path / 'receipt.json'
    data = tmp_path / 'sdk.py'
    data.write_bytes(b'original')
    facts.update(schema_version=2, installed_files=installer.package_wheels.file_hashes({
        'sdk.py': b'original'}), installed_members={p['name']: ['sdk.py']
                                                    for p in document['packages']})
    installer.write_receipt(path, facts)
    assert json.loads(path.read_text(encoding='utf-8')) == facts
    installer.check_receipt(path, document)
    facts['lock_sha256'] = '0' * 64
    path.write_text(json.dumps(facts), encoding='utf-8')
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


def test_bootstrap_help_needs_only_stdlib():
    """The helper imports without SDK, packaging, pip or pytest site packages."""
    result = subprocess.run([sys.executable, '-S', '-m', 'scripts.install_dependencies',
                             '--help'], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert '--development' in result.stdout


def test_same_version_replacement_invalidates_verified_receipt(tmp_path):
    """Changing code with unchanged metadata must invalidate the recorded installation."""
    document = load_lock(ROOT)
    hashes = {p['name']: p['artifacts'][0]['sha256'] for p in document['packages']}
    facts = installer.receipt(document, 'vendored', hashes)
    source = tmp_path / 'lib/sdk.py'
    source.parent.mkdir()
    source.write_bytes(b'original code')
    facts.update(schema_version=2, installed_files=installer.package_wheels.file_hashes({
        'lib/sdk.py': source.read_bytes()}), installed_members={p['name']: ['lib/sdk.py']
                                                                for p in document['packages']})
    path = tmp_path / installer.RECEIPT
    installer.write_receipt(path, facts)
    installer.check_receipt(path, document)
    source.write_bytes(b'replacement code with same Name/Version/Requires-Dist')
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


def test_proposed_runtime_conflict_precedes_effects(installation):
    """A retained package compatible only with the old runtime cannot allow an install."""
    _, pip, graph, _, _, path = installation
    path.write_text('previous receipt', encoding='ascii')

    def check(arguments, _root, _budget):
        if '--dry-run' in arguments and any(arg.endswith('.whl') for arg in arguments):
            raise installer.InstallError('proposed dependency conflict')
        return '{"installed":[]}' if arguments[0] == 'inspect' else ''

    pip.side_effect = check
    with pytest.raises(installer.InstallError, match='proposed dependency conflict'):
        installer.install(ROOT, development=False)
    assert path.read_text(encoding='ascii') == 'previous receipt'
    assert not any('--force-reinstall' in call.args[0] for call in pip.call_args_list)
    graph.assert_not_called()


def test_setup_uses_venv_helper_and_preserves_interpreter_contract():
    """PYTHON chooses checks; VENV_PYTHON remains setup's bootstrap interpreter."""
    result = subprocess.run(['make', '-n', 'setup', 'VENV_PYTHON=/bootstrap/python',
                             'PYTHON=/external/python'], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert '"/bootstrap/python" -m venv .venv' in result.stdout
    assert '.venv/bin/python -m scripts.install_dependencies --development' in result.stdout
    assert 'pip install -r requirements-dev.txt' not in result.stdout


@pytest.fixture(name='installation')
def installation_fixture(tmp_path, monkeypatch):
    """Keep real lock/inventory policy, replace only external effects with strict ports."""
    document = load_lock(ROOT)
    sdk = sdk_wheel.load_vendored_wheel(ROOT)
    selection = sdk_wheel.Selection(sdk_wheel.Source.VENDORED, sdk_wheel.Reason.TIMEOUT, sdk, None)
    acquire_sdk = create_autospec(sdk_wheel.acquire_sdk, spec_set=True, return_value=selection)
    acquire = create_autospec(installer.package_wheels.acquire, spec_set=True)
    acquire.return_value = b'verified-package-bytes'
    check = create_autospec(installer.runtime_lock.check_host, spec_set=True)
    metadata = create_autospec(installer.check_wheel_metadata, spec_set=True)
    pip = create_autospec(installer.pip, spec_set=True)
    pip.side_effect = lambda arguments, *_args: ('{"installed":[]}'
                                                 if arguments[0] == 'inspect' else '')
    graph = create_autospec(installer.graph, spec_set=True)
    monkeypatch.setattr(installer.runtime_lock, 'check_host', check)
    monkeypatch.setattr(installer.sdk_wheel, 'acquire_sdk', acquire_sdk)
    monkeypatch.setattr(installer.package_wheels, 'acquire', acquire)
    monkeypatch.setattr(installer, 'check_wheel_metadata', metadata)
    monkeypatch.setattr(installer, 'pip', pip)
    monkeypatch.setattr(installer, 'graph', graph)
    monkeypatch.setattr(installer.package_wheels, 'payload_files', create_autospec(
        installer.package_wheels.payload_files, spec_set=True,
        return_value={'sdk.py': b'original'}))
    monkeypatch.setattr(installer.package_wheels, 'locked_files', create_autospec(
        installer.package_wheels.locked_files, spec_set=True,
        return_value=installer.package_wheels.file_hashes({'sdk.py': b'original'})))

    def attest(_root, _document, facts, _expected):
        (tmp_path / 'sdk.py').write_bytes(b'original')
        return {**facts, 'schema_version': 2,
                'installed_files': installer.package_wheels.file_hashes({'sdk.py': b'original'}),
                'installed_members': {p['name']: ['sdk.py'] for p in document['packages']}}

    monkeypatch.setattr(installer, 'attest_environment', create_autospec(
        installer.attest_environment, spec_set=True, side_effect=attest))
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    return document, pip, graph, acquire_sdk, acquire, tmp_path / installer.RECEIPT


@pytest.mark.parametrize('development', [False, True])
def test_full_install_orders_validation_reinstall_and_receipt(installation, development):
    """Even the same version is reinstalled from acquired bytes before success attestation."""
    document, pip, graph, sdk, acquire, path = installation
    installer.install(ROOT, development=development)
    calls = [c.args[0] for c in pip.call_args_list]
    assert calls[0] == ['check']
    runtime = next(c for c in calls if '--force-reinstall' in c)
    assert runtime[:4] == ['install', '--no-index', '--no-deps', '--force-reinstall']
    filenames = [Path(p).name for p in runtime[8:]]
    assert filenames == [p['artifacts'][0]['filename'] for p in document['packages']]
    assert all(not Path(p).exists() for p in runtime[8:])
    assert all('--extra-index-url' not in c for c in calls)
    sdk.assert_called_once_with(ROOT, 'auto')
    assert acquire.call_count >= len(document['packages']) - 1
    assert all(0 < c.kwargs['timeout'] <= 300 for c in acquire.call_args_list)
    assert graph.call_count == (2 if development else 1)
    if development:
        proposed = calls[2]
        assert '--dry-run' in proposed and '-c' in proposed
        assert calls[-1][0:3] == ['install', '--no-index', '--find-links']
    installer.check_receipt(path, document)


@pytest.mark.parametrize('stage', ['runtime', 'graph', 'development', 'graph-after-dev'])
def test_failed_install_invalidates_previous_receipt(installation, stage):
    """No partial install may retain an earlier successful receipt."""
    _, pip, graph, _, _, path = installation
    path.write_text('old receipt', encoding='ascii')
    if stage == 'runtime':
        pip.side_effect = ['', '{"installed":[]}', '', installer.InstallError('pip failed')]
    elif stage == 'development':
        pip.side_effect = ['', '{"installed":[]}', '', '', installer.InstallError('pip failed')]
    elif stage == 'graph-after-dev':
        graph.side_effect = [None, installer.InstallError('graph drift')]
    else:
        graph.side_effect = installer.InstallError('graph drift')
    with pytest.raises(installer.InstallError):
        installer.install(ROOT, development=True)
    assert not path.exists()


def test_solver_failure_precedes_runtime_effects(installation):
    """A dev conflict never changes runtime/source or invalidates a current receipt."""
    _, pip, graph, sdk, _, path = installation
    path.write_text('previous valid receipt', encoding='ascii')
    pip.side_effect = ['', installer.InstallError('solver conflict')]
    with pytest.raises(installer.InstallError):
        installer.install(ROOT, development=True)
    assert path.read_text(encoding='ascii') == 'previous valid receipt'
    assert not any('--force-reinstall' in c.args[0] for c in pip.call_args_list)
    sdk.assert_called_once()
    graph.assert_not_called()


def test_other_package_failure_never_reselects_sdk(installation):
    """No silent downgrade or SDK source switch follows missing PyPI/reserve."""
    _, pip, graph, sdk, acquire, path = installation
    acquire.side_effect = installer.package_wheels.PackageError('package reserve unavailable')
    with pytest.raises(installer.package_wheels.PackageError):
        installer.install(ROOT, development=True)
    sdk.assert_called_once()
    graph.assert_not_called()
    assert pip.call_count == 1 and not path.exists()


def test_global_budget_never_resets(monkeypatch):
    """Time is shared across all packages instead of multiplying request allowances."""
    clock = create_autospec(installer.time.monotonic, spec_set=True,
                            side_effect=[100, 200, 399, 400])
    monkeypatch.setattr(installer.time, 'monotonic', clock)
    budget = installer.Budget(300)
    assert budget.remaining() == 200 and budget.remaining() == 1
    with pytest.raises(installer.InstallError, match='deadline'):
        budget.remaining()


@pytest.mark.parametrize('problem', [
    'missing', 'duplicate', 'schema', 'symlink', 'hashes', 'source'])
def test_invalid_receipt_fails_closed(tmp_path, problem):
    """No implicit broad approval or malformed local attestation is accepted."""
    document = load_lock(ROOT)
    path = tmp_path / 'receipt.json'
    facts = installer.receipt(document, 'vendored', {
        p['name']: p['artifacts'][0]['sha256'] for p in document['packages']})
    if problem == 'missing':
        pass
    elif problem == 'duplicate':
        path.write_text('{"schema_version":1,"schema_version":1}', encoding='ascii')
    elif problem == 'symlink':
        target = tmp_path / 'target.json'
        target.write_text(json.dumps(facts), encoding='ascii')
        path.symlink_to(target)
    else:
        facts[{'schema': 'schema_version', 'hashes': 'artifact_hashes',
               'source': 'sdk_source'}[problem]] = False
        path.write_text(json.dumps(facts), encoding='ascii')
    with pytest.raises(installer.InstallError, match='receipt'):
        installer.check_receipt(path, document)


def test_graph_checks_pip_inspect_and_constraints(monkeypatch):
    """The canonical graph check receives acquisition hashes, followed by pip check."""
    document = load_lock(ROOT)
    inspect = {'version': '1'}
    pip = create_autospec(installer.pip, spec_set=True, side_effect=[json.dumps(inspect), ''])
    check = create_autospec(installer.runtime_lock.check_installed_graph, spec_set=True)
    monkeypatch.setattr(installer, 'pip', pip)
    monkeypatch.setattr(installer.runtime_lock, 'check_installed_graph', check)
    budget = installer.Budget(300)
    installer.graph(document, ROOT, budget, {'example': 'digest'})
    check.assert_called_once_with(document, inspect, profile='development',
                                  artifact_hashes={'example': 'digest'})
    assert [c.args[0] for c in pip.call_args_list] == [['inspect', '--local'], ['check']]


def test_malformed_inspect_is_fatal(monkeypatch):
    """Invalid JSON never produces a successful graph/receipt."""
    pip = create_autospec(installer.pip, spec_set=True, return_value='not JSON')
    monkeypatch.setattr(installer, 'pip', pip)
    with pytest.raises(installer.InstallError, match='inspect'):
        installer.graph(load_lock(ROOT), ROOT, installer.Budget(300))


def test_sdk_metadata_validated_before_install():
    """Opaque dependency declarations match the lock without a homegrown marker parser."""
    document = load_lock(ROOT)
    sdk = sdk_wheel.load_vendored_wheel(ROOT)
    package = next(p for p in document['packages'] if p['name'] == sdk_wheel.NAME)
    installer.check_wheel_metadata(package, sdk.data)
    with pytest.raises(installer.InstallError, match='metadata'):
        installer.check_wheel_metadata({**package, 'requires_dist': []}, sdk.data)


@pytest.mark.parametrize('ok', [False, True])
def test_cli_safe_success_failure(monkeypatch, capsys, ok):
    """Command exit reflects verified setup; raw exceptions do not reach stderr."""
    install = create_autospec(installer.install, spec_set=True,
                              side_effect=None if ok else OSError('credential'))
    monkeypatch.setattr(installer, 'install', install)
    assert installer.main(['--project-root', str(ROOT), '--development']) == (0 if ok else 1)
    output = capsys.readouterr()
    assert 'credential' not in output.err
    assert ('installed' in output.out) == ok


def test_cli_preserves_safe_approval_reason(monkeypatch, capsys):
    """A mismatch still visibly requires approval rather than an opaque setup failure."""
    install = create_autospec(installer.install, spec_set=True,
                              side_effect=sdk_wheel.WheelError('SDK approval_required'))
    monkeypatch.setattr(installer, 'install', install)
    assert installer.main([]) == 1
    assert 'SDK approval_required' in capsys.readouterr().err


@pytest.mark.parametrize('drift', [None, 'version', 'metadata', 'missing'])
def test_make_guard_checks_real_locked_metadata(tmp_path, monkeypatch, drift):
    """Receipt alone cannot conceal runtime drift; the guard does not import SDK code."""
    document = load_lock(ROOT)
    hashes = {p['name']: p['artifacts'][0]['sha256'] for p in document['packages']}
    facts = installer.receipt(document, 'vendored', hashes)
    files = {'sdk.py': b'original'}
    members = {p['name']: ['sdk.py'] for p in document['packages']}
    (tmp_path / 'sdk.py').write_bytes(files['sdk.py'])
    facts.update(schema_version=2, installed_files=installer.package_wheels.file_hashes(files),
                 installed_members=members)
    installer.write_receipt(tmp_path / installer.RECEIPT, facts)
    monkeypatch.setattr(installer, 'runtime_snapshot', create_autospec(
        installer.runtime_snapshot, spec_set=True, return_value=(files, members, files)))
    monkeypatch.setattr(installer.package_wheels, 'locked_files', create_autospec(
        installer.package_wheels.locked_files, spec_set=True,
        return_value=installer.package_wheels.file_hashes(files)))
    distributions = []
    for package in document['packages']:
        distribution = create_autospec(installer.importlib.metadata.Distribution,
                                       instance=True, spec_set=True)
        distribution.metadata = {'Name': package['name']}
        distribution.version = package['version']
        distribution.requires = package['requires_dist']
        distributions.append(distribution)
    if drift == 'version':
        distributions[0].version = '0.0.0'
    elif drift == 'metadata':
        distributions[0].requires = ['unknown']
    elif drift == 'missing':
        distributions.pop()
    probe = create_autospec(installer.importlib.metadata.distributions, spec_set=True,
                            return_value=distributions)
    check_host = create_autospec(installer.runtime_lock.check_host, spec_set=True)
    pip = create_autospec(installer.pip, spec_set=True)
    monkeypatch.setattr(installer.importlib.metadata, 'distributions', probe)
    monkeypatch.setattr(installer.runtime_lock, 'check_host', check_host)
    monkeypatch.setattr(installer.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(installer, 'pip', pip)
    if drift:
        with pytest.raises(installer.runtime_lock.LockError):
            installer.check_environment(ROOT)
        pip.assert_not_called()
    else:
        installer.check_environment(ROOT)
        assert pip.call_args.args[:2] == (['check'], ROOT)
    probe.assert_called_once()


def test_requirement_drift_fails_before_network(tmp_path):
    """A copied bootstrap with mismatched generated requirements is not accepted."""
    document = load_lock(ROOT)
    (tmp_path / 'requirements.txt').write_text('manual drift', encoding='ascii')
    with pytest.raises(installer.InstallError, match='requirements drift'):
        installer.check_inputs(tmp_path, document)


@pytest.mark.parametrize('failure', ['dump', 'replace', 'symlink'])
def test_receipt_publication_is_atomic_and_cleans_temporary(tmp_path, monkeypatch, failure):
    """A publication failure keeps the old attestation and no orphan temporary."""
    path = tmp_path / 'receipt.json'
    path.write_text('old', encoding='ascii')
    if failure == 'symlink':
        path = tmp_path / 'linked.json'
        path.symlink_to(tmp_path / 'receipt.json')
    elif failure == 'dump':
        dump = create_autospec(installer.json.dump, spec_set=True, side_effect=OSError())
        monkeypatch.setattr(installer.json, 'dump', dump)
    else:
        replace = create_autospec(installer.os.replace, spec_set=True, side_effect=OSError())
        monkeypatch.setattr(installer.os, 'replace', replace)
    with pytest.raises((installer.InstallError, OSError)):
        installer.write_receipt(path, {})
    assert (tmp_path / 'receipt.json').read_text(encoding='ascii') == 'old'
    assert not list(tmp_path.glob('runtime-receipt-*'))


def test_empty_sdk_budget_fails_before_acquisition(installation, monkeypatch):
    """SDK's 60s maximum cannot overrun an almost exhausted 300s setup budget."""
    _, _, _, sdk, _, _ = installation
    remaining = create_autospec(installer.Budget.remaining, spec_set=True, return_value=59)
    monkeypatch.setattr(installer.Budget, 'remaining', remaining)
    with pytest.raises(installer.InstallError, match='SDK acquisition budget'):
        installer.install(ROOT)
    sdk.assert_not_called()


def test_unlocked_sdk_hash_prevents_install(installation):
    """Approval does not substitute for an explicit runtime artifact lock update."""
    from dataclasses import replace  # pylint: disable=import-outside-toplevel
    _, pip, graph, sdk, _, path = installation
    selected = sdk.return_value
    altered = replace(selected.wheel.manifest, sha256='0' * 64)
    sdk.return_value = replace(selected, wheel=replace(selected.wheel, manifest=altered))
    with pytest.raises(installer.runtime_lock.LockError, match='outside runtime lock'):
        installer.install(ROOT)
    assert pip.call_count == 1
    graph.assert_not_called()
    assert not path.exists()
