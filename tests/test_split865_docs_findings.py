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


def test_language_docs_match_wildcard_allowlist():
    allowlist = read('tools/new_code_allowlist.txt')
    assert '**/*.py' in allowlist and '**/*.sh' in allowlist
    fragment = read('changelog.d/restore-rust-only-new-code.md')
    assert 'again refuses new Python and shell modules' not in fragment
    assert 'allowlisted' in fragment
    contribution = read('CONTRIBUTING.md')
    assert 'New production code must be Rust' not in contribution
    assert 'New Python files require a reasoned' not in contribution
    assert 'New code is Rust.' not in read('skills/phase-batch-orchestration/SKILL.md')


def test_run_readers_are_documented_read_only():
    text = read('docs/SPAWNER.md')
    assert 'Run readers persist expired' not in text
    assert 'Run listing is read-only.' in text


def test_unsandboxed_writer_requires_operator_approval():
    text = read('skills/phase-batch-orchestration/SKILL.md')
    assert '--sandbox workspace-write' in text
    assert 'explicit operator approval' in text


def test_base_waiver_docs_require_operator_proof():
    text = read('docs/SPAWNER.md')
    assert 'only card `base_decision: D-<digits>` waives known lag' not in text
    assert 'PLANGRAPH_BASE_DECISIONS_FILE' in text
    assert 'Source: operator' in text and 'Waives: spawn-base:' in text
