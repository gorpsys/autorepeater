"""The first actual result defines a dynamic suite whose originals must remain valid."""
import json
import xml.etree.ElementTree as ET
from test.test_retry_evidence import SHA, write_attempt, write_history

import pytest

from scripts.retry_evidence import inspect_history, verify_history
from scripts.retry_settings import RunSettings


@pytest.mark.parametrize('size', [2, 5, 25])
@pytest.mark.parametrize('retry', [False, True])
def test_dynamic_first_attempt_and_legal_failed_subset(tmp_path, size, retry):
    """Nested modules, classes and parameter IDs need no separate collection inventory."""
    settings = RunSettings()
    nodes = tuple(f'e2e/group_{index}/test_cases.py::TestCases::test_case[a::b]'
                  for index in range(size))
    entries = [write_attempt(tmp_path, 1, {
        node: ('failed', True) if retry and index == 0 else ('passed', False)
        for index, node in enumerate(nodes)})]
    if retry:
        entries.append(write_attempt(tmp_path, 2, {nodes[0]: ('passed', False)}))
    states = dict.fromkeys(nodes, 'passed')
    write_history(tmp_path, entries, states, settings)
    assert inspect_history(tmp_path, settings, expected_sha=SHA) == states
    assert verify_history(tmp_path, settings, expected_sha=SHA) == size


@pytest.mark.parametrize('field', ['entered', 'cleaned'])
def test_duplicate_fixture_proof_is_rejected(tmp_path, field):
    """A repeated fixture marker cannot substitute for a distinct finished lifecycle."""
    settings = RunSettings()
    node = 'e2e/test_cases.py::test_case'
    entries = [write_attempt(tmp_path, 1, {node: ('passed', False)})]
    path = tmp_path / 'attempts/01/result.json'
    report = json.loads(path.read_text())
    report[field] *= 2
    path.write_text(json.dumps(report))
    write_history(tmp_path, entries, {node: 'passed'}, settings)
    with pytest.raises(ValueError, match='duplicate'):
        verify_history(tmp_path, settings, expected_sha=SHA)


def test_duplicate_final_junit_cannot_hide_behind_valid_originals(tmp_path):
    """The final merged XML has the same unique-ID contract as every original attempt."""
    settings = RunSettings()
    node = 'e2e/test_cases.py::test_case'
    entries = [write_attempt(tmp_path, 1, {node: ('passed', False)})]
    write_history(tmp_path, entries, {node: 'passed'}, settings)
    path = tmp_path / 'junit.xml'
    tree = ET.parse(path)
    tree.getroot().append(tree.getroot()[0])
    tree.getroot().set('tests', '2')
    tree.write(path)
    with pytest.raises(ValueError, match='duplicate'):
        verify_history(tmp_path, settings, expected_sha=SHA)
