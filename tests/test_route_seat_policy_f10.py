"""Rule 3.66 (f10): one counted smart seat; the two weak OpenCode seats only when no smart leg serves."""
import pytest

from plangraph.planner.legs import (review_route_pool, REVIEW_ARGON, REVIEW_ARGON_LOW, REVIEW_HYDROGEN,
                                    REVIEW_REQUIRED, REVIEW_SONNET)
from plangraph.planner.routing import plan_spawn

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")
BY_ID = {leg.id: leg for leg in review_route_pool()}
MODELS = {leg_id: BY_ID[leg_id].model for leg_id in (REVIEW_HYDROGEN, REVIEW_ARGON_LOW, REVIEW_ARGON)}
HYDROGEN, ARGONS = {MODELS[REVIEW_HYDROGEN]}, {MODELS[REVIEW_ARGON_LOW], MODELS[REVIEW_ARGON]}
GEMINI = HYDROGEN | ARGONS


def reader(down=()):
    return lambda model, **_scope: {"state": "limited" if model in down else "available", "evidence": None}


def review(writers, down=(), card=None):
    return plan_spawn(dict(estimate_tokens=1000, **(card or {})), role="review", writer_families=writers,
                      leg_reader=reader(down), catalog_rows=())


def seats(plan):
    return [leg.id for leg in plan.review_seats]


def test_a_claude_head_gets_one_hydrogen_seat_while_hydrogen_serves():
    plan = review(("claude",))
    assert seats(plan) == [REVIEW_HYDROGEN] and plan.error is None
    assert plan.review_seats[0].tool_mode == "tools"
    assert plan.seat_coverage[0]["leg"] == REVIEW_HYDROGEN and plan.seat_coverage[0]["covers"]


def test_a_gpt_head_gets_one_hydrogen_seat():
    assert seats(review(("gpt",))) == [REVIEW_HYDROGEN]


def test_a_gemini_head_gets_one_sonnet_seat():
    plan = review(("gemini",))
    assert seats(plan) == [REVIEW_SONNET] and plan.error is None


def test_with_only_the_opencode_legs_serving_the_two_weak_seats_review():
    plan = review(("claude",), down=GEMINI)
    assert seats(plan) == list(REVIEW_REQUIRED) and plan.error is None
    assert REVIEW_SONNET not in [leg.id for leg in plan.legs]   # a Claude head never gets the Claude seat


def test_a_gemini_head_with_sonnet_unavailable_falls_to_the_weak_pair():
    # the Claude seat is not in the route when its quota is out; both weak seats seat the head.
    plan = review(("gemini",), down={BY_ID[REVIEW_SONNET].model, REVIEW_SONNET})
    assert seats(plan) == list(REVIEW_REQUIRED)


def test_the_route_chain_runs_smart_legs_before_the_weak_pair():
    ids = [leg.id for leg in review(("gpt",)).legs]
    assert ids[0] == REVIEW_HYDROGEN
    assert max(ids.index(i) for i in (REVIEW_HYDROGEN, REVIEW_ARGON_LOW, REVIEW_ARGON)) < min(
        ids.index(i) for i in REVIEW_REQUIRED)
    # hydrogen out: the pair seats the head and leads (clause 6); argon stays behind it
    ids = [leg.id for leg in review(("gpt",), down=HYDROGEN).legs]
    assert ids[:2] == list(REVIEW_REQUIRED) and REVIEW_ARGON_LOW in ids


def test_hydrogen_out_gives_the_weak_pair_not_argon_clause_6():
    assert seats(review(("claude",), down=HYDROGEN)) == list(REVIEW_REQUIRED)


def test_a_credential_diff_on_a_non_claude_head_gets_sonnet_and_keeps_the_weak_pair_for_the_keeper():
    # The keeper close needs two non-Anthropic families on a credential card, so the pair stays seated.
    assert seats(review(("gpt",), card={"risk_tags": ["secrets"]})) == [REVIEW_SONNET, *REVIEW_REQUIRED]


def test_the_lone_smart_seat_slot_holds_the_next_smart_legs_before_the_weak_pair():
    """The production seat run restricts re-routing to the slot: hydrogen's slot must reach argon first."""
    from plangraph.spawner.seat_artifact import reserve_seat_families
    plan = review(("gpt",))
    spares, owners = reserve_seat_families(plan, ("gpt",))
    ids = [leg.id for leg in spares[0]]
    assert ids == [REVIEW_ARGON_LOW, REVIEW_ARGON]   # smart legs only: the weak pair launches as two seats, not as a spare
    assert not set(ids) & set(REVIEW_REQUIRED) and REVIEW_HYDROGEN not in ids


# Rule 3.66 (f10) clause 6 (D-6787): the weak pair also seats a head whose smart seat is limited or busy.
def routed(writers, *, down=(), busy=()):
    return plan_spawn(dict(estimate_tokens=1000), role="review", writer_families=writers,
                      leg_reader=reader(down), catalog_rows=(), busy_legs=busy)


def test_smart_free_gives_one_smart_seat():
    assert seats(routed(("claude",))) == [REVIEW_HYDROGEN]


def test_smart_limited_gives_the_weak_pair_even_when_argon_still_serves():
    plan = routed(("claude",), down=HYDROGEN)
    assert seats(plan) == list(REVIEW_REQUIRED) and plan.error is None
    assert [leg.id for leg in plan.legs[:2]] == list(REVIEW_REQUIRED)   # the pair leads its own fallbacks
    assert len({leg.family for leg in plan.review_seats}) == 2


def test_smart_slots_full_gives_the_weak_pair_at_once():
    plan = routed(("claude",), busy=(REVIEW_HYDROGEN,))
    assert seats(plan) == list(REVIEW_REQUIRED)
    assert {s["leg"]: s["reason"] for s in plan.skipped}[REVIEW_HYDROGEN] == "review_slots_busy"
    # a busy leg that is not the head's smart seat changes nothing
    assert seats(routed(("claude",), busy=("opencode-go-glm-5.3-flash-not-a-leg",))) == [REVIEW_HYDROGEN]


def test_a_gemini_heads_smart_seat_is_sonnet_and_follows_the_same_rule():
    assert seats(routed(("gemini",))) == [REVIEW_SONNET]
    assert seats(routed(("gemini",), busy=(REVIEW_SONNET,))) == list(REVIEW_REQUIRED)
    assert seats(routed(("gemini",), down={BY_ID[REVIEW_SONNET].model})) == list(REVIEW_REQUIRED)


def test_the_service_reports_a_smart_leg_busy_only_when_every_slot_is_running(tmp_path, monkeypatch):
    from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
    from plangraph.spawner.service import SpawnService
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(DEFAULT_POLICY_PATH))
    cap = load_route_policy().review_parallel_slots[REVIEW_HYDROGEN]
    service = SpawnService(state=tmp_path)

    def running(n, status="running", role="review"):
        return {"runs": [{"role": role, "status": status, "chosen_leg": {"id": REVIEW_HYDROGEN}}] * n}
    for n, other, expected in ((cap - 1, {}, ()), (cap, {}, (REVIEW_HYDROGEN,)),
                               (cap, {"status": "completed"}, ()), (cap, {"role": "implement"}, ())):
        monkeypatch.setattr(service, "ps", lambda n=n, other=other: running(n, **other))
        assert service.busy_review_legs() == expected, (n, other)


GLM = BY_ID[REVIEW_REQUIRED[1]].model
ARGONS = {"gemini-4-argon-low", "gemini-4-argon-medium"}


def test_an_incomplete_weak_pair_is_not_reported_as_the_pair():
    # Hydrogen limited and GLM down: DeepSeek plus Argon is no pair; Argon is the one counted seat.
    plan = routed(("gpt",), down=HYDROGEN | {GLM})
    assert seats(plan) == [REVIEW_ARGON_LOW] and plan.error is None
    # Every Gemini leg and GLM down: Sonnet is the counted seat for a GPT head.
    assert seats(routed(("gpt",), down=GEMINI | {GLM})) == [REVIEW_SONNET]
    # A Claude head has no Sonnet: DeepSeek cannot be a "pair" alone, so two families seat it or it errors.
    plan = routed(("claude",), down=GEMINI | {GLM})
    assert REVIEW_REQUIRED[1] not in seats(plan)
    assert not (plan.error is None and seats(plan) == [REVIEW_REQUIRED[0], REVIEW_ARGON_LOW])
