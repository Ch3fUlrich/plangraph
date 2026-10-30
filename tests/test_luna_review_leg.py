"""Review leg cx-gpt-6-luna (D-2983/D-2984): a gpt-family review seat, never a writer.

The leg joins DEFAULT_LEGS right after cx-gpt-6.1-sol and is excluded from any
review routed for a PR written by its own family (D-2511 family exclusion).
"""
from __future__ import annotations

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.routing import plan_spawn

CARD = {"estimate_tokens": 20000, "risk_tags": []}
LUNA = "cx-gpt-6-luna"


def test_luna_leg_in_default_legs():
    leg = next((leg for leg in DEFAULT_LEGS if leg.id == LUNA), None)
    assert leg is not None
    assert (leg.family, leg.harness, leg.model, leg.roles) == (
        "gpt", "opencode", "omniroute/cx-gpt-6-luna", ("review",))
    ids = [leg.id for leg in DEFAULT_LEGS]
    assert ids[ids.index("cx-gpt-6.1-sol") + 1] == LUNA


def test_luna_never_writes():
    assert any(leg.id == LUNA for leg in DEFAULT_LEGS)  # precondition: the leg exists
    assert LUNA not in WRITER_ORDER
    for estimate in (1000, 20000, 100000):
        plan = plan_spawn({"estimate_tokens": estimate}, role="implement")
        assert LUNA not in [leg.id for leg in plan.legs]
        assert LUNA not in [c["leg"] for c in plan.candidates]


def test_luna_excluded_for_gpt_written_prs():
    # D-2511: a sol-written PR (writer_families ("gpt",)) never picks luna as a
    # reviewer.  The skipped record proves it is the family rule doing the work,
    # not the leg's position in the list.
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",))
    assert LUNA not in [leg.id for leg in plan.legs]
    assert LUNA not in [leg.id for leg in plan.review_seats]
    assert any(s["leg"] == LUNA and s["reason"] == "excluded_family"
               for s in plan.skipped)


def test_luna_eligible_for_other_written_prs():
    # luna is inline-only (SPAWNER-REGISTRY-ROUTE): routable once the card names the diff.
    card = dict(CARD, diff_ref={"repo": "/srv/repo", "base": "a1", "head": "b2"})
    plan = plan_spawn(card, role="review", writer_families=("mimo",))
    assert LUNA in [leg.id for leg in plan.legs]
    assert not any(s["leg"] == LUNA for s in plan.skipped)


def test_catalog_seeds_luna_with_opencode_harness():
    from plangraph.routing_support import resolver
    from plangraph.routing_support.catalog_db import CatalogDB
    db = CatalogDB(":memory:")
    db.init_and_seed()
    rows = [r for r in resolver.get_catalog_models(db) if r["id"] == "omniroute/cx-gpt-6-luna"]
    assert len(rows) == 1
    row = rows[0]
    assert row["harness"] == "opencode" and row["family"] == "gpt"
    assert row["context_tokens"] == 900000 and row["trains_on_prompts"] == 0


def test_catalog_registers_kimi_with_opencode_harness():
    # D-3033: kimi (opencode go) is a hand-picked review seat; the override path
    # (spawner/service.py) refuses a catalog row without an explicit harness.
    from plangraph.routing_support import resolver
    from plangraph.routing_support.catalog_db import CatalogDB
    db = CatalogDB(":memory:")
    db.init_and_seed()
    for model_id in ("opencode-go/kimi-k3.1-pro", "opencode-go/kimi-k3"):
        rows = [r for r in resolver.get_catalog_models(db) if r["id"] == model_id]
        assert len(rows) == 1
        row = rows[0]
        assert row["harness"] == "opencode" and row["family"] == "kimi"
        # The override admission (spawner/service.py) also needs these.
        assert row["tier"] in ("credit", "trial", "free", "subscription", "paid")
        assert type(row["context_tokens"]) is int and row["context_tokens"] > 0
        assert resolver.detect_model_family(model_id) == "kimi"


def test_kimi_harness_does_not_make_kimi_a_routed_leg():
    # Registration only admits a hand-picked seat; routing never offers kimi.
    for role in ("implement", "review"):
        plan = plan_spawn(CARD, role=role, writer_families=("claude",))
        for model_id in ("opencode-go/kimi-k3.1-pro", "opencode-go/kimi-k3"):
            assert model_id not in [leg.model for leg in plan.legs]
            assert model_id not in [leg.model for leg in plan.review_seats]


def test_catalog_kimi_ids_match_opencode_models_listing():
    # D-3068: `opencode models` lists kimi-k3.1-pro and kimi-k3, not kimi-k3-max.
    from plangraph.routing_support import resolver
    from plangraph.routing_support.catalog_db import CatalogDB
    db = CatalogDB(":memory:")
    db.init_and_seed()
    rows = {r["id"]: r for r in resolver.get_catalog_models(db) if r["family"] == "kimi"}
    assert set(rows) == {"opencode-go/kimi-k3.1-pro", "opencode-go/kimi-k3"}
    for row in rows.values():
        assert row["harness"] == "opencode" and row["trains_on_prompts"] == 0
