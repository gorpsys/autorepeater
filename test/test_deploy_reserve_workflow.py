"""Reserve deployment preserves the production trust boundary without cloud calls."""
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def workflow_scalar(value):
    """Decode only the scalar/flow-list syntax used by these two workflows."""
    if value == '{}':
        return {}
    if value in ('true', 'false'):
        return value == 'true'
    if value.isdecimal():
        return int(value)
    if value.startswith('[') and value.endswith(']'):
        return [workflow_scalar(item.strip()) for item in value[1:-1].split(',')]
    if value.startswith(("'", '"')):
        assert value[-1] == value[0]
        return value[1:-1]
    assert not value.startswith(('[', '{', '&', '*', '!'))
    return value or None


def workflow_block(lines, index, indent, style):
    """Preserve literal shell lines and fold the existing multi-line gate condition."""
    block = []
    while index < len(lines) and lines[index].startswith(' ' * (indent + 2)):
        block.append(lines[index][indent + 2:])
        index += 1
    return ('\n'.join(block) + '\n' if style == '|' else ' '.join(block)), index


def parse_workflow(path: Path) -> dict:
    """Parse the workflows' restricted YAML grammar using only stdlib, failing closed."""
    lines = path.read_text(encoding='utf-8').splitlines()

    def mapping(start, indent):
        result = {}
        index = start
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            depth = len(line) - len(line.lstrip())
            if depth < indent:
                break
            assert depth == indent and '\t' not in line
            match = re.fullmatch(r'([a-zA-Z_][a-zA-Z0-9_-]*):(?: (.*))?', line.strip())
            assert match is not None, f'unsupported YAML: {line}'
            key, value = match.groups()
            assert key not in result
            index += 1
            if value in ('|', '>-'):
                result[key], index = workflow_block(lines, index, indent, value)
            elif value is not None:
                result[key] = workflow_scalar(value)
            elif index < len(lines) and lines[index].startswith(' ' * (indent + 2)):
                if lines[index].lstrip().startswith('- '):
                    steps = []
                    while index < len(lines) and lines[index].startswith(' ' * (indent + 2) + '- '):
                        original = lines[index]
                        lines[index] = ' ' * (indent + 4) + original.lstrip()[2:]
                        step, index = mapping(index, indent + 4)
                        steps.append(step)
                    result[key] = steps
                else:
                    result[key], index = mapping(index, indent + 2)
            else:
                result[key] = None
        return result, index

    document, end = mapping(0, 0)
    assert end == len(lines)
    return document


@pytest.fixture(name='workflows')
def workflow_documents():
    """Parse actual checked-in files; never use a duplicated workflow fixture."""
    return tuple(parse_workflow(ROOT / f'.github/workflows/{name}.yml')
                 for name in ('deploy-prod', 'deploy-prod-reserve'))


def test_only_manual_dispatch_and_two_modes(workflows):
    """The UI choice and shell allowlist must agree, including a mandatory input."""
    _, reserve = workflows
    assert reserve['on'] == {'workflow_dispatch': {'inputs': {'mode': {
        'description': 'Cloud package mode', 'type': 'choice', 'required': True,
        'default': 'vendored', 'options': ['vendored', 'offline'],
    }}}}
    assert set(reserve['jobs']) == {'gate', 'deploy'}


def test_permissions_concurrency_and_cloud_parameters_match_original(workflows):
    """Compare every actual resource/auth value, rather than a stale copied sample."""
    original, reserve = workflows
    assert reserve['permissions'] == original['permissions'] == {}
    assert reserve['concurrency'] == original['concurrency'] == {
        'group': 'deploy-prod', 'cancel-in-progress': False, 'queue': 'max'}
    for name in ('gate', 'deploy'):
        baseline, actual = original['jobs'][name], reserve['jobs'][name]
        for key in ('permissions', 'runs-on', 'timeout-minutes'):
            assert actual[key] == baseline[key]
        assert 'environment' not in actual
        assert not any(step.get('continue-on-error') or step.get('if')
                       for step in actual['steps'])
    assert reserve['jobs']['gate']['permissions'] == {
        'contents': 'read', 'checks': 'read', 'statuses': 'read'}
    assert reserve['jobs']['deploy']['env'] == original['jobs']['deploy']['env']
    assert reserve['jobs']['deploy']['env']['FUNCTION_MEMORY'] == '256MB'
    assert reserve['jobs']['deploy']['env']['FUNCTION_TIMEOUT'] == '60s'


@pytest.mark.parametrize('repository,ref,event,expected', [
    ('gorpsys/autorepeater', 'refs/heads/master', 'workflow_dispatch', True),
    ('fork/autorepeater', 'refs/heads/master', 'workflow_dispatch', False),
    ('gorpsys/autorepeater', 'refs/heads/feature', 'workflow_dispatch', False),
    ('gorpsys/autorepeater', 'refs/pull/1/merge', 'pull_request', False),
    ('gorpsys/autorepeater', 'refs/heads/master', 'workflow_run', False),
])
def test_actual_gate_condition_restricts_repository_master_and_dispatch(
        workflows, repository, ref, event, expected):
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Evaluate the actual guard's restricted AND clauses without Python eval."""
    condition = workflows[1]['jobs']['gate']['if']
    clauses = [re.fullmatch(r"github\.(repository|ref|event_name) == '([^']+)'", item.strip())
               for item in condition.split('&&')]
    assert len(clauses) == 3 and all(item is not None for item in clauses)
    context = {'repository': repository, 'ref': ref, 'event_name': event}
    assert all(context[item[1]] == item[2] for item in clauses) is expected


def test_exact_sha_and_gate_order_precede_cloud_effects(workflows):
    """The same trusted helper authorizes jobs and rechecks immediately before OIDC."""
    original, reserve = workflows
    gate = reserve['jobs']['gate']
    deploy = reserve['jobs']['deploy']
    assert gate['outputs'] == original['jobs']['gate']['outputs']
    assert gate['steps'] == original['jobs']['gate']['steps']
    assert deploy['needs'] == ['gate']
    assert deploy['if'] == "${{ needs.gate.outputs.ready == 'true' }}"
    assert deploy['steps'][:2] == original['jobs']['deploy']['steps'][:2]
    assert deploy['steps'][0]['with'] == {
        'ref': '${{ needs.gate.outputs.sha }}', 'persist-credentials': False}
    assert deploy['steps'][3:] == original['jobs']['deploy']['steps'][3:]
    assert deploy['steps'][-2]['run'] == 'python3 -m scripts.deploy_gate --check-once'
    assert deploy['steps'][-1]['run'] == 'python3 -m scripts.yandex_deploy'
    assert all('id-token' not in step.get('permissions', {}) for step in gate['steps'])


@pytest.mark.parametrize('mode,expected', [
    ('vendored', ['claude-yandex-archive USE_VENDORED_SDK=1']),
    ('offline', ['claude-yandex-archive USE_VENDORED_SDK=1 OFFLINE_ARCHIVE=1']),
    ('', []), ('auto', []), ('OFFLINE', []), ('vendored offline', []),
    ('vendored; touch injected', []), ('$(touch injected)', []),
])
def test_actual_build_script_allowlist_before_any_build(workflows, tmp_path, mode, expected):
    """Execute the workflow shell with a recording Make stub; no build/network occurs."""
    step = workflows[1]['jobs']['deploy']['steps'][2]
    assert step['shell'] == 'bash'
    assert step['env'] == {'ARCHIVE_MODE': '${{ inputs.mode }}'}
    calls = tmp_path / 'calls'
    script = ('make() { printf "%s\\n" "$*" >> "$CALLS"; }\n'
              'export -f make\n' + step['run'])
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, check=False,
                            capture_output=True, text=True,
                            env={'ARCHIVE_MODE': mode, 'CALLS': str(calls)}, timeout=10)
    assert result.returncode == (0 if expected else 1)
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected
    assert not (tmp_path / 'injected').exists()
