"""RED-FIRST property tests for plangraph/spawner/resolver.py.

Each test maps to one pathology in the current resolver:
- test_risk_monotone_intelligence -> risk floor is skipped when no candidate clears
  it and a hardcoded fallback hides the empty set; resolver.py:638-640 and
  resolver.py:656-665.
- test_reviewer_family_distinct_and_in_catalog -> review selection injects
  hardcoded mimo/muse outside the catalog instead of failing; resolver.py:497-507
  and resolver.py:551-561.
- test_exclusions_hold_in_every_fallback -> task selection returns a hardcoded
  fallback that ignores family exclusions and never raises; resolver.py:656-665.
- test_context_gate_uses_trajectory -> trajectory factor is only applied to
  price_cliff_tokens, not to context_window; resolver.py:417-425.
- test_cost_scale_invariance -> epsilon band membership depends on absolute cost
  scale, so speed tie-breaking changes rank order; resolver.py:352 and
  resolver.py:360-366.
- test_pick_stability_under_one_point -> resolve_model_for_task lacks
  relax_floor/relax_reason controls; resolver.py:594-603.
"""

from __future__ import annotations

import math
import random
import signal

import pytest

from plangraph.spawner.resolver import (
    _passes_context_and_cliff,
    detect_model_family,
    rank_candidates,
    resolve_model_for_task,
    resolve_review_plan,
)
from plangraph.spawner.risk import RISK_MIN_INTELLIGENCE

def call_with_timeout(fn, seconds=5):
    """Run fn with SIGALRM so a resolver loop cannot hang the test process."""

    def _handle_timeout(signum, frame):
        raise TimeoutError(f"call exceeded {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)

class FakeDB:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def list_models(self, active_only=False):
        return [dict(r) for r in self._rows]

def row(
    id,
    intel,
    cost=0.2,
    tier="subscription",
    speed=100.0,
    ctx=1000000,
    combos=("l2-worker", "l2-reviewer"),
    **kw,
):
    family = kw.pop("family", id.split("/", 1)[0] if "/" in id else id)
    d = {
        "id": id,
        "family": family,
        "aa_intelligence": float(intel),
        "verified_tok_per_s": float(speed),
        "cost_per_task_usd": float(cost),
        "pool_burn_weight": float(kw.pop("burn", 1.0)),
        "tier": tier,
        "available": int(kw.pop("available", 1)),
        "context_window": int(ctx),
        "combos": list(combos),
    }
    d.update(kw)
    return d

MONOTONE_TOLERANCE = 1e-9

def test_risk_monotone_intelligence():
    rng = random.Random(1234)
    catalogs = []

    # Crafted catalog: low risk filters out intel 30, normal risk includes it.
    crafted = [
        row("a/lowintel", 30, cost=0.05, speed=1000.0),
        row("b/midintel", 40, cost=3.0, speed=1.0),
        row("c/pad1", 30, cost=2.0, speed=1.0),
        row("d/pad2", 31, cost=2.0, speed=1.0),
        row("e/pad3", 32, cost=2.0, speed=1.0),
        row("f/pad4", 33, cost=2.0, speed=1.0),
    ]
    catalogs.append(crafted)

    for _ in range(99):
        n = rng.randint(6, 12)
        rows = []
        for j in range(n):
            rows.append(
                row(
                    f"rand{j}/m{j}",
                    rng.uniform(30.0, 60.0),
                    cost=rng.uniform(0.05, 3.0),
                    tier=rng.choice(["subscription", "paid", "free", "credit", "trial"]),
                    speed=rng.uniform(1.0, 300.0),
                )
            )
        catalogs.append(rows)

    risks = ["low", "normal", "high", "critical"]
    for idx, catalog in enumerate(catalogs):
        selected = {}
        for risk in risks:
            try:
                res = call_with_timeout(
                    lambda r=risk, c=catalog: resolve_model_for_task(
                        role="implement",
                        risk=r,
                        combo_name="l2-worker",
                        need_tokens=32000,
                        db=FakeDB(c),
                    )
                )
            except ValueError:
                continue
            cand = res.get("candidate", {})
            selected[risk] = float(cand.get("aa_intelligence", -1e9))

        prev = None
        prev_risk = None
        for risk in risks:
            if risk not in selected:
                continue
            intel = selected[risk]
            if prev is not None:
                assert intel >= prev - MONOTONE_TOLERANCE, (
                    f"catalog {idx}: selected intel decreased beyond "
                    f"{MONOTONE_TOLERANCE} intelligence points from "
                    f"{prev_risk} {prev} to {risk} {intel}"
                )
            prev = intel
            prev_risk = risk

        if "critical" in selected:
            min_intel = float(RISK_MIN_INTELLIGENCE["critical"])
            any_qualifies = any(
                float(r.get("aa_intelligence", 0.0)) >= min_intel
                and int(r.get("available", 1)) != 0
                for r in catalog
            )
            if any_qualifies:
                assert selected["critical"] >= min_intel - 1e-9, (
                    f"catalog {idx}: critical selected intel "
                    f"{selected['critical']} < {min_intel}"
                )


def test_reviewer_family_distinct_and_in_catalog():
    rows = [
        row("mimo/mimo-v2.6-pro", 46),
        row("claude/claude-x", 46),
        row("gemini/gemini-x", 47),
        row("qwen/qwen-x", 48),
        row("gpt/gpt-x", 49),
    ]
    plan = call_with_timeout(
        lambda: resolve_review_plan("mimo/mimo-v2.6-pro", risk="high", db=FakeDB(rows))
    )
    catalog_ids = {r["id"] for r in rows}
    reviewer_ids = [rev["model"] for rev in plan["reviewers"]]
    families = [rev["family"] for rev in plan["reviewers"]]
    writer_family = detect_model_family("mimo/mimo-v2.6-pro")

    assert all(rid in catalog_ids for rid in reviewer_ids), (
        f"reviewer id outside catalog: {reviewer_ids} vs {catalog_ids}"
    )
    assert all(f != writer_family for f in families), (
        f"writer family leaked into reviewers: {families}"
    )
    assert len(set(families)) == len(families), (
        f"reviewer families not distinct: {families}"
    )

    only_mimo = [
        row("mimo/mimo-v2.6-pro", 46),
        row("mimo/mimo-v2.6-lite", 45),
        row("mimo/mimo-v2.6-mini", 44),
    ]
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_review_plan(
                "mimo/mimo-v2.6-pro", risk="high", db=FakeDB(only_mimo)
            )
        )

def test_exclusions_hold_in_every_fallback():
    excluded_only = [
        row("claude/claude-a", 46),
        row("agy/claude-b", 47),
        row("gemini/gemini-a", 48),
        row("agy/gemini-b", 49),
    ]
    with pytest.raises(ValueError):
        call_with_timeout(
            lambda: resolve_model_for_task(
                role="implement",
                risk="normal",
                combo_name="l2-worker",
                need_tokens=32000,
                exclude_families=["claude"],
                author_model="gemini/x",
                db=FakeDB(excluded_only),
            )
        )

    relax_rows = [
        row("claude/claude-a", 46, combos=("l2-worker",)),
        row("gemini/gemini-a", 48, combos=("l2-worker",)),
        row("qwen/qwen-outside", 50, cost=0.1, combos=("l3-driver",)),
    ]
    res = call_with_timeout(
        lambda: resolve_model_for_task(
            role="implement",
            risk="normal",
            combo_name="l2-worker",
            need_tokens=32000,
            exclude_families=["claude"],
            author_model="gemini/x",
            db=FakeDB(relax_rows),
        )
    )
    selected_family = detect_model_family(res["model"])
    assert res["model"] in {r["id"] for r in relax_rows}
    assert selected_family not in {"claude", "gemini"}
    assert res["model"] == "qwen/qwen-outside"

def test_context_gate_uses_trajectory():
    m = {"context_window": 100000, "id": "a/x"}

    assert call_with_timeout(
        lambda: _passes_context_and_cliff(m, 80000, role="implement")
    ) is False
    assert call_with_timeout(
        lambda: _passes_context_and_cliff(m, 60000, role="implement")
    ) is True
    assert call_with_timeout(
        lambda: _passes_context_and_cliff(m, 90000, role="reviewer")
    ) is True
    assert call_with_timeout(
        lambda: _passes_context_and_cliff(m, 95000, role="reviewer")
    ) is False

def test_speed_scale_invariance():
    base_specs = [
        ("r1/x", 0.0100, 10.0),
        ("r2/x", 0.0175, 20.0),
        ("r3/x", 0.0250, 30.0),
        ("r4/x", 0.0325, 40.0),
        ("r5/x", 0.0400, 1000.0),
    ]

    for risk in ("low", "normal", "high", "critical"):
        orders = []
        for c in (0.1, 1.0, 10.0):
            rows = [
                row(
                    rid,
                    50.0,
                    cost=cost,
                    tier="subscription",
                    speed=speed * c,
                    combos=("l2-worker",),
                )
                for rid, cost, speed in base_specs
            ]
            ranked = call_with_timeout(
                lambda r=rows, rk=risk: rank_candidates(
                    r, role="implement", risk=rk, need_tokens=32000
                )
            )
            orders.append([m["id"] for m in ranked])
        assert orders[0] == orders[1] == orders[2], (
            f"risk {risk}: speed scaling changed rank order: {orders}"
        )


def test_pick_stability_under_one_point():
    rng = random.Random(2024)
    catalogs = []

    for i in range(200):
        rows = [
            row(
                f"c{i}/dominant",
                60.0,
                cost=0.05,
                tier="subscription",
                speed=200.0,
            )
        ]
        for j in range(5):
            rows.append(
                row(
                    f"c{i}/r{j}",
                    rng.uniform(30.0, 50.0),
                    cost=rng.uniform(1.0, 3.0),
                    tier=rng.choice(["paid", "free", "credit", "trial"]),
                    speed=rng.uniform(1.0, 100.0),
                )
            )
        catalogs.append(rows)

    flips = 0
    for rows in catalogs:
        base = call_with_timeout(
            lambda r=rows: resolve_model_for_task(
                role="implement",
                risk="normal",
                combo_name="l2-worker",
                need_tokens=32000,
                relax_floor=True,
                relax_reason="t",
                db=FakeDB(r),
            )
        )

        perturbed_rows = []
        for r in rows:
            r2 = dict(r)
            r2["aa_intelligence"] = float(r2["aa_intelligence"]) + rng.uniform(-1.0, 1.0)
            perturbed_rows.append(r2)

        pert = call_with_timeout(
            lambda r=perturbed_rows: resolve_model_for_task(
                role="implement",
                risk="normal",
                combo_name="l2-worker",
                need_tokens=32000,
                relax_floor=True,
                relax_reason="t",
                db=FakeDB(r),
            )
        )
        if base["model"] != pert["model"]:
            flips += 1

    flip_rate = flips / len(catalogs)
    assert flip_rate <= 0.10, (
        f"pick flip rate {flip_rate:.3f} ({flips}/{len(catalogs)}) exceeds 10%"
    )
