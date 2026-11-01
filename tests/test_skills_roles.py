"""Guard for the portable role skills and the Field Guide seed (AO-SKILLS slice 1, D-653).

The skills are plain markdown read by any agent harness, so the only contract that
can break silently is their shape: front matter, size, the verdict line the review
role must emit, the prohibitions the implement role must state, and the repo paths
the Field Guide points at.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("research", "review", "implement")
SKILL_MAX_LINES = 150
GUIDE = ROOT / "docs" / "field-guide" / "index.md"
GUIDE_MAX_LINES = 100


def _front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "missing YAML front matter"
    out = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        assert sep, f"front matter line without a key: {line!r}"
        out[key.strip()] = value.strip()
    return out


def _skill(role):
    path = ROOT / "skills" / role / "SKILL.md"
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("role", ROLES)
def test_skill_front_matter_and_size(role):
    text = _skill(role)
    meta = _front_matter(text)
    assert meta.get("name") == role
    assert meta.get("description"), "description must be non-empty"
    assert len(text.splitlines()) <= SKILL_MAX_LINES


def test_review_skill_states_the_verdict_line():
    text = _skill("review")
    assert "VERDICT: ACCEPT|REQUEST_CHANGES" in text
    assert "SEVERITY file:line - problem - fix" in text
    for level in ("HIGH", "MED", "LOW"):
        assert re.search(rf"^- \*\*{level}\*\*", text, re.M), f"no definition of {level}"


def test_implement_skill_forbids_spawn_push_merge():
    text = _skill("implement").lower()
    for verb in ("spawn", "push", "merge"):
        assert re.search(rf"never[^.\n]*\b{verb}", text), f"implement skill must forbid {verb}"


def test_research_skill_states_labels_and_template():
    text = _skill("research")
    for label in ("verified", "sourced", "inferred"):
        assert label in text
    for heading in ("Question", "Findings", "Contradictions", "Unknowns", "Next steps"):
        assert re.search(rf"^{heading}\b", text, re.M), f"template lacks {heading}"


def test_field_guide_size():
    assert GUIDE.is_file()
    assert len(GUIDE.read_text(encoding="utf-8").splitlines()) <= GUIDE_MAX_LINES


def _repo_paths(text):
    """Backticked tokens that look like a file path (no spaces, a slash or a file extension)."""
    for token in re.findall(r"`([^`\n]+)`", text):
        if re.fullmatch(r"[\w./-]+", token) and ("/" in token or re.search(r"\.\w{1,4}$", token)):
            yield token


def test_field_guide_paths_exist():
    paths = sorted(set(_repo_paths(GUIDE.read_text(encoding="utf-8"))))
    assert paths, "the guide must point at repo paths"
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert not missing, f"field guide points at paths that do not exist: {missing}"


def test_repo_paths_extractor_skips_commands():
    got = list(_repo_paths("`uv run --no-project x` `tools/a.py` `fcntl` `CHANGELOG.md`"))
    assert got == ["tools/a.py", "CHANGELOG.md"]
