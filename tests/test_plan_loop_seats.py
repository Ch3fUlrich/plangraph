from dataclasses import replace
from pathlib import Path

import pytest

from tools import plan_loop_seats as seats


DEFAULT = Path(__file__).parents[1] / "tools" / "plan-loop.default.toml"


def policy(tmp_path, edit=lambda text: text):
    path = tmp_path / "policy.toml"
    path.write_text(edit(DEFAULT.read_text()))
    return seats.load_policy(path)


def test_default_private_confirm_reaches_families_at_every_level():
    p = seats.load_policy(DEFAULT)
    for level in (1, 2, 3):
        state = seats.State(level=level)
        families = set()
        for _ in range(5):
            leg, = seats.pick("confirm", state, p)
            families.add(seats.seat_family(leg.family))
            state.use(leg.family)
        assert families == {seats.seat_family(f) for f in
                            ("mimo", "deepseek", "qwen", "glm", "gpt")}


@pytest.mark.parametrize("edit", [
    lambda t: t + '\nallow_unwitnessed = true\n',
    lambda t: t.replace('rung = 1', 'unknown = 1', 1),
    lambda t: t.replace('mimo-v2.6-pro', 'missing-leg'),
    lambda t: t.replace('planner_family = "anthropic"', 'planner_family = "openai"'),
    lambda t: t.replace('mimo-v2.6-pro', 'claude-haiku-5.5'),
    lambda t: t.replace('rung = 3, training = true', 'rung = 3, training = false'),
    lambda t: t.replace('rung = 1, training = true', 'rung = 1, training = false'),
    lambda t: t.replace('material = "private"', 'material = "other"'),
    lambda t: t.replace('max_rounds = 6', 'max_rounds = true'),
    lambda t: t.replace('budget_fraction = 0.40', 'budget_fraction = 1.1'),
    lambda t: t.replace('rung = 1', 'rung = 0', 1),
    lambda t: t.replace('fact_check_leg = [', 'fact_check_leg = ["missing", '),
])
def test_load_refuses(tmp_path, edit):
    with pytest.raises(ValueError):
        policy(tmp_path, edit)


def test_registry_contributor_is_stricter(monkeypatch):
    monkeypatch.setattr(seats, "DEFAULT_LEGS", tuple(
        replace(item, model="example-contributor") if item.id == "mimo-v2.6-pro"
        else item for item in seats.DEFAULT_LEGS))
    with pytest.raises(ValueError, match="training"):
        seats.load_policy(DEFAULT)


def test_ladder_and_exemptions():
    p = seats.load_policy(DEFAULT)
    state = seats.State(level=3, slug="sample")
    for kind in ("discovery", "stacked", "scope-cut", "escalated"):
        assert [leg.id for leg in seats.pick(kind, state, p)] == ["cx-gpt-6.1-sol"]
    state.level = 4
    for kind in ("confirm", "diff", "r0"):
        assert seats.pick(kind, state, p)
    with pytest.raises(seats.SelectionRefused) as error:
        seats.pick("stacked", state, p)
    assert str(error.value) == (
        "BLOCKED plan:sample | ladder-exhausted | options: add a leg, *replan")


def test_family_recency_and_aliases():
    p = seats.load_policy(DEFAULT)
    state = seats.State()
    for family in ("mimo", "deepseek", "qwen", "glm"):
        state.use(family)
    assert seats.pick("stacked", state, p)[0].family == "gpt"
    state.use("openai")
    assert seats.pick("stacked", state, p)[0].family == "mimo"
    state.use("mimo")
    assert seats.pick("stacked", state, p)[0].family == "deepseek"
    assert len(state.used_families) == 5


def test_high_discovery_never_degrades(tmp_path):
    p = seats.load_policy(DEFAULT)
    picked = seats.pick("discovery", seats.State(tier="high"), p)
    assert len({seats.seat_family(leg.family) for leg in picked}) == 2
    with pytest.raises(seats.SelectionRefused, match="review-loop-stalled"):
        seats.pick("discovery", seats.State(tier="high", level=3), p)


def test_r0_keeps_fact_check_order():
    p = seats.load_policy(DEFAULT)
    state = seats.State(level=4)
    state.use("qwen")
    assert seats.pick("r0", state, p)[0].id == p.fact_check_leg[0]


def test_private_training_filter_and_public_order(tmp_path):
    private = seats.load_policy(DEFAULT)
    public = policy(tmp_path, lambda t: t.replace('"private"', '"public"'))
    state = seats.State(level=3)
    state.use("gpt")
    assert seats.pick("stacked", state, private)[0].family == "gpt"
    assert seats.pick("stacked", state, public)[0].family == "muse"


def test_ladder_rises_only_on_consecutive_productive_counted_rounds():
    state = seats.State()
    state.end_round(True)
    state.end_round(False, counted=False)
    assert state.level == 1
    state.end_round(True)
    assert state.level == 2
    state.end_round(False)
    state.end_round(True)
    assert state.level == 2
    state.escalate()
    assert state.level == 3


def test_lens_lowest_yield_with_stable_ties():
    state = seats.State()
    assert state.lens() == seats.LENSES[0]
    state.lens_yields[seats.LENSES[0]] = 2
    assert state.lens() == seats.LENSES[1]
    state.lens_yields[seats.LENSES[1]] = 1
    assert state.lens() == seats.LENSES[2]


def test_pick_is_pure_and_unknown_kind_refused():
    p = seats.load_policy(DEFAULT)
    state = seats.State()
    assert seats.pick("stacked", state, p) == seats.pick("stacked", state, p)
    assert state.used_families == []
    with pytest.raises(ValueError, match="kind"):
        seats.pick("unknown", state, p)
