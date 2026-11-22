"""Acceptance tests for the deterministic task effort profile."""
import pytest


@pytest.mark.parametrize(("paths", "fanout", "risk", "tests", "tokens", "level", "task_class"), [
    (["docs/a.md"], 0, "low", True, 100, "L0", "small_change"),
    (["src/a.py", "tests/test_a.py"], 0, "normal", True, 100, "L1", "small_change"),
    ([f"src/a{i}.py" for i in range(4)], 0, "normal", True, 100, "L2", "medium_change"),
    ([f"src/a{i}.py" for i in range(8)], 0, "normal", True, 100, "L3", "large_change"),
    ([f"src/a{i}.py" for i in range(13)], 0, "normal", True, 100, "L4", "large_change"),
    (["src/a.py"], 3, "normal", True, 100, "L2", "medium_change"),
    (["src/a.py"], 0, "normal", False, 100, "L3", "small_change"),
    (["src/a.py"], 0, "high", True, 100, "L3", "small_change"),
    (["src/a.py"], 0, "critical", True, 100, "L4", "small_change"),
    (["src/a.py"], 0, "normal", True, 100_000, "L4", "small_change"),
])
def test_effort_level_table(paths, fanout, risk, tests, tokens, level, task_class):
    from plangraph.planner.routing import rule_level

    result = rule_level(dict(paths=paths, write_set=["docs/ignored.md"], fanout=fanout,
                             risk=risk, has_tests=tests, window_class="128k",
                             estimate_tokens=tokens))
    assert (result["level"], result["task_class"]) == (level, task_class)


def test_effort_profile_floors_and_planner_adjustments():
    from plangraph.planner.routing import effort_profiles, rule_level, task_class

    table = effort_profiles()
    assert [table["levels"][f"L{i}"]["floor"] for i in range(5)] == [37.5, 44, 49, 50, 51]
    assert [table["risk_floors"][r] for r in ("low", "normal", "high", "critical")] == [37.5, 44, 46, 50]
    assert table["levels"]["L3"]["seats"] == 2
    assert not table["levels"]["L4"]["cascade"]
    for n, size in [(1, "small"), (4, "medium"), (8, "large")]:
        for kind in ("change", "regression"):
            value = dict(paths=[f"src/a{i}.py" for i in range(n)], has_tests=True, check_kind=kind)
            assert task_class(value) == f"{size}_{kind}"
    assert rule_level({"level": "L4", "risk": "low"})["reason"] == "planner_raise"
    lower = rule_level({"level": "L0", "risk": "high"})
    assert (lower["level"], lower["floor"], lower["lowered"]) == ("L3", 50, "L0")
