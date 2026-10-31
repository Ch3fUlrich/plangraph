"""Tier/catalog gate contracts (G3, D-3258/D-3264).

Catalog rows with ``tier == "credit"`` take the same expiry gate as credit
legs (no policy entry or expired -> excluded, never scored at top); the
non-credit tiers keep their base 41e7b729 relative order; the SPAWNER.md
tier table is checked against the code.
"""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from plangraph.planner.route_policy import RoutePolicy
from plangraph.routing_support.catalog_seed import TIER_RUNG
from plangraph.routing_support.credit_gate import (
    catalog_credit_skip_reason,
    clear_policy_cache,
)
from plangraph.routing_support.resolver import (
    rank_candidates,
    score_model,
    select_top_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
LEG_MODEL = "console-anthropic/claude-sonnet-5-5"


def _row(tier="credit", model=LEG_MODEL, **extra):
    row = {
        "id": model,
        "model_id": model,
        "tier": tier,
        "aa_intelligence": 50.0,
        "cost_per_task_usd": 0.20,
        "pool_burn_weight": 1.0,
        "verified_tok_per_s": 100.0,
        "context_window": 128000,
        "available": 1,
        "active": True,
    }
    row.update(extra)
    return row


@pytest.mark.parametrize(
    "entry,usable",
    [
        (None, False),  # no policy entry at all
        ({}, False),  # no expires
        ({"expires": "nov"}, False),  # unparsable: fail closed
        ({"expires": None}, False),
        ({"expires": "2026-11-29"}, False),  # before last usable day
        ({"expires": "2026-11-30"}, True),  # expiry day itself is usable
        ({"expires": "2026-12-01"}, True),
    ],
)
def test_credit_catalog_row_gets_leg_expiry_gate(entry, usable):
    """Catalog credit rows take exactly the leg expiry verdict (C1)."""
    from plangraph.planner.legs import DEFAULT_LEGS

    leg = next(l for l in DEFAULT_LEGS if l.tier == "credit" and l.model == LEG_MODEL)
    credit_tiers = {} if entry is None else {"console-anthropic": entry}
    policy = RoutePolicy(credit_tiers=credit_tiers, today=date(2026, 11, 30))
    reason = catalog_credit_skip_reason(_row(), policy)
    assert (reason is None) == usable
    # Same gate as the credit leg, verdict for verdict.
    assert reason == policy.skip_reason(leg, "implement")


def test_non_credit_rows_never_gate():
    policy = RoutePolicy(credit_tiers={}, today=date(2026, 11, 30))
    for tier in ("trial", "free", "subscription", "paid"):
        assert catalog_credit_skip_reason(_row(tier=tier), policy) is None
    assert catalog_credit_skip_reason({}, policy) is None


def _set_policy(tmp_path, monkeypatch, expires):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"credit_tiers": {"console-anthropic": {"expires": expires}}}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    clear_policy_cache()


def test_credit_row_without_live_credit_is_not_scored_top(tmp_path, monkeypatch):
    """Without live credit the row is excluded, not top-preference (C1)."""
    credit = _row()
    trial = _row(tier="trial", model="agy/trial-model")
    # Ungated, the credit bonus (+14.0) beats trial (+10.5).
    assert score_model(credit) > score_model(trial)
    _set_policy(tmp_path, monkeypatch, "2000-01-01")  # expired
    ranked = rank_candidates([credit, trial], role="implement", risk="normal")
    assert [r["id"] for r in ranked] == [trial["id"]]
    assert select_top_candidate([credit, trial])["id"] == trial["id"]


def test_credit_row_missing_policy_entry_is_not_scored_top(tmp_path, monkeypatch):
    credit = _row()
    trial = _row(tier="trial", model="agy/trial-model")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"credit_tiers": {}}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    clear_policy_cache()
    assert select_top_candidate([credit, trial])["id"] == trial["id"]


def test_credit_row_with_live_credit_tops_again(tmp_path, monkeypatch):
    credit = _row()
    trial = _row(tier="trial", model="agy/trial-model")
    _set_policy(tmp_path, monkeypatch, "2999-01-01")
    assert select_top_candidate([credit, trial])["id"] == credit["id"]
    assert [r["id"] for r in rank_candidates([credit, trial])] == [credit["id"], trial["id"]]


def test_expired_credit_catalog_row_excluded_on_automatic_path(tmp_path, monkeypatch):
    """resolve_model_for_task never picks an expired-credit catalog row (C1)."""
    from plangraph.routing_support.resolver import resolve_model_for_task

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows

        def list_models(self, active_only=False):
            return [dict(r) for r in self._rows]

    credit = _row()
    trial = _row(tier="trial", model="agy/trial-model")
    reader = lambda *a, **k: {"state": "ok", "evidence": {}}
    _set_policy(tmp_path, monkeypatch, "2999-01-01")
    picked = resolve_model_for_task(
        role="implement", risk="normal", combo_name="l2-worker",
        db=FakeDB([credit, trial]), leg_state_reader=reader,
    )
    assert picked["model"] == credit["id"]
    _set_policy(tmp_path, monkeypatch, "2000-01-01")
    picked = resolve_model_for_task(
        role="implement", risk="normal", combo_name="l2-worker",
        db=FakeDB([credit, trial]), leg_state_reader=reader,
    )
    assert picked["model"] == trial["id"]


def test_non_credit_tier_order_matches_base():
    """Base 41e7b729 contract: W_tier order trial > free > subscription > paid.

    The G3 ladder moved only credit (rung 2 -> 0); the non-credit tiers keep
    their relative order against the base contract.
    """
    scores = {t: score_model(_row(tier=t, model=f"agy/{t}"))
              for t in ("trial", "free", "subscription", "paid")}
    assert scores["trial"] > scores["free"] > scores["subscription"] > scores["paid"]
    rungs = [TIER_RUNG[t] for t in ("trial", "free", "subscription", "paid")]
    assert rungs == sorted(rungs)
    ranked = rank_candidates([_row(tier=t, model=f"agy/{t}")
                              for t in ("paid", "subscription", "free", "trial")])
    assert [r["tier"] for r in ranked] == ["trial", "free", "subscription", "paid"]


def test_spawner_docs_tier_table_matches_code():
    """The SPAWNER.md W_tier table is checked against TIER_RUNG and score_model."""
    text = (ROOT / "docs" / "SPAWNER.md").read_text(encoding="utf-8")
    rows = re.findall(r"^- `([a-z]+)` \(rung (\d+)\): \$([+-]?\d+\.\d+)\$?$", text, re.M)
    assert len(rows) == 5, "SPAWNER.md must list the 5-tier W_tier table"
    assert {tier for tier, _, _ in rows} == set(TIER_RUNG)
    for tier, rung, weight in rows:
        assert int(rung) == TIER_RUNG[tier], f"{tier}: rung drifts from TIER_RUNG"
        assert float(weight) == (4 - int(rung)) * 3.5, f"{tier}: weight drifts from formula"
        # Against the scoring code: W_tier is the score delta vs paid.
        bonus = score_model(_row(tier=tier, model=f"agy/{tier}")) - score_model(
            _row(tier="paid", model="agy/paid"))
        assert float(weight) == pytest.approx(bonus), f"{tier}: weight drifts from score_model"
    # The prose "5-Rung Tier Priority Order" table must not contradict TIER_RUNG
    # either: a reader copying rungs from it must get the code's order.
    section = text.split("## 4. 5-Rung Tier Priority Order", 1)[1].split("###", 1)[0]
    table = re.findall(r"^\|\s*\*?\*?(\d)\*?\*?\s*\|\s*`([a-z]+)`", section, re.M)
    assert len(table) == 5, "SPAWNER.md must show the 5-rung priority table"
    assert {tier for _, tier in table} == set(TIER_RUNG)
    for rung, tier in table:
        assert int(rung) == TIER_RUNG[tier], f"{tier}: priority table rung drifts from TIER_RUNG"
