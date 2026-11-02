"""KEEPER-SNAPSHOT-PG: ``Store.snapshot`` is set-based (a constant number of queries) and its
bytes equal the old per-item path's for the same state.

Mutations killed: a per-item query creeping back in (count grows with N), a verdict of another
round or epoch leaking into ``reviews``, evidence order or the ``decision_text`` source changing.
"""
from __future__ import annotations

import json

from keeper_util import World, defn, pass_review, seed, to_claimed, to_done, to_in_review
from plangraph.keeper.item_fields import MODEL_FIELDS, parse_choices, parse_kg_refs
from plangraph.keeper.store_read import _decode_models, _decode_run_ids
from plangraph.views_progress import progress


def _legacy_snapshot(store, now):
    """The pre-KEEPER-SNAPSHOT-PG ``Store._snapshot``: evidence and verdicts queried per item."""
    items = store._all_items()
    corrected, fixed = {}, {}
    for item in items:
        if item["corrects"]:
            corrected.setdefault(item["corrects"], []).append(item["key"])
        if item.get("fixes"):
            fixed.setdefault(item["fixes"], []).append(item["key"])
    created_ts, done_ts, reviewed_keys, merged_keys = store._snapshot_run_meta()
    answers = store.answer_map()
    out = []
    for item in items:
        reviews = [{"reviewer": v.get("reviewer"), "family": v.get("family")}
                   for v in store._round_verdicts(item)]
        out.append({
            "project": item["project"], "id": item["id"], "key": item["key"],
            "title": item["title"], "description": item["description"],
            "host": item["host"], "wait": item.get("wait"),
            **{name: item.get(name) for name in MODEL_FIELDS},
            "choices": parse_choices(item.get("choices")),
            "kg_refs": parse_kg_refs(item.get("kg_refs")),
            "waits_on_operator": bool(item.get("operator"))
            and item.get("state") not in ("done", "cancelled"),
            "kind": item["kind"], "track": item["track"],
            "owner": item["owner"], "state": item["state"],
            "estimate_h": item["estimate_h"], "estimate_src": item["estimate_src"],
            "depends_on": item["depends_on"], "corrects": item["corrects"],
            "done_criteria": item["done_criteria"],
            "evidence_kinds": item["evidence_kinds"],
            "operator": item["operator"], "priority": item["priority"],
            "review_tier": item["review_tier"],
            "source": item["source"], "note": item["note"],
            "epoch": item["epoch"], "claimed_by": item["claimed_by"],
            "worker_run": None, "reviewer_run": None,
            "lease_until": item["lease_until"], "model": item["model"],
            "review_waiver": item.get("review_waiver"), "reviews": reviews,
            "evidence": [{"kind": r["kind"], "ref": r["ref"], "epoch": r["epoch"],
                          "verified": r["verified"], "at": r["at"]}
                         for r in store._evidence(item["key"])],
            "updated_at": item["updated_at"],
            "corrected_by": sorted(corrected.get(item["key"], [])),
            "fixes": item.get("fixes"),
            "fixed_by": sorted(fixed.get(item["key"], [])),
            "writer_model": item.get("writer_model"),
            "seat_models": _decode_models(item.get("seat_models")),
            "run_ids": _decode_run_ids(item.get("run_ids")),
            "created_at": item.get("created_at") or created_ts.get(item["key"]),
            "done_at": done_ts.get(item["key"]),
            "reviewed": item["key"] in reviewed_keys,
            "merged": item["key"] in merged_keys,
            "answer": ({"text": answers[item["key"]]["text"],
                        "decision_id": answers[item["key"]]["decision_id"]}
                       if item["key"] in answers else None),
        })
    head = store.conn.execute(
        "SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return {"generated": store._iso(now), "head": head[0] if head else None,
            "head_seq": head[1] if head else 0, "items": out,
            "aliases": {a["old"]: a["new"] for a in store._aliases()},
            "activity": store._activity(now),
            "decision_text": store._decision_text(items, now),
            "progress": progress(out)}


class _Counting:
    """A connection proxy counting ``execute`` calls (the round trips on Postgres)."""

    def __init__(self, conn):
        self._conn, self.n = conn, 0

    def execute(self, *args, **kw):
        self.n += 1
        return self._conn.execute(*args, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _busy_world(tmp_path, lanes):
    world = World(tmp_path)
    keys = seed(world, [(f"L{i}", []) for i in range(lanes)], review_tier="high")
    to_done(world, keys[0])                              # review_passed + decision evidence
    to_in_review(world, keys[1])                         # in_review, evidence, no verdict
    to_claimed(world, keys[2])                           # claimed
    epoch = to_claimed(world, keys[3])
    world.store.add_evidence(world.p("l1main"), keys[3], epoch, "decision", {"id": "D-2"})
    world.store.submit(world.p("l1main"), keys[3], epoch)
    world.store.verdict(world.p("reviewer"), keys[3], False, "changes")   # failing verdict
    to_in_review(world, keys[4])
    world.store.verdict(world.p("reviewer"), keys[4], True, "first seat")  # high tier: stays in_review
    return world, keys


def test_snapshot_bytes_equal_the_per_item_path(tmp_path):
    world, _ = _busy_world(tmp_path, 8)
    try:
        now = world.store.now()
        new = json.dumps(world.store._snapshot(now)).encode()
        old = json.dumps(_legacy_snapshot(world.store, now)).encode()
        assert new == old
        assert any(i["reviews"] for i in json.loads(new)["items"]), "fixture needs a verdict"
        assert any(i["evidence"] for i in json.loads(new)["items"])
    finally:
        world.close()


def test_snapshot_query_count_is_independent_of_item_count(tmp_path):
    counts = []
    for lanes in (6, 30):
        sub = tmp_path / f"n{lanes}"
        sub.mkdir()
        world, _ = _busy_world(sub, lanes)
        try:
            proxy = _Counting(world.store.conn)
            world.store.conn = proxy
            world.store._snapshot(world.store.now())
            counts.append(proxy.n)
        finally:
            world.store.conn = proxy._conn
            world.close()
    assert counts[0] == counts[1], f"snapshot queries grow with items: {counts}"
    assert counts[0] <= 16, counts
