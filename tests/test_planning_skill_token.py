"""Hermetic guards for graph-first planning and actionable brief wording."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/plangraph-planning/SKILL.md"
CONTRACT = SKILL.parent / "references/task-contract.md"


def test_plan_graph_questions_then_research():
    text = SKILL.read_text()
    phrases = (
        "0. Read the plan graph first: the item text and the docs it names; ask only what the plan leaves open.",
        "operator question batch covering scope, acceptance, constraints and risks",
        "each question options, default first",
        "one follow-up round on conflicts before research",
        "Spawn researchers by card via plangraph-spawn in the route table's order, from different providers",
        "2-3 prompt angles; each researcher returns at most 15 bullets plus sources",
        "static token cap on the card",
    )
    positions = [text.index(phrase) for phrase in phrases]
    assert positions == sorted(positions)


def test_routing_effort_review_and_size():
    text = SKILL.read_text()
    assert not re.search(r"\b(?:Sonnet|Opus|Haiku|GPT[- ]?\d|Gemini[- ]?\d)\b", text, re.I)
    assert "high effort" not in text
    assert "Effort is medium unless the operator asks otherwise." in text
    assert "one review seat of a non-Claude family different from the writer" in text
    assert "the route table decides" in text
    assert "registry policy block" in text and "docs/PLANNER-KIT.md" in text
    assert len(text.splitlines()) < 500


def test_contract_fields_and_wording_tables():
    text = CONTRACT.read_text()
    for field in ("Why:", "Unblocks:", "Last attempt:"):
        assert re.search(rf"^{field}\s+<", text, re.M)
    wording = text.split("## Wording\n", 1)[1].split("## Orchestrator briefs", 1)[0]
    default, avoid = wording.split("### Default text\n", 1)[1].split("### Avoid\n")
    assert len([line for line in default.splitlines() if line.startswith("|")]) == 10
    assert len([line for line in avoid.splitlines() if line.startswith("|")]) == 11
    for purpose in ("Why", "Way out", "Integrity", "Honest report", "Disagreement", "Retry", "Handoff", "Feedback"):
        assert f"| {purpose} |" in default
    assert "Facts only; blameless" in default
    assert "That does not transfer directly to prompts at run time" in avoid
    assert "not an exit shipped by this contract" in wording


def test_no_exit_footer():
    for path in (SKILL, CONTRACT):
        assert "PG_QUESTION" not in path.read_text()


def test_evals_are_single_line_skill_rules():
    cases = json.loads((SKILL.parent / "evals/evals.json").read_text())
    assert {case["id"] for case in cases} >= {
        "plangraph-planning-graph-first", "plangraph-planning-brief-purpose-retry",
    }
    lines = SKILL.read_text().lower().splitlines()
    for case in cases:
        for phrase in case["must"]:
            assert any(phrase.lower() in line for line in lines)
