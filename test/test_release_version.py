"""Release versions are explicit, monotonic and bound to immutable commits."""
from pathlib import Path
import subprocess
import runpy
import sys
from unittest.mock import create_autospec

import pytest

from scripts import release_version as release


def notes(version='1.1.0'):
    """Use the same release heading format as the project's CHANGELOG."""
    return f'## Не выпущено\n\n## [{version}](https://example.com) - 2026-10-14\n\n- Change.\n'


def git(root, *arguments):
    """All integration repositories and remotes are local temporary directories."""
    return subprocess.check_output(
        ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', *arguments],
        cwd=root, text=True, stderr=subprocess.PIPE, timeout=10).strip()


@pytest.fixture(name='repository')
def release_repository(tmp_path):
    """Prepare an existing release and a candidate without a network connection."""
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'autorepeater').mkdir()
    (root / 'autorepeater/__init__.py').write_text('__version__ = "1.0.0"\n')
    (root / 'CHANGELOG.md').write_text(notes('1.0.0'))
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    git(root, 'tag', 'v1.0.0')
    (root / 'autorepeater/__init__.py').write_text('__version__ = "1.1.0"\n')
    (root / 'CHANGELOG.md').write_text(notes())
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'candidate')
    return root, base


@pytest.mark.parametrize('value', ['1.2.3', '0.0.0', '10.2.0'])
def test_numeric_versions(value):
    """Each component is a decimal integer."""
    assert release.parse_version(value) == tuple(map(int, value.split('.')))


@pytest.mark.parametrize('value', [
    '', 'v1.2.3', '1.2', '01.2.3', '1.2.3-rc.1', '1.2.3+git', '-1.0.0',
])
def test_non_release_versions_rejected(value):
    """Only the agreed stable X.Y.Z release format is accepted."""
    with pytest.raises(release.ReleaseError):
        release.parse_version(value)


@pytest.mark.parametrize('text', ['', '## Не выпущено\n- Pending\n', notes('01.2.3'),
                                      notes().replace('## [', '## Release ['),
                                      notes().replace('2026-10-14', '2026-02-30'),
                                      notes().replace('- Change.', '')])
def test_missing_or_invalid_release_rejected(text):
    """A placeholder, malformed heading or empty release cannot authorize a merge."""
    with pytest.raises(release.ReleaseError):
        release.read_release(text)


def test_release_ignores_fenced_examples_and_unreleased_section():
    """Documentation examples do not select a candidate version."""
    result = release.read_release('```markdown\n' + notes('9.0.0') + '```\n' + notes())
    assert result.version == (1, 1, 0)
    assert result.name == '1.1.0'
    assert result.date == '2026-10-14'
    assert result.notes == '- Change.'
    assert release.read_release('## Не выпущено\n', required=False) is None


def test_candidate_must_exceed_all_tags_numerically(repository):
    """The comparison includes all release tags and handles two-digit components."""
    root, base = repository
    assert release.validate(root, base=base)[0].name == '1.1.0'
    git(root, 'tag', 'v1.10.0')
    with pytest.raises(release.ReleaseError, match='greater'):
        release.validate(root, base=base)


@pytest.mark.parametrize('candidate', ['1.0.1', '1.1.0'])
def test_base_version_also_protects_against_delayed_tag_creation(repository, candidate):
    """Two PRs cannot reuse a merged version while its tag is still pending."""
    root, _ = repository
    base = git(root, 'rev-parse', 'HEAD')
    (root / 'CHANGELOG.md').write_text(notes(candidate))
    (root / 'autorepeater/__init__.py').write_text(f'__version__ = "{candidate}"\n')
    with pytest.raises(release.ReleaseError, match='greater'):
        release.validate(root, base=base)


def test_delayed_master_job_can_tag_before_an_already_tagged_descendant(repository):
    """Publication order does not move older tags onto newer commits."""
    root, _ = repository
    candidate = git(root, 'rev-parse', 'HEAD')
    git(root, 'commit', '--allow-empty', '-qm', 'later merge')
    git(root, 'tag', 'v1.2.0')
    git(root, 'checkout', '--detach', candidate)
    with pytest.raises(release.ReleaseError, match='greater'):
        release.validate(root)
    assert release.validate(root, allow_existing=True)[0].name == '1.1.0'


def test_current_tag_allowed_only_for_same_commit_and_master_retry(repository):
    """A rerun is safe only when the tag already identifies this exact commit."""
    root, _ = repository
    git(root, 'tag', 'v1.1.0')
    with pytest.raises(release.ReleaseError):
        release.validate(root)
    assert release.validate(root, allow_existing=True)[2]
    git(root, 'commit', '--allow-empty', '-qm', 'different commit')
    with pytest.raises(release.ReleaseError, match='different commit'):
        release.validate(root, allow_existing=True)


@pytest.mark.parametrize('value', [
    '__version__ = "2.0.0"', '__version__ = get_version()', '',
    '__version__ = "1.1.0"\n__version__ = "1.1.0"',
])
def test_application_and_changelog_versions_must_match(repository, value):
    """Missing, dynamic, repeated or mismatched CLI versions are rejected."""
    root, _ = repository
    (root / 'autorepeater/__init__.py').write_text(value)
    with pytest.raises(release.ReleaseError):
        release.validate(root)


def test_bootstrap_base_without_release_heading_and_unrelated_tags(repository):
    """The first versioned PR still works against an older unversioned CHANGELOG."""
    root, _ = repository
    (root / 'CHANGELOG.md').write_text('## Не выпущено\n- Historical work\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'old changelog format')
    base = git(root, 'rev-parse', 'HEAD')
    (root / 'CHANGELOG.md').write_text(notes())
    git(root, 'tag', 'unrelated')
    assert release.validate(root, base=base)[0].name == '1.1.0'


def test_base_without_changelog_and_git_failures(repository):
    """An absent historic CHANGELOG is allowed, but failed Git commands are not."""
    root, _ = repository
    git(root, 'rm', 'CHANGELOG.md')
    git(root, 'commit', '-qm', 'no changelog')
    base = git(root, 'rev-parse', 'HEAD')
    (root / 'CHANGELOG.md').write_text(notes())
    assert release.validate(root, base=base)[0].name == '1.1.0'
    with pytest.raises(release.ReleaseError, match='git command failed'):
        release.git(root, 'invalid-command')


def test_unreadable_release_files_fail_safely(tmp_path, capsys):
    """Configuration errors do not produce success or a raw traceback."""
    assert release.main(['check', '--project-root', str(tmp_path)]) == 1
    assert 'release files unavailable' in capsys.readouterr().err


def test_annotated_tag_publication_and_idempotent_retry(repository, tmp_path):
    """A real local remote receives exactly the candidate commit and notes."""
    root, _ = repository
    remote = tmp_path / 'remote.git'
    remote.mkdir()
    git(remote, 'init', '--bare', '-q')
    git(root, 'remote', 'add', 'origin', str(remote))
    assert release.main(['tag', '--project-root', str(root)]) == 0
    sha = git(root, 'rev-parse', 'HEAD')
    assert git(root, 'rev-parse', 'v1.1.0^{commit}') == sha
    assert git(remote, 'rev-parse', 'v1.1.0^{commit}') == sha
    assert git(root, 'cat-file', '-t', 'v1.1.0') == 'tag'
    assert '- Change.' in git(root, 'cat-file', 'tag', 'v1.1.0')
    assert release.main(['tag', '--project-root', str(root)]) == 0


def test_main_check_and_safe_failure(repository, capsys):
    """The command returns actionable success and failure exit codes."""
    root, base = repository
    assert release.main(['check', '--project-root', str(root), '--base', base]) == 0
    assert '1.1.0' in capsys.readouterr().out
    assert release.main(['check', '--project-root', str(root), '--base', 'bad-ref']) == 1
    assert 'Release rejected' in capsys.readouterr().err


def test_script_entrypoint_checks_without_publishing(repository, monkeypatch):
    """Running the module as a script preserves check-only behavior."""
    root, _ = repository
    monkeypatch.setattr(sys, 'argv', ['release_version', 'check', '--project-root', str(root)])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(release.__file__, run_name='__main__')
    assert raised.value.code == 0
    assert 'v1.1.0' not in git(root, 'tag', '--list').splitlines()


def test_git_failures_do_not_expose_raw_output(monkeypatch, tmp_path):
    """Git timeouts are sanitized instead of leaking captured output."""
    failing = create_autospec(subprocess.run, spec_set=True,
                              side_effect=subprocess.TimeoutExpired(['git'], 30))
    monkeypatch.setattr(release.subprocess, 'run', failing)
    with pytest.raises(release.ReleaseError, match='git command failed'):
        release.git(tmp_path, 'status')


def test_workflows_check_version_before_dependencies_and_only_tag_master():
    """PR validation is mandatory and read-only; only master can publish tags."""
    root = Path(__file__).resolve().parents[1]
    checks = (root / '.github/workflows/python-app.yml').read_text()
    assert checks.index('scripts.release_version check') < checks.index('make setup')
    assert 'name: Offline checks' in checks and 'contents: read' in checks
    assert 'VERSION_BASE_SHA: ${{ github.event.pull_request.base.sha }}' in checks
    workflow = (root / '.github/workflows/release-tag.yml').read_text()
    assert 'pull_request' not in workflow
    assert 'branches: [ master ]' in workflow
    assert "github.ref == 'refs/heads/master'" in workflow
    assert 'contents: write' in workflow and 'fetch-depth: 0' in workflow
    assert 'ref: ${{ github.sha }}' in workflow
    assert 'cancel-in-progress: false' in workflow and 'queue: max' in workflow
    assert 'scripts.release_version tag' in workflow
    assert '--force' not in workflow and 'secrets.' not in workflow
