"""Keep Windows fallback claims bounded by their actual evidence."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preparation_fallback_docs_distinguish_injected_seams_from_native_refusals():
    text = " ".join((ROOT / "docs/turn-budget-hook.md").read_text().split())
    start = text.index("Preparation has separate bounded fallbacks")
    end = text.index("Every unexpected failed call", start)
    evidence = text[start:end]
    for fallback in (
        "Absolute/canonical-parent", "DELETE/plain-create", "write-reopen",
        "metadata-open", "permission-path", "sync-reopen",
    ):
        assert fallback in evidence
    assert "proven only by injected seams, not native OS refusals" in evidence
    assert "combined preparation failures are also proven only by injected seams" in evidence
    assert "on Linux the final publication uses `fs::rename`, not a Windows rename API" in evidence
    assert "native Windows call site of the historical error 87 remains unidentified" in evidence
    assert "Cross-compilation proves type correctness, not native refusal or OS acceptance" in evidence


def test_native_workstation_acceptance_is_not_claimed_by_builder_docs():
    text = " ".join((ROOT / "docs/turn-budget-hook.md").read_text().split())
    assert "Windows workstation acceptance (not executed on the Linux builder)" in text
    assert "complete 20 native workstation runs" in text
    assert "no Windows run ID or completed proof is claimed here" in text
