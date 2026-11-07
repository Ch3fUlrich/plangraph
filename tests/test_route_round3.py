"""Regression probes for round-three review findings."""
import ast
from dataclasses import replace
from pathlib import Path
import subprocess

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn


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
    baseline = subprocess.check_output(
        ["git", "show", "origin/main:CHANGELOG.md"])
    lines = baseline.splitlines(keepends=True)
    prefix = b"  page (8c18e56). md-sync fetches all keeper repos first; "
    assert lines[264].startswith(prefix)
    assert lines[264].endswith(b" repo registered.\n")
    lines[264] = prefix + b"the data repo registered.\n"
    assert Path("CHANGELOG.md").read_bytes() == b"".join(lines)


def test_top_level_definition_spacing():
    for name in ("src/plangraph/planner/routing.py", "tests/test_spawn_cli.py"):
        text = Path(name).read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        for previous, node in zip(tree.body, tree.body[1:]):
            if isinstance(previous, (ast.FunctionDef, ast.ClassDef)) and isinstance(
                    node, (ast.FunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                assert lines[previous.end_lineno:start - 1] == ["", ""], (name, node.name)
