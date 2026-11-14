"""Documentation contracts for confirmed split-review findings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def test_registry_claim_names_python_scope():
    text = read('changelog.d/test-worktree-registry-guard.md')
    assert 'test-owned temporary roots' in text


def test_drive_exemption_uses_resolved_target():
    text = read('changelog.d/SEAT-RECORDS-ALWAYS-POSTABLE-S4.md')
    assert 'resolved real paths' in text
    assert 'lexical absolute paths' not in text
