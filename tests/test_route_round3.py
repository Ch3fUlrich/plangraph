"""Regression probes for round-three review findings."""
import ast
from dataclasses import replace
from pathlib import Path
import subprocess
import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import USABLE_FRACTION_PCT, plan_spawn

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")


def assert_review_seat_safety(estimate):
    """Retain main's exact diff/no-diff seats and writer-family fence."""
    from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
    for diff in ({"diff_ref": {"repo": "/repo", "base": "HEAD~1", "head": "HEAD"}}, {}):
        review = plan_spawn({"estimate_tokens": estimate, **diff}, role="review",
                            writer_families=("gpt",), policy=load_route_policy(DEFAULT_POLICY_PATH), catalog_rows=())
        assert len({leg.family for leg in review.review_seats}) == 2
        assert all(leg.family != "gpt" for leg in review.review_seats)
        # Rule 3.61 order for a gpt writer, two families: inline-only mimo-v2.6-pro needs diff metadata; every leg
        # serves a card up to the usable share of its own request limit (per-leg context).
        limit = {leg.id: leg.max_request_tokens for leg in DEFAULT_LEGS}
        fits = lambda leg_id: estimate * 100 <= USABLE_FRACTION_PCT * limit[leg_id]
        order = (["mimo-v2.6-pro"] if diff else []) + ["opencode-deepseek-v4.1-flash-free",
                                                     "opencode-go-deepseek-v4-flash", "opencode-go-glm-5.3-flash"]
        expected, families = [], set()
        for leg_id in order:
            family = "mimo" if "mimo" in leg_id else ("glm" if "glm" in leg_id else "deepseek")
            if fits(leg_id) and family not in families and len(expected) < 2:
                expected.append(leg_id)
                families.add(family)
        assert [leg.id for leg in review.review_seats] == expected



def test_review_same_family_table_order():
    base = next(l for l in DEFAULT_LEGS if l.id == "mimo-v2.6-flash")
    legs = (replace(base, id="first", roles=("review",)),
            replace(base, id="second", roles=("review",)))
    def plan(a, b):
        # MiMo is inline-only: provide its required diff metadata so this
        # ranking probe compares eligible legs, not missing-diff exclusions.
        return plan_spawn({"estimate_tokens": 1000,
                           "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}, role="review",
                          writer_families=("gpt",), legs=legs,
                          benchmark={name: {"aa_intelligence": score,
                                     "cost_per_task_usd": 0, "pool_burn_weight": 0}
                                     for name, score in (("first", a), ("second", b))})
    assert [l.id for l in plan(20, 90).legs] == ["first", "second"]
    assert [l.id for l in plan(90, 20).legs] == ["first", "second"]


def test_seats_use_review_route(monkeypatch):
    from plangraph.planner import routing
    original = routing.route_card
    def review_only(*args, **kwargs):
        assert kwargs.get("role") == "review"
        return original(*args, **kwargs)
    monkeypatch.setattr(routing, "route_card", review_only)
    plan = plan_spawn({"estimate_tokens": 1000}, role="review",
                      writer_families=("claude",))
    assert plan.review_seats


def test_changelog_fragment_only():
    assert Path("CHANGELOG.md").read_bytes() == subprocess.check_output(
        ["git", "show", "origin/main:CHANGELOG.md"])


def test_top_level_definition_spacing():
    for name in ("src/plangraph/planner/routing.py", "src/plangraph/routing_support/leg_state.py",
                 "tests/test_spawn_cli.py"):
        text = Path(name).read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        for previous, node in zip(tree.body, tree.body[1:]):
            if isinstance(previous, (ast.FunctionDef, ast.ClassDef)) and isinstance(
                    node, (ast.FunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                assert lines[previous.end_lineno:start - 1] == ["", ""], (name, node.name)


def test_registered_fallback_inputs():
    from plangraph.planner.legs import DEFAULT_LEGS
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    for id in ("omniroute-deepseek-flash", "omniroute-muse-spark", "agy-opus",
               "opencode-go-glm-5.3-flash"):
        assert table[id].disabled is None
    assert table["deepseek"].tier == "paid"
    assert table["opencode-go-glm-5.3-flash"].model == "opencode-go/glm-5.3-flash"


def test_each_role_falls_through_failed_and_limited():
    from plangraph.planner.route_policy import RoutePolicy
    base = next(leg for leg in DEFAULT_LEGS if leg.id == "opencode-deepseek-v4.1-flash-free")
    for role in ("implement", "review", "orchestrate"):
        legs = (replace(base, id="first", roles=(role,), disabled=None),
                replace(base, id="second", model="opencode/big-pickle", family="pickle", roles=(role,), disabled=None))
        for state in ("limited", "down"):
            def reader(model, **kwargs):
                return {"state": state if model == base.model else "available", "evidence": None}
            plan = plan_spawn({"estimate_tokens": 1000}, role=role, legs=legs,
                              policy=RoutePolicy(), leg_reader=reader, writer_families=("gpt",))
            assert [leg.id for leg in plan.legs] == ["second"]


@pytest.mark.parametrize("estimate", [100000, 110000, 110001, 300000])
def test_review_seats_follow_each_legs_request_limit(estimate):
    assert_review_seat_safety(estimate)
