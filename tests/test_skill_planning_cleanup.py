"""plangraph-planning names no swarm-orchestration path and no persistent L1/L2 session (RT-P4-SKILLS-REWRITE)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "skills" / "plangraph-planning").rglob("*.md"))
PERSISTENT = re.compile(
    r"(?:persistent|long[- ]lived|long[- ]running|long[- ]term|always[- ]on)\s+(?:L1|L2)"
    r"|(?:L1|L2)\s+sessions?\s+(?:stays?|persists?|keeps?\s+running)",
    re.IGNORECASE)


def test_skill_files_found():
    assert (ROOT / "skills" / "plangraph-planning" / "SKILL.md") in FILES


def test_no_swarm_orchestration_reference():
    bad = [f.name for f in FILES if "swarm-orchestration" in f.read_text(encoding="utf-8")]
    assert not bad, bad


def test_no_persistent_l1_l2_session():
    bad = [(f.name, m.group(0)) for f in FILES for m in PERSISTENT.finditer(f.read_text(encoding="utf-8"))]
    assert not bad, bad


def test_persistent_pattern_matches_known_bad_phrases():
    for text in ("persistent L2", "long-lived L1", "long-running L2 session", "always-on L2",
                 "the L2 session keeps running", "L1 sessions persist"):
        assert PERSISTENT.search(text), text
