"""Track-keyed ancestry floors (plangraph:KEEPER-EVIDENCE-TRACK-FLOOR, D-3335).

A commit+ci evidence may resolve to a ``<project>/<track>`` repo
(KEEPER-EVIDENCE-TRACK-REPO); its ancestry floor must come from that repo's
own head recorded on the floor event (``bases``), not from the primary
``base``.  A floor event predating ``bases`` (or whose track repo was not
configured) carries no track floor, so the floor check is skipped for that
repo only; a configured repo with no usable head records ``"unknown"`` and
refuses.  A key that is no legitimate key of the item, and a malformed
``bases`` payload, fail closed.  The fakes of ``test_evidence_track_repo`` are
reused here by import, never copied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evidence_fakes import HEAD_SHA, SHA, make_ctx, verify
from keeper_util import World, defn
from plangraph.keeper import auth, evidence
from plangraph.keeper.evidence_floor import check_floor
from test_evidence_track_repo import (
    FLOOR,
    ITEM,
    PRIMARY_REPO,
    PROJECT,
    TRACK,
    TRACK_KEY,
    TRACK_REPO,
    TrackGit,
    _ci,
    _git,
)

#: The track repo's head at the floor event: the floor of a track commit.
TRACK_FLOOR = "e" * 40

DEMO = "demo"
DEMO_KEY = f"{DEMO}:T1"
DEMO_TRACK_KEY = f"{DEMO}/{TRACK}"


def _world(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    return World(root)


def _items(w, project):
    w.store.create_items(w.p("router"), [
        defn("T1", [], project=project, track=TRACK, evidence_kinds=["commit+ci"]),
        defn("GOAL", ["T1"], project=project, kind="goal"),
    ])


def _floor_ctx(tmp_path, payload, *, name="w", git=None, ci=None):
    """The Context the store builds for ITEM after a floor event *payload*."""
    w = _world(tmp_path, name)
    _items(w, PROJECT)
    w.store._append(w.p("router"), ITEM["key"], "released", payload, w.clock())
    w.store.ctx = SimpleNamespace(git=git or _git(), ci=ci or _ci())
    ctx = w.store._ctx(w.store._item(ITEM["key"]), w.clock())
    w.close()
    return ctx


def _event_payloads(store, key):
    rows = store.conn.execute(
        "SELECT type, payload FROM events WHERE key=?"
        " AND type IN ('claimed','released') ORDER BY seq", (key,)).fetchall()
    return [(row["type"], json.loads(row["payload"])) for row in rows]


# --------------------------------------------------------------------------- #
# (a) a floor event with the primary base only carries no track floor
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload, expected_bases", [
    ({"base": FLOOR}, {}),
    ({"base": FLOOR, "bases": {PROJECT: FLOOR}}, {PROJECT: FLOOR}),
])
def test_floor_event_with_primary_base_only_verifies_track_commit(
        tmp_path, payload, expected_bases):
    # descends=False: were any floor applied to the track repo, the answer
    # would refuse; a verify here proves the check was skipped for that repo.
    ctx = _floor_ctx(tmp_path, payload, git=_git(descends=False))
    assert ctx.bind_floor_sha == FLOOR
    assert ctx.bind_floor_shas == expected_bases
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is True, res.detail


# --------------------------------------------------------------------------- #
# (b)/(c) the track floor comes from bases[track_key]
# --------------------------------------------------------------------------- #

def test_track_commit_descending_from_track_floor_verifies(tmp_path):
    git = _git()
    ctx = _floor_ctx(tmp_path, {"base": FLOOR,
                                "bases": {PROJECT: FLOOR, TRACK_KEY: TRACK_FLOOR}},
                     git=git)
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is True, res.detail
    assert git.ancestor_calls[-1] == (TRACK_REPO, TRACK_FLOOR, SHA)


def test_track_commit_not_descending_from_track_floor_refused(tmp_path):
    git = _git(descends=False)
    ctx = _floor_ctx(tmp_path, {"base": FLOOR,
                                "bases": {PROJECT: FLOOR, TRACK_KEY: TRACK_FLOOR}},
                     git=git)
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "does not descend" in res.detail
    assert TRACK_FLOOR[:12] in res.detail


# --------------------------------------------------------------------------- #
# (d) a primary commit is floored exactly as before
# --------------------------------------------------------------------------- #

def test_primary_commit_floor_unchanged(tmp_path):
    payload = {"base": FLOOR, "bases": {PROJECT: FLOOR, TRACK_KEY: TRACK_FLOOR}}
    ctx = _floor_ctx(tmp_path, payload, name="pass", git=_git(primary_has=True))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is True, res.detail

    ctx = _floor_ctx(tmp_path, payload, name="refuse",
                     git=_git(primary_has=True, descends=False))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert ("does not descend from the item's last release/re-claim"
            f" ({FLOOR[:12]})") in res.detail


# --------------------------------------------------------------------------- #
# (e) a malformed bases payload fails closed
# --------------------------------------------------------------------------- #

def test_malformed_bases_payload_fails_closed(tmp_path):
    # a non-dict bases
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": "junk"}, name="nondict")
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "malformed per-repo floor (bases)" in res.detail

    # a track entry that is not a lowercase 40/64 hex sha
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": {TRACK_KEY: "NOT-A-SHA"}},
                     name="badentry")
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "malformed per-repo floor (bases)" in res.detail

    # a malformed bases poisons the floor for the primary repo too
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": []}, name="primary",
                     git=_git(primary_has=True))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "malformed per-repo floor (bases)" in res.detail


def test_malformed_bind_floor_shas_refused():
    for shas in ("junk", [FLOOR], {TRACK_KEY: "NOT-A-SHA"}, {TRACK_KEY: None}):
        ctx = make_ctx(git=_git(), ci=_ci(), bind_floor_required=True,
                       bind_floor_sha=FLOOR, bind_floor_shas=shas)
        res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
        assert res.ok is False, shas
        assert "malformed per-repo floor (bases)" in res.detail


# --------------------------------------------------------------------------- #
# (f) claim and release record bases for every repo that holds a head
# --------------------------------------------------------------------------- #

def test_claim_and_release_payloads_carry_bases_for_both_keys(tmp_path):
    w = _world(tmp_path, "w")
    _items(w, DEMO)
    git = TrackGit({DEMO: PRIMARY_REPO, DEMO_TRACK_KEY: TRACK_REPO},
                   {PRIMARY_REPO: False, TRACK_REPO: True},
                   refs={DEMO: "main", DEMO_TRACK_KEY: "main"})
    w.store.ctx = SimpleNamespace(git=git)
    epoch = w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)["epoch"]
    w.store.release(w.p("l1main"), DEMO_KEY, epoch)
    payloads = dict(_event_payloads(w.store, DEMO_KEY))
    for kind in ("claimed", "released"):
        assert payloads[kind]["base"] == HEAD_SHA
        assert payloads[kind]["bases"] == {DEMO: HEAD_SHA, DEMO_TRACK_KEY: HEAD_SHA}
    w.close()


def test_claim_and_release_payloads_carry_only_the_primary_key_without_track_repo(tmp_path):
    w = _world(tmp_path, "w")
    _items(w, DEMO)
    git = TrackGit({DEMO: PRIMARY_REPO}, {PRIMARY_REPO: False})
    w.store.ctx = SimpleNamespace(git=git)
    epoch = w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)["epoch"]
    w.store.release(w.p("l1main"), DEMO_KEY, epoch)
    payloads = dict(_event_payloads(w.store, DEMO_KEY))
    for kind in ("claimed", "released"):
        assert payloads[kind]["base"] == HEAD_SHA
        assert payloads[kind]["bases"] == {DEMO: HEAD_SHA}
    w.close()


class _SplitHeadGit(TrackGit):
    """Only the track repo's head is known; the primary head is unknown."""

    def head_sha(self, repo, ref="main"):
        return {TRACK_REPO: HEAD_SHA}.get(repo)


def test_bases_skip_a_key_whose_head_is_unknown(tmp_path):
    # the primary head unknown, the track head known: only the known key is
    # recorded and ``base`` is absent
    w = _world(tmp_path, "w")
    _items(w, DEMO)
    git = _SplitHeadGit({DEMO: PRIMARY_REPO, DEMO_TRACK_KEY: TRACK_REPO},
                        {PRIMARY_REPO: False, TRACK_REPO: True})
    w.store.ctx = SimpleNamespace(git=git)
    w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)
    _, payload = _event_payloads(w.store, DEMO_KEY)[0]
    assert "base" not in payload
    assert payload["bases"] == {DEMO_TRACK_KEY: HEAD_SHA}
    w.close()

    # both heads unknown: ``base`` is still absent (primary unchanged), but
    # the configured track repo now records "unknown" so its head lookup
    # failure cannot look like "not configured"
    w = _world(tmp_path, "w2")
    _items(w, DEMO)
    git = TrackGit({DEMO: PRIMARY_REPO, DEMO_TRACK_KEY: TRACK_REPO},
                   {PRIMARY_REPO: False, TRACK_REPO: True}, head=None)
    w.store.ctx = SimpleNamespace(git=git)
    w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)
    _, payload = _event_payloads(w.store, DEMO_KEY)[0]
    assert "base" not in payload
    assert payload["bases"] == {DEMO_TRACK_KEY: "unknown"}
    w.close()


def test_payload_stays_legacy_when_no_repo_holds_a_head(tmp_path):
    # no track repo configured and an unknown primary head: nothing to say,
    # so neither ``base`` nor ``bases`` is recorded and the payload stays
    # byte-identical to a pre-change one (the pinned legacy chain of
    # test_keeper_pg_parity), reading back as a legacy floor event
    w = _world(tmp_path, "w")
    _items(w, DEMO)
    git = TrackGit({DEMO: PRIMARY_REPO}, {PRIMARY_REPO: False}, head=None)
    w.store.ctx = SimpleNamespace(git=git)
    w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)
    _, payload = _event_payloads(w.store, DEMO_KEY)[0]
    assert "base" not in payload
    assert "bases" not in payload
    w.close()


#: A slashed key that is neither the item's project nor its project/track.
FOREIGN_KEY = "other/tr"  # (r2 a)/(b): a key that is no legitimate key of the item


def test_foreign_slashed_key_refused_without_ancestry_lookup():
    git = _git()
    ctx = make_ctx(git=git, ci=_ci(), bind_floor_required=True,
                   bind_floor_sha=FLOOR, bind_floor_shas={PROJECT: FLOOR})
    res = check_floor(ctx, TRACK_REPO, SHA, {}, FOREIGN_KEY, ITEM)
    assert res is not None and res.ok is False
    assert f"commit resolves to repo {FOREIGN_KEY}" in res.detail
    assert "no ancestry floor for this item" in res.detail
    assert git.ancestor_calls == []


def test_foreign_slashed_key_refused_when_item_is_none():
    git = _git()
    ctx = make_ctx(git=git, ci=_ci(), bind_floor_required=True,
                   bind_floor_sha=FLOOR, bind_floor_shas={PROJECT: FLOOR})
    res = check_floor(ctx, TRACK_REPO, SHA, {}, FOREIGN_KEY, None)
    assert res is not None and res.ok is False
    assert "no ancestry floor for this item" in res.detail
    assert git.ancestor_calls == []


UPPER_SHA = "C" * 40  # (r2 c): an uppercase sha in bases is malformed


def test_uppercase_sha_in_bases_is_malformed(tmp_path):
    payload = {"base": FLOOR, "bases": {PROJECT: FLOOR, TRACK_KEY: UPPER_SHA}}
    for name, git in (("track", _git()), ("primary", _git(primary_has=True))):
        ctx = _floor_ctx(tmp_path, payload, name=name, git=git)
        res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
        assert res.ok is False, name
        assert "malformed per-repo floor (bases)" in res.detail


# (r2 d): a failing track head lookup records "unknown" and refuses the track
class _RaiseTrackHeadGit(TrackGit):
    """The track repo is configured but its head lookup raises."""

    def head_sha(self, repo, ref="main"):
        if repo == TRACK_REPO:
            raise RuntimeError("head lookup failed")
        return super().head_sha(repo, ref)


def _scoped(w, name="l1pg"):
    """A stored l1 principal scoped to PROJECT, able to claim its items."""
    token = auth.add_principal(w.conn, name, "l1", [PROJECT], family="teama")
    w.principals[name] = auth.authenticate(w.conn, token)
    return w.principals[name]


def test_track_head_failure_records_unknown_and_refuses_track_commit(tmp_path):
    w = _world(tmp_path, "w")
    _items(w, PROJECT)
    p = _scoped(w)
    git = _RaiseTrackHeadGit({PROJECT: PRIMARY_REPO, TRACK_KEY: TRACK_REPO},
                            {PRIMARY_REPO: False, TRACK_REPO: True},
                            refs={PROJECT: "main", TRACK_KEY: "main"})
    w.store.ctx = SimpleNamespace(git=git)
    epoch = w.store.claim(p, ITEM["key"], lease_s=7200)["epoch"]
    w.store.release(p, ITEM["key"], epoch)
    released = dict(_event_payloads(w.store, ITEM["key"]))["released"]
    assert released["bases"] == {PROJECT: HEAD_SHA, TRACK_KEY: "unknown"}
    w.close()

    # a later track commit floored against "unknown" gets the floor refusal
    ctx = _floor_ctx(tmp_path, released, name="later")
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert ("no ancestry floor recorded for this item's last release/re-claim;"
            " refusing") in res.detail


# (r2 e): a floor event claimed by another principal floors the track repo

def _claimed_floor_ctx(tmp_path, payload, *, name="w", git=None, ci=None):
    """The store's Context for ITEM after a ``claimed`` floor event by
    another principal (not a release) carrying *payload*."""
    w = _world(tmp_path, name)
    _items(w, PROJECT)
    w.store.ctx = SimpleNamespace(git=git or _git(), ci=ci or _ci())
    w.store.claim(_scoped(w), ITEM["key"], lease_s=7200)
    w.store._append(w.p("l1other"), ITEM["key"], "claimed", payload, w.clock())
    ctx = w.store._ctx(w.store._item(ITEM["key"]), w.clock())
    w.close()
    return ctx


def test_claimed_by_another_principal_floors_track_commit(tmp_path):
    payload = {"base": FLOOR, "bases": {PROJECT: FLOOR, TRACK_KEY: TRACK_FLOOR}}
    git = _git()
    ctx = _claimed_floor_ctx(tmp_path, payload, git=git)
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is True, res.detail
    assert git.ancestor_calls[-1] == (TRACK_REPO, TRACK_FLOOR, SHA)

    ctx = _claimed_floor_ctx(tmp_path, payload, name="refuse",
                             git=_git(descends=False))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "does not descend" in res.detail
    assert TRACK_FLOOR[:12] in res.detail


# (r2 f): a primary commit never falls back to bases[primary]
def test_primary_commit_with_valid_bases_and_no_floor_sha_refused():
    ctx = make_ctx(git=_git(primary_has=True), ci=_ci(), bind_floor_required=True,
                   bind_floor_sha=None,
                   bind_floor_shas={PROJECT: FLOOR, TRACK_KEY: TRACK_FLOOR})
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert ("no ancestry floor recorded for this item's last release/re-claim;"
            " refusing") in res.detail


# (r2 g): a track key absent from bases keeps today's skip
def test_track_key_absent_from_bases_skips_floor(tmp_path):
    # the track repo was not configured at event time: no track entry in
    # bases, so no floor applies to that repo and the commit verifies
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": {PROJECT: FLOOR}},
                     name="absent", git=_git(descends=False))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is True, res.detail
