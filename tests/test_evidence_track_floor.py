"""Track-keyed ancestry floors (plangraph:KEEPER-EVIDENCE-TRACK-FLOOR, D-3335).

A commit+ci evidence may resolve to a ``<project>/<track>`` repo
(KEEPER-EVIDENCE-TRACK-REPO); its ancestry floor must come from that repo's
own head recorded on the floor event (``bases``), not from the primary
``base``.  A floor event predating ``bases`` (or whose track repo had no head)
carries no track floor, so the floor check is skipped for that repo only.  A
malformed ``bases`` payload fails closed.  The fakes of
``test_evidence_track_repo`` are reused here by import, never copied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evidence_fakes import HEAD_SHA, SHA, make_ctx, verify
from keeper_util import World, defn
from plangraph.keeper import evidence
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
    assert "no ancestry floor" in res.detail

    # a track entry that is not a lowercase 40/64 hex sha
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": {TRACK_KEY: "NOT-A-SHA"}},
                     name="badentry")
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "no ancestry floor" in res.detail

    # a malformed bases poisons the floor for the primary repo too
    ctx = _floor_ctx(tmp_path, {"base": FLOOR, "bases": []}, name="primary",
                     git=_git(primary_has=True))
    res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
    assert res.ok is False
    assert "no ancestry floor" in res.detail


def test_malformed_bind_floor_shas_refused():
    for shas in ("junk", [FLOOR], {TRACK_KEY: "NOT-A-SHA"}, {TRACK_KEY: None}):
        ctx = make_ctx(git=_git(), ci=_ci(), bind_floor_required=True,
                       bind_floor_sha=FLOOR, bind_floor_shas=shas)
        res = verify("commit+ci", {"commit": SHA}, ctx, item=ITEM)
        assert res.ok is False, shas
        assert "no ancestry floor" in res.detail


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

    # both heads unknown: neither ``base`` nor ``bases`` is recorded, so the
    # payload stays byte-identical to a pre-change one (the pinned legacy
    # chain of test_keeper_pg_parity) and reads back as a legacy floor event
    w = _world(tmp_path, "w2")
    _items(w, DEMO)
    git = TrackGit({DEMO: PRIMARY_REPO, DEMO_TRACK_KEY: TRACK_REPO},
                   {PRIMARY_REPO: False, TRACK_REPO: True}, head=None)
    w.store.ctx = SimpleNamespace(git=git)
    w.store.claim(w.p("l1main"), DEMO_KEY, lease_s=7200)
    _, payload = _event_payloads(w.store, DEMO_KEY)[0]
    assert "base" not in payload
    assert "bases" not in payload
    w.close()
