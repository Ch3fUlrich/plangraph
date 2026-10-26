"""E3b seat-family selection (D-2913): the code-owned seat order and its legs.

Part 1 tests. The pick/probe/verify rows land with their scope items; this
file grows with them. Everything is hermetic: fake probe, fake leg_state.
"""
import dataclasses
from datetime import datetime, timedelta

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, SEAT_ORDER
from plangraph.planner.routing import plan_spawn, validate_model
from plangraph.review.family import FamilyError, check_cross_family, model_family
from plangraph.routing_support.leg_state import LegStateError, LegStateStore
from plangraph.routing_support.probe import available

# The six console-openrouter / opencode free review legs of the slice, with
# the exact model ids the host listing showed (design E3b "catalog legs").
NEW_REVIEW_LEGS = {
    "opencode-mimo-v2.6-flash-free": "opencode/mimo-v2.6-flash-free",
    "or-nemotron-3-ultra-free": "console-openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "or-gemma-4-31b-free": "console-openrouter/google/gemma-4-31b-it:free",
    "or-inkling-free": "console-openrouter/thinkingmachines/inkling:free",
    "or-qwen3.8-27b-free": "console-openrouter/qwen/qwen3.8-27b:free",
    "or-north-mini-free": "console-openrouter/cohere/north-mini-code:free",
}


def test_new_review_legs_shape():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id, model in NEW_REVIEW_LEGS.items():
        leg = by_id.get(leg_id)
        assert leg is not None, leg_id
        assert leg.model == model
        assert leg.roles == ("review",), leg_id
        assert leg.tier == "free", leg_id
        assert leg.trains_on_data is True, leg_id
        assert leg.harness == "opencode", leg_id


def test_new_legs_pass_validate_model():
    for leg_id, model in NEW_REVIEW_LEGS.items():
        assert validate_model(model) == model, leg_id


def test_seat_order_is_the_code_owned_order():
    assert SEAT_ORDER == (
        "qodercli-qwen3.8-flash",
        "opencode-mimo-v2.6-flash-free",
        "or-nemotron-3-ultra-free",
        "or-gemma-4-31b-free",
        "or-inkling-free",
        "or-qwen3.8-27b-free",
        "or-north-mini-free",
        "agy-sonnet-5.5",
        "opencode-glm-5.3-flash-free",
    )
    ids = {leg.id for leg in DEFAULT_LEGS}
    assert all(leg_id in ids for leg_id in SEAT_ORDER)
    assert SEAT_ORDER[-1] == "opencode-glm-5.3-flash-free"  # glm last (D-2913)


def test_deepseek_is_never_a_seat():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    assert "review" not in by_id["opencode-deepseek-v4.1-flash-free"].roles
    assert all("deepseek" not in leg_id for leg_id in SEAT_ORDER)


# --- E3b item 3: plan_spawn picks the review seats by itself (D-2913) ---

def _card(risk=None):
    card = {"estimate_tokens": 2000}
    if risk is not None:
        card["risk"] = risk
    return card


def _leg_state(available=None, limited=(), verified=None):
    """Fake leg_state reader; with `available`, everything else is limited.

    ``verified=None`` means every leg is verified ("every leg verified and
    available"); otherwise ``verified`` is the set of verified leg ids
    (E3b item 5: verified before counted).
    """
    available = None if available is None else set(available)
    verified = None if verified is None else frozenset(verified)

    def reader(model, now=None, account=None, combo=None):
        if available is not None and model not in available:
            state = "limited"
        else:
            state = "limited" if model in limited else "ok"
        return {"state": state, "evidence": "fake"}

    reader.verified = lambda leg, now=None: True if verified is None else leg in verified
    return reader


def _seat_ids(plan):
    return [leg.id for leg in plan.review_seats]


def test_seat_order_beats_tier_position():
    # Every earlier non-Claude family is a writer: glm is the final non-Claude
    # seat, with Sonnet retained after it as the last resort.
    writers = ("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere")
    plan = plan_spawn(_card("high"), role="review", writer_families=writers,
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free", "agy-sonnet-5.5"]
    assert plan.error is None
    # negative control: block anthropic too and only glm is left, with the
    # existing error text kept when fewer seats than n are found
    plan = plan_spawn(_card("high"), role="review", writer_families=writers + ("anthropic",),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]
    assert plan.error == "two distinct review families unavailable"


def test_review_seats_by_writer_family_and_risk():
    # writers xiaomi + anthropic, risk high: qwen then nemotron-ultra
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash", "or-nemotron-3-ultra-free"]
    # writer alibaba: mimo-flash-free then nemotron-ultra
    plan = plan_spawn(_card("high"), role="review", writer_families=("alibaba",),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-mimo-v2.6-flash-free", "or-nemotron-3-ultra-free"]
    # risk standard: one seat
    plan = plan_spawn(_card(), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash"]
    assert plan.error is None


def test_writer_family_spellings_pick_the_same_seats():
    picks = {}
    for spelling in ("qwen", "alibaba", "mimo", "xiaomi"):
        plan = plan_spawn(_card("high"), role="review", writer_families=(spelling,),
                          leg_reader=_leg_state(), legs=DEFAULT_LEGS)
        picks[spelling] = _seat_ids(plan)
    assert picks["qwen"] == picks["alibaba"]
    assert picks["mimo"] == picks["xiaomi"]
    # positive control on the real pick, not just equality
    assert picks["alibaba"] == ["opencode-mimo-v2.6-flash-free", "or-nemotron-3-ultra-free"]
    # negative control: an unknown writer family is refused, never guessed
    with pytest.raises(FamilyError):
        plan_spawn(_card(), role="review", writer_families=("kling",),
                   leg_reader=_leg_state(), legs=DEFAULT_LEGS)


def test_glm_is_picked_only_when_every_earlier_family_is_blocked():
    writers = ("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere")
    # anthropic's leg is unavailable; every other family ahead of glm is a writer
    plan = plan_spawn(_card(), role="review", writer_families=writers,
                      leg_reader=_leg_state(limited=("sonnet-5.5", "agy/sonnet-5.5")),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]
    # negative control: one earlier family free (cohere) takes the seat first
    plan = plan_spawn(_card(), role="review", writer_families=writers[:-1],
                      leg_reader=_leg_state(limited=("sonnet-5.5", "agy/sonnet-5.5")),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["or-north-mini-free"]
    # negative control: an unverified earlier family is skipped like a blocked
    # one - cohere's leg is available but unverified, so glm takes the seat
    plan = plan_spawn(_card(), role="review", writer_families=writers[:-1],
                      leg_reader=_leg_state(limited=("sonnet-5.5", "agy/sonnet-5.5"),
                                            verified=("opencode-glm-5.3-flash-free",)),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]


@pytest.mark.parametrize("blocked", ["excluded", "unavailable"])
def test_claude_review_remains_selectable_as_last_resort(blocked):
    non_claude = ("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere", "zhipu")
    claude = next(leg for leg in DEFAULT_LEGS if leg.id == "agy-sonnet-5.5")
    plan = plan_spawn(
        _card(), role="review", legs=DEFAULT_LEGS,
        exclude_families=non_claude if blocked == "excluded" else (),
        leg_reader=_leg_state(available=(claude.model, "agy/" + claude.model)
                             if blocked == "unavailable" else None),
    )
    assert _seat_ids(plan) == [claude.id]


def test_glm_review_precedes_every_claude_but_follows_other_non_claude():
    plan = plan_spawn(_card(), role="review", legs=DEFAULT_LEGS,
                      writer_families=("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines"),
                      leg_reader=_leg_state())
    assert _seat_ids(plan) == ["or-north-mini-free"]
    plan = plan_spawn(_card(), role="review", legs=DEFAULT_LEGS,
                      writer_families=("alibaba", "xiaomi", "nvidia", "google", "thinkingmachines", "cohere"),
                      leg_reader=_leg_state())
    assert _seat_ids(plan) == ["opencode-glm-5.3-flash-free"]


def test_deepseek_is_refused_even_when_a_config_lists_it():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = tuple(
        dataclasses.replace(by_id["or-nemotron-3-ultra-free"],
                            model="opencode/deepseek-v4.1-flash-free", family="deepseek")
        if leg.id == "or-nemotron-3-ultra-free" else leg for leg in DEFAULT_LEGS)
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=legs)
    seats = _seat_ids(plan)
    assert seats == ["qodercli-qwen3.8-flash", "or-gemma-4-31b-free"]
    assert any(s["leg"] == "or-nemotron-3-ultra-free" and s["reason"] == "deepseek_seat"
               for s in plan.skipped)


def test_family_table_maps_the_new_legs():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    expected = {
        "or-gemma-4-31b-free": "google",
        "or-inkling-free": "thinkingmachines",
        "or-north-mini-free": "cohere",
        "or-qwen3.8-27b-free": "alibaba",
    }
    for leg_id, family in expected.items():
        assert model_family(by_id[leg_id].model) == family, leg_id
    # negative control: an unknown model id has no family (refused, not guessed)
    assert model_family("totally/unknown-model") is None


def test_check_cross_family_agrees_with_plan_spawn():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id, model in NEW_REVIEW_LEGS.items():
        family = model_family(model)
        # cross-family: the keeper's table and the spawner both seat this leg
        assert check_cross_family("openai", model) == family
        plan = plan_spawn(_card(), role="review", writer_families=("openai",),
                          leg_reader=_leg_state(available=(model,)), legs=DEFAULT_LEGS)
        assert _seat_ids(plan) == [leg_id], leg_id
        # negative control: the writer's own family is refused by both
        with pytest.raises(FamilyError):
            check_cross_family(family, model)
        plan = plan_spawn(_card(), role="review", writer_families=(family,),
                          leg_reader=_leg_state(), legs=DEFAULT_LEGS)
        assert leg_id not in _seat_ids(plan), leg_id


# --- E3b item 4: live probe (D-2913) ---

NEMOTRON_MODEL = "console-openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"


def _fake_probe(listing):
    """Return (probe, calls): serves `listing` and counts its invocations."""
    calls = []

    def probe():
        calls.append(1)
        return listing

    return probe, calls


def _listing(exclude=()):
    excluded = set(exclude)
    return "\n".join(leg.model for leg in DEFAULT_LEGS if leg.model not in excluded)


def test_probe_skips_an_unlisted_model_id(monkeypatch, tmp_path):
    # the host's PATH never decides: the binary check is faked hermetically
    monkeypatch.setattr("plangraph.routing_support.probe.which",
                        lambda name: "/fake/bin/" + name)
    # positive control: every model id listed -> the picks are unchanged
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path / "listed"))
    probe, calls = _fake_probe(_listing())
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS, probe=probe)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash", "or-nemotron-3-ultra-free"]
    assert not [s for s in plan.skipped if s["reason"].startswith("probe: ")]
    assert len(calls) == 1
    # negative control: the nemotron model id is not listed -> the leg leaves
    # with the probe reason and the next family in seat order takes the seat
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path / "unlisted"))
    probe, calls = _fake_probe(_listing(exclude=(NEMOTRON_MODEL,)))
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS, probe=probe)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash", "or-gemma-4-31b-free"]
    assert any(s["leg"] == "or-nemotron-3-ultra-free"
               and s["reason"] == "probe: model not listed: " + NEMOTRON_MODEL
               for s in plan.skipped)


from plangraph.routing_support.probe import _models as _production_models


def test_plan_probe_all_roles_and_default_cache(monkeypatch, tmp_path):
    from dataclasses import replace
    import subprocess
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    monkeypatch.setattr("plangraph.routing_support.probe.which", lambda name: name)
    calls = []
    def listing(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, _listing(exclude=(NEMOTRON_MODEL,)))
    monkeypatch.setattr(subprocess, "run", listing)
    monkeypatch.setattr("plangraph.routing_support.probe._models", _production_models)
    now = datetime(2026, 10, 26, 12, 0)
    legs = tuple(replace(l, roles=("implement", "review", "design")) for l in DEFAULT_LEGS)
    for role in ("implement", "review", "design"):
        plan = plan_spawn(_card("high"), role=role, leg_reader=_leg_state(), now=now, legs=legs)
        assert any(s["leg"] == "or-nemotron-3-ultra-free"
                   and s["reason"].startswith("probe: ") for s in plan.skipped)
        assert NEMOTRON_MODEL not in [l.model for l in (*plan.legs, *plan.review_seats)]
    assert len(calls) == 1


def test_probe_cache_is_used_on_the_second_call(monkeypatch, tmp_path):
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    legs = [leg for leg in DEFAULT_LEGS
            if leg.id in ("or-nemotron-3-ultra-free", "or-gemma-4-31b-free")]
    probe, calls = _fake_probe(NEMOTRON_MODEL)
    now = datetime(2026, 10, 26, 12, 0)
    ok_ids, reasons = available(legs, probe, now)
    assert ok_ids == ["or-nemotron-3-ultra-free"]
    assert set(reasons) == {"or-gemma-4-31b-free"}
    # second call inside the 10 min window reuses the cached listing
    ok_ids2, reasons2 = available(legs, probe, now + timedelta(minutes=5))
    assert (ok_ids2, reasons2) == (ok_ids, reasons)
    assert len(calls) == 1
    # negative control: past the window the probe runs again
    available(legs, probe, now + timedelta(minutes=11))
    assert len(calls) == 2


def test_probe_binary_check_for_qodercli_and_agy(monkeypatch, tmp_path):
    monkeypatch.setenv("PLANGRAPH_SPAWN_STATE", str(tmp_path))
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    legs = [by_id["qodercli-qwen3.8-flash"], by_id["agy-sonnet-5.5"],
            by_id["or-nemotron-3-ultra-free"]]
    probe, _calls = _fake_probe(_listing())
    now = datetime(2026, 10, 26, 12, 0)
    # positive control: both binaries on PATH -> only the listing decides
    monkeypatch.setattr("plangraph.routing_support.probe.which",
                        lambda name: "/fake/bin/" + name)
    ok_ids, reasons = available(legs, probe, now)
    assert ok_ids == ["qodercli-qwen3.8-flash", "agy-sonnet-5.5",
                      "or-nemotron-3-ultra-free"]
    assert reasons == {}
    # negative control: the binary is missing -> the harness leg is unavailable
    monkeypatch.setattr("plangraph.routing_support.probe.which", lambda name: None)
    ok_ids, reasons = available(legs, probe, now)
    assert ok_ids == ["or-nemotron-3-ultra-free"]
    assert reasons == {"qodercli-qwen3.8-flash": "qodercli not on PATH",
                       "agy-sonnet-5.5": "agy not on PATH"}


# --- E3b item 5: verified before counted (D-2913) ---


def _store(tmp_path):
    return LegStateStore(tmp_path / "state.sqlite")


def test_record_verify_requires_leg_ok_and_run(tmp_path):
    store = _store(tmp_path)
    for args in (("", True, "run-1"), ("or-gemma-4-31b-free", True, ""),
                 ("or-gemma-4-31b-free", 1, "run-1")):
        with pytest.raises(LegStateError):
            store.record_verify(*args)
    assert store.verified("or-gemma-4-31b-free", 0.0) is False


def test_verified_counts_only_a_fresh_pass(tmp_path):
    store = _store(tmp_path)
    now = 1_000_000.0
    leg = "or-nemotron-3-ultra-free"
    # negative control: the 6 new legs ship unverified (no verify record)
    assert store.verified(leg, now) is False
    store.record_verify(leg, False, "run-1", now)
    assert store.verified(leg, now) is False        # a failed verify never counts
    store.record_verify(leg, True, "run-2", now)
    assert store.verified(leg, now) is True         # a passing verify counts
    assert store.verified(leg, now + 6 * 86400) is True
    # negative control: a 7-day-old verify does not count
    assert store.verified(leg, now + 7 * 86400) is False


def test_grandfathered_legs_verified_in_code_and_revocable(tmp_path):
    store = _store(tmp_path)
    now = 1_000_000.0
    # legs that already served as seats are grandfathered as verified in code
    for leg in ("qodercli-qwen3.8-flash", "agy-sonnet-5.5", "opencode-glm-5.3-flash-free"):
        assert store.verified(leg, now) is True, leg
    # negative control: a failed verify revokes the grandfathered state again
    store.record_verify("agy-sonnet-5.5", False, "run-3", now)
    assert store.verified("agy-sonnet-5.5", now) is False


def test_unverified_legs_are_skipped_as_seats():
    # the hermetic default state has only the grandfathered legs verified
    plan = plan_spawn(_card("high"), role="review",
                      writer_families=("xiaomi", "anthropic", "glm"),
                      leg_reader=_leg_state(verified=("qodercli-qwen3.8-flash",
                                                      "agy-sonnet-5.5",
                                                      "opencode-glm-5.3-flash-free")),
                      legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash"]
    assert plan.error == "two distinct review families unavailable"
    assert any(s["leg"] == "or-nemotron-3-ultra-free" and s["reason"] == "unverified"
               for s in plan.skipped)
    # positive control: every leg verified and available -> qwen + nemotron-ultra
    plan = plan_spawn(_card("high"), role="review", writer_families=("xiaomi", "anthropic"),
                      leg_reader=_leg_state(), legs=DEFAULT_LEGS)
    assert _seat_ids(plan) == ["qodercli-qwen3.8-flash", "or-nemotron-3-ultra-free"]


def test_a_passing_verify_counts_and_an_old_one_does_not(tmp_path):
    store = LegStateStore(tmp_path / "state.sqlite")
    now = 1_000_000.0
    writers = ("xiaomi", "anthropic", "glm")

    def pick():
        return _seat_ids(plan_spawn(_card("high"), role="review",
                                    writer_families=writers, leg_reader=store,
                                    legs=DEFAULT_LEGS, now=now))
    # unverified legs are skipped as seats
    assert pick() == ["qodercli-qwen3.8-flash"]
    # negative control: a 7-day-old verify does not count
    store.record_verify("or-gemma-4-31b-free", True, "run-1", now - 7 * 86400)
    assert pick() == ["qodercli-qwen3.8-flash"]
    # positive control: a passing verify counts, in seat order
    store.record_verify("or-gemma-4-31b-free", True, "run-2", now - 60)
    assert pick() == ["qodercli-qwen3.8-flash", "or-gemma-4-31b-free"]
    # negative control: a failed verify revokes the seat again
    store.record_verify("or-gemma-4-31b-free", False, "run-3", now - 30)
    assert pick() == ["qodercli-qwen3.8-flash"]
