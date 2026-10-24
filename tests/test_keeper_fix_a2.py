"""TASK A' (review findings): verdict/legacy-state fixes.

Covers S3-1 (verdict+waiver refuse tier 'none'), S4 (legacy move hygiene and
the normal tier rule to done), S2-3/S2-4 (verdict TOCTOU re-checks), S2-5/S2-6
(keyword boundaries + edit auto-raise) and S1-1 (epoch-less changes ignored).
"""
import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.store import Store


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _mk(world, pid="L1", project="demo", **kw):
    lane = defn(pid, project=project, **kw)
    goal = defn("GOAL", [pid], project=project, kind="goal",
                title="plan goal", track="T0")
    world.store.create_items(world.p("router"), [lane, goal])
    return f"{project}:{pid}"


def _legacy_in_review(world, key):
    """Router legacy-move to in_review with verified router evidence."""
    world.store.set_legacy_state(world.p("router"), key, "in_review")
    world.store.add_evidence_router(world.p("router"), key, "decision",
                                    {"id": "D-1"})


# ------------------------------------------------------------- S3-1: none tier
def test_verdict_on_none_tier_is_bad_state(world):
    key = _mk(world, review_tier="none", evidence_kinds=["decision"])
    _legacy_in_review(world, key)
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert exc.value.code == "bad_state"
    assert world.store._item(key)["state"] == "in_review"


def test_verdict_changes_on_none_tier_is_bad_state(world):
    key = _mk(world, review_tier="none", evidence_kinds=["decision"])
    _legacy_in_review(world, key)
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, False, "no")
    assert exc.value.code == "bad_state"
    assert world.store._item(key)["state"] == "in_review"


def test_waive_on_none_tier_is_bad_state(world):
    key = _mk(world, review_tier="none", evidence_kinds=["decision"])
    world.store.set_legacy_state(world.p("router"), key, "claimed")
    with pytest.raises(Refused) as exc:
        world.store.waive_second_family(world.p("router"), key, "D-217")
    assert exc.value.code == "bad_state"
    assert world.store._item(key)["review_waiver"] is None


# ------------------------------------------------- S4: legacy move hygiene
def test_legacy_move_bumps_epoch_fresh_round_clears(world):
    # Tier ``none`` is the only tier that still accepts the legacy ``claimed``
    # move (KEEPER-OPCLAIM-CLOSE); the epoch/round/column bookkeeping below is
    # what the test pins, and it is identical on that tier.
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="none", evidence_kinds=["decision"])
    before = world.store._item(key)
    assert (before["epoch"], before["round"]) == (0, 0)
    result = world.store.set_legacy_state(world.p("router"), key, "claimed")
    assert result["claimed_by"] == "ext-team"
    item = world.store._item(key)
    assert item["state"] == "claimed"
    assert item["epoch"] == before["epoch"] + 1
    assert item["round"] == before["round"] + 1
    assert item["claimed_by"] == "ext-team"
    assert item["delegate"] is None
    assert item["reviewer"] is None
    assert item["lease_until"] is None
    assert world.store.verify_integrity() == []


def test_reaper_leaves_legacy_claim_alone(world):
    # A legacy claim carries no lease, so the reaper must not touch it; tier
    # ``none`` is where that claim can still be made (KEEPER-OPCLAIM-CLOSE).
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="none", evidence_kinds=["decision"])
    world.store.set_legacy_state(world.p("router"), key, "claimed")
    world.clock.advance(30 * 24 * 3600)
    out = world.store.reap(world.p("system"))
    assert out["expired"] == []
    assert world.store._item(key)["state"] == "claimed"


def test_legacy_standard_one_verdict_done(world):
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="standard")
    _legacy_in_review(world, key)
    out = world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert out["state"] == "done" and out["reviews_passed"] == 1
    assert world.store.verify_integrity() == []


def test_legacy_high_needs_two_distinct_families(world):
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="high")
    _legacy_in_review(world, key)
    out = world.store.verdict(world.p("reviewer"), key, True, "first")
    assert out["state"] == "in_review" and out["reviews_passed"] == 1
    out = world.store.verdict(world.p("reviewer2"), key, True, "second")
    assert out["state"] == "done" and out["reviews_passed"] == 2
    assert world.store.verify_integrity() == []


def test_legacy_verdict_without_evidence_refused(world):
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="standard")
    world.store.set_legacy_state(world.p("router"), key, "in_review")
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert exc.value.code == "evidence_missing"
    assert world.store._item(key)["state"] == "in_review"


def test_legacy_owner_string_is_claimant_for_reviewer_check(world):
    """S4-2: claimed_by stays the owner string (not a principal name)."""
    key = _mk(world, owner="external-human", owner_family="teamX",
              review_tier="standard")
    world.store.set_legacy_state(world.p("router"), key, "in_review")
    assert world.store._item(key)["claimed_by"] == "external-human"
    world.store.add_evidence_router(world.p("router"), key, "decision",
                                    {"id": "D-1"})
    out = world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert out["state"] == "done"


def test_legacy_stale_round_verdicts_do_not_count(world):
    """S4-4: a verdict from an earlier round never fills a seat."""
    key = _mk(world, owner="ext-team", owner_family="teamX",
              review_tier="high")
    world.store._append(world.p("reviewer"), key, "review_verdict",
                         {"reviewer": "reviewer", "family": "teamc",
                          "ok": True, "epoch": 0, "round": 0},
                         world.clock())
    _legacy_in_review(world, key)
    out = world.store.verdict(world.p("reviewer2"), key, True, "first?")
    assert out["state"] == "in_review" and out["reviews_passed"] == 1


# ------------------------------------------------- S2-3/S2-4: TOCTOU re-checks
def _rig_second_read(store, mutate):
    real = Store._get_or_refuse
    calls = []

    def rigged(self, key):
        item = real(self, key)
        calls.append(1)
        if len(calls) == 2:
            self.conn.execute(mutate + " WHERE key=?", (key,))
            item = real(self, key)
        return item

    return rigged.__get__(store, Store)


def test_verdict_conflict_when_round_moves_under_lock(world, monkeypatch):
    from keeper_util import to_in_review
    key = _mk(world)
    to_in_review(world, key)
    monkeypatch.setattr(
        world.store, "_get_or_refuse",
        _rig_second_read(world.store, "UPDATE items SET round=round+1"))
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert exc.value.code == "conflict"


def test_verdict_conflict_when_claimant_moves_under_lock(world, monkeypatch):
    from keeper_util import to_in_review
    key = _mk(world)
    to_in_review(world, key)
    monkeypatch.setattr(
        world.store, "_get_or_refuse",
        _rig_second_read(world.store,
                          "UPDATE items SET claimed_by='workerA'"))
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, True, "ok")
    assert exc.value.code == "conflict"


def test_changes_conflict_when_round_moves_under_lock(world, monkeypatch):
    from keeper_util import to_in_review
    key = _mk(world)
    to_in_review(world, key)
    monkeypatch.setattr(
        world.store, "_get_or_refuse",
        _rig_second_read(world.store, "UPDATE items SET round=round+1"))
    with pytest.raises(Refused) as exc:
        world.store.verdict(world.p("reviewer"), key, False, "no")
    assert exc.value.code == "conflict"


# ------------------------------------------------- S2-5: keyword boundaries
@pytest.mark.parametrize("title", ["rotate my_secret", "SECRET_KEY rollout",
                                   "keeper2 migration", "the GATEWAY fix"])
def test_keyword_letter_boundaries_match(world, title):
    key = _mk(world, title=title, track="T0")
    assert world.store._item(key)["review_tier"] == "high", title


def test_keyword_keystore_stays_standard(world):
    key = _mk(world, title="Read the keystore", track="T0")
    assert world.store._item(key)["review_tier"] == "standard"


# ------------------------------------------------- S2-6: edit auto-raise
def test_edit_title_to_keyword_raises_standard_to_high(world):
    key = _mk(world, title="Ordinary lane", track="T0")
    assert world.store._item(key)["review_tier"] == "standard"
    world.store.edit_definition(world.p("router"), key,
                                {"title": "rotate the gateway secret"})
    assert world.store._item(key)["review_tier"] == "high"
    assert world.store.verify_integrity() == []


def test_edit_track_to_keyword_raises_standard_to_high(world):
    key = _mk(world, title="Ordinary lane", track="T0")
    world.store.edit_definition(world.p("router"), key,
                                {"track": "security"})
    assert world.store._item(key)["review_tier"] == "high"


def test_edit_never_lowers_high(world):
    key = _mk(world, title="Rotate the secret", track="T0")
    assert world.store._item(key)["review_tier"] == "high"
    world.store.edit_definition(world.p("router"), key,
                                {"title": "Ordinary lane"})
    assert world.store._item(key)["review_tier"] == "high"


# ------------------------------------------------- S1-1: epoch-less changes ignored
def test_epochless_changes_requested_ignored_by_streak(world):
    from keeper_util import to_in_review
    key = _mk(world)
    to_in_review(world, key)
    world.store._append(world.p("reviewer"), key, "changes_requested",
                         {"state": "claimed", "reason": "old, no epoch"},
                         world.clock())
    out = world.store.verdict(world.p("reviewer"), key, False, "first")
    assert out["state"] == "claimed"
