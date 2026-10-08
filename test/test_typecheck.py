"""Prove the real type checker rejects invalid use of our evidence DTOs."""
from pathlib import Path
import subprocess
import sys


def test_mypy_rejects_invalid_dto_field_type(tmp_path: Path) -> None:
    """Use repository settings, not a mock or weakened checker configuration."""
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / 'invalid_evidence.py'
    source.write_text(
        'from scripts.sandbox_evidence import MoneyEvidence\n'
        'evidence = MoneyEvidence(currency="rub", amount=42)\n', encoding='utf-8')

    result = subprocess.run(
        [sys.executable, '-m', 'mypy', '--config-file', str(root / 'pyproject.toml'),
         '--cache-dir', str(tmp_path / 'cache'), str(source)],
        cwd=root, capture_output=True, text=True, check=False, timeout=60)

    assert result.returncode == 1, result.stdout + result.stderr
    assert '[arg-type]' in result.stdout
    assert 'MoneyEvidence' in result.stdout
    assert 'expected "str"' in result.stdout
