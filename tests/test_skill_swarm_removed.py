"""No plangraph skill or hierarchy doc points at the retired swarm-orchestration skill (RT-P4-SKILLS-REWRITE)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_swarm_orchestration_pointer():
    files = [*sorted((ROOT / "skills").rglob("*.md")), ROOT / "docs" / "PLANNING-HIERARCHY.md"]
    bad = [str(f.relative_to(ROOT)) for f in files if "swarm-orchestration" in f.read_text(encoding="utf-8")]
    assert not bad, bad


def test_orchestration_caps_documented():
    text = (ROOT / "docs" / "PLANNER-KIT.md").read_text(encoding="utf-8")
    assert "## Orchestration caps" in text
    assert "max_review_loops" in text
