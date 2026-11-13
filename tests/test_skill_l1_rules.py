"""L1 rules have a disposition and one copy in the orchestrator card."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("rule_id, phrases", [
    ("R-lead-01", ("An L1 takes its own named default at once and reports it.",
                   "It waits for the router only for GO rows, waivers and router-only keeper verbs.")),
    ("R-lead-02", ("When a route leg fails, the L1 tries the next leg the spawner route offers "
                   "for that role before sending BLOCKED.",
                   "If the card or order names one model, rule 11 forbids swaps "
                   "and the L1 backs off or sends BLOCKED.")),
    ("R-lead-03", ("Every seat card carries its item key.",)),
    ("R-lead-04", ("After two design rounds with no agreement, the L1 cuts the item smaller.",)),
    ("R-lead-05", ("A fix for a live failure is done only after a live proof run.",)),
    ("R-lead-06", ("Before any statement about seat families the L1 calls the spawner `route` verb.",)),
    ("R-lead-07", ("No worker, seat or session starts on a base behind the origin default branch.",
                   "Fetch first", "cut the worktree at the fetched origin default head",
                   "name the base sha in the card.", "A stale base or a failed fetch stops the spawn.")),
])
def test_l1_rule_disposition_and_single_card_copy(rule_id, phrases):
    table = (ROOT / "docs/skills/rule-disposition.md").read_text(encoding="utf-8")
    card = (ROOT / "skills/unattended-orchestration/cards/orchestrator.md").read_text(encoding="utf-8")
    rows = [[cell.strip() for cell in line.strip("|").split("|")]
            for line in table.splitlines() if line.startswith("| R-")]
    matches = [row for row in rows if row[0] == rule_id]
    assert len(matches) == 1, rule_id
    assert matches[0][1:3] == ["card", "role card: orchestrator"]
    assert card.count(f"{rule_id}: ") == 1
    rule = re.split(r"\n(?:R-[\w-]+:|#{1,6} )", card.split(f"{rule_id}: ", 1)[1], maxsplit=1)[0]
    rule = rule.split("(why:", 1)[0]
    for phrase in phrases:
        assert " ".join(card.split()).count(phrase) == 1, phrase
        assert " ".join(rule.split()).count(phrase) == 1, (rule_id, phrase)
        for name in ("SKILL.md", "cards/planner.md", "cards/worker.md", "cards/reviewer.md"):
            other = ROOT / "skills/unattended-orchestration" / name
            assert phrase not in " ".join(other.read_text(encoding="utf-8").split()), (name, phrase)
    assert ";" not in rule and "\u2014" not in rule
