"""Required and manual package smoke share Make and never access trading credentials."""
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def package_step(text):
    """Read the single package step without requiring a new YAML dependency."""
    steps = re.split(r'^    - ', text, flags=re.MULTILINE)
    matches = [s for s in steps if 'run: make check-cloud-package' in s]
    assert len(matches) == 1
    return matches[0]


def test_required_smoke_runs_after_one_coverage_without_skip_or_allow_failure():
    """The existing required identity now includes an explicit network package stage."""
    text = (ROOT / '.github/workflows/python-app.yml').read_text()
    assert 'name: Offline checks' in text
    assert text.count('run: make coverage') == 1
    assert text.index('run: make coverage') < text.index('run: make check-cloud-package')
    step = package_step(text)
    assert 'if:' not in step and 'continue-on-error' not in step
    assert 'timeout-minutes: 20' in step
    assert 'CLOUD_PACKAGE_SHA: ${{ github.sha }}' in step
    assert 'ref: ${{ github.sha }}' in text and 'persist-credentials: false' in text
    assert 'pytest' not in step and 'cloud_dependencies' not in step


def test_manual_job_is_distinct_dispatch_only_pinned_and_secret_free():
    """A manual result cannot impersonate Offline checks or Sandbox E2E."""
    text = (ROOT / '.github/workflows/cloud-package-check.yml').read_text()
    assert 'workflow_dispatch:' in text
    assert all(trigger not in text for trigger in ('pull_request:', 'push:', 'workflow_run:'))
    assert 'name: Cloud package smoke' in text
    assert 'Offline checks' not in text and 'Sandbox E2E' not in text
    assert 'contents: read' in text and ': write' not in text and 'secrets.' not in text
    assert 'ref: ${{ github.sha }}' in text and 'persist-credentials: false' in text
    step = package_step(text)
    assert 'CLOUD_PACKAGE_SHA: ${{ github.sha }}' in step
    assert 'timeout-minutes: 20' in step
    assert 'if:' not in step and 'continue-on-error' not in text
    assert 'make setup' not in text and 'make coverage' not in text
    assert 'python-version: "3.12"' in text
    assert 'deploy' not in text and 'sandbox_token' not in text


@pytest.mark.parametrize('status', [0, 7])
def test_both_workflows_propagate_make_failure_without_shell_pipeline(tmp_path, status):
    """Execute the actual shared step with a recording shell function, without packages."""
    for name in ('python-app', 'cloud-package-check'):
        step = package_step((ROOT / f'.github/workflows/{name}.yml').read_text())
        command = re.search(r'^      run: (.+)$', step, re.MULTILINE)[1]
        result = subprocess.run(['bash', '-c',
                                 'make() { printf "%s\\n" "$*"; return "$STATUS"; }; ' + command],
                                cwd=tmp_path, env={'STATUS': str(status)}, check=False,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == status
        assert result.stdout == 'check-cloud-package\n'
