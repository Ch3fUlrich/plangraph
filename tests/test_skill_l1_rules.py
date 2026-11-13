"""L1 rules have a disposition and one copy in the orchestrator card."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("rule_id, phrase", [
    ("R-lead-01", "takes its own named default at once and reports it"),
    ("R-lead-02", "tries the next route leg before it sends BLOCKED"),
    ("R-lead-03", "Every seat card carries its item key."),
    ("R-lead-04", "After two design rounds with no agreement"),
    ("R-lead-05", "done only after a live proof run"),
    ("R-lead-06", "Before any statement about seat families"),
    ("R-lead-07", "A stale base or a failed fetch stops the spawn."),
])
def test_l1_rule_disposition_and_single_card_copy(rule_id, phrase):
    table = (ROOT / "docs/skills/rule-disposition.md").read_text(encoding="utf-8")
    card = (ROOT / "skills/unattended-orchestration/cards/orchestrator.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip("|").split("|")]
            for line in table.splitlines() if line.startswith("| R-")]
    matches = [row for row in rows if row[0] == rule_id]
    assert len(matches) == 1, rule_id
    assert matches[0][1:3] == ["card", "role card: orchestrator"]
    assert card.count(f"{rule_id}: ") == 1
    assert " ".join(card.split()).count(phrase) == 1
    rule = card.split(f"{rule_id}: ", 1)[1].split("(why:", 1)[0]
    assert ";" not in rule and "\u2014" not in rule
