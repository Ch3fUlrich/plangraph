"""STALE-PREMISE-WATERMARK-RECONFIRM: the premise gate reads decision ids from
premise fields only, the item's own source decision is never "newer", and
``reconfirm KEY D-NNN`` is the one audited verb that raises a per-item floor
read by every claim path.
"""
from __future__ import annotations

import os

import pytest

from keeper_util import World, defn
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter


@pytest.fixture
def decisions(tmp_path):
    path = tmp_path / "decisions-log.md"
    path.write_text("| ID | project | decision |\n|---|---|---|\n", encoding="utf-8")
    return path


@pytest.fixture
def w(tmp_path, decisions):
    world = World(tmp_path)
    world.store.ctx = {"decisions": DecisionsAdapter(decisions)}
    try:
        yield world
    finally:
        world.close()


def _decide(path, did, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"| D-{did} | demo | {text} |\n")


def _newer(w, key):
    return w.store.show(w.p("router"), key)["newer_decisions"]


def _refuses(w, key):
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), key)
    assert "premise outdated" in exc.value.msg, exc.value.msg
    return exc.value.msg


_GOALS = iter(range(1000))


def _mk(w, *lanes):
    """Create *lanes* in one batch under a fresh goal (a batch without a goal is refused)."""
    goal = f"GOAL{next(_GOALS)}"
    w.store.create_items(w.p("router"), [*lanes, defn(goal, [d["id"] for d in lanes], kind="goal")])


def _created(w, key):
    return next(e["payload"] for e in keeper_events.read_events(w.conn)
                if e["key"] == key and e["type"] == "item_created")


_METADATA = [("session", "D-999999"), ("design_ref", "docs/D-999999.md"), ("similar", ["D-999999"]),
             ("category", "D-999999"), ("supersedes", "D-999999")]


@pytest.mark.parametrize("field,value", _METADATA)
def test_definition_metadata_does_not_advance_another_items_watermark(w, decisions, field, value):
    # C cites D-5800; A carries the metadata; B cites nothing and is defined after A, so B's
    # watermark is the clock at its own definition and no metadata id may be in it.
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", **{field: value}))
    _mk(w, defn("B"))
    assert "D-999999" not in _created(w, "demo:A").get("note", "")
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B now also covers the web; supersedes the old scope")
    assert "D-5900" in _refuses(w, "demo:B")
    assert _newer(w, "demo:B") == ["D-5900"]
    # the item itself: its own metadata is never a citation either
    _decide(decisions, 5901, "demo:A scope changed")
    assert "D-5901" in _refuses(w, "demo:A")


def test_a_session_is_metadata_on_the_creation_event(w):
    _mk(w, defn("A", session="D-999999"))
    assert _created(w, "demo:A")["session"] == "D-999999"
    assert "D-999999" not in (w.store._item("demo:A")["note"] or "")


def test_an_imported_session_stays_out_of_the_premise(w, decisions, tmp_path):
    from plangraph.keeper.importer import load_dir
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "p.yaml").write_text(
        "items:\n"
        "- {project: demo, id: C, title: c, description: per D-5800, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: A, title: a, session: D-999999, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: Z, title: z, owner: tester, track: T0, estimate_h: 1, kind: lane}\n"
        "- {project: demo, id: GOAL, title: g, kind: goal, depends_on: [A, Z, C], owner: tester, track: T0, estimate_h: 1}\n",
        encoding="utf-8")
    items = load_dir(plan)
    assert [i["session"] for i in items if i["id"] == "A"] == ["D-999999"]
    assert all("D-999999" not in (i.get("note") or "") for i in items)
    w.store.import_items(w.p("router"), items)
    assert _created(w, "demo:A")["session"] == "D-999999"
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:Z scope changed")          # Z is imported after C: its watermark is 5800
    assert "D-5900" in _refuses(w, "demo:Z")


@pytest.mark.parametrize("bad", [7, "", "a\nb", "a\rb", "x" * 201, "password: svcacct/Zq9xk2LmPw"])
def test_a_session_must_be_a_one_line_credential_free_string(w, bad):
    with pytest.raises(Refused) as exc:
        _mk(w, defn("A", session=bad))
    assert "session" in exc.value.msg
    _mk(w, defn("OK", session="sess-1"))       # the same batch shape passes with a good one


@pytest.mark.parametrize("field", ["title", "description", "done_criteria", "note", "source"])
def test_every_premise_field_of_a_definition_moves_the_clock(w, decisions, field):
    value = "D-5800" if field == "source" else "per D-5800"     # a source counts as one whole reference
    _mk(w, defn("X", **{field: value}))
    _mk(w, defn("B"))
    assert _created(w, "demo:X")[field] == value
    assert not {"title", "done_criteria", "note", "source"} & set(_created(w, "demo:B"))   # no cite, no key
    assert w.store._decision_clock()["marks"][-1] == 5800
    _decide(decisions, 5900, "demo:B scope changed")
    assert _newer(w, "demo:B") == ["D-5900"]            # the event-derived fallback


@pytest.mark.parametrize("field", ["title", "description", "done_criteria", "note"])
def test_an_edit_of_a_premise_field_moves_the_clock(w, decisions, field):
    _mk(w, defn("X"))
    assert w.store._decision_clock()["marks"][-1] == -1
    edit = {field: "per D-5900"}
    if field == "done_criteria":      # the criteria ledger wants a reason; it is not premise text
        edit.update(criteria_reason="clarify", criteria_evidence="ledger entry")
    w.store.edit_definition(w.p("router"), "demo:X", edit)
    assert w.store._decision_clock()["marks"][-1] == 5900


def test_an_edited_premise_moves_the_watermark_through_its_fields(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    assert _newer(w, "demo:A") == ["D-5900"]
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "per D-5900"})
    assert _newer(w, "demo:A") == []


def test_the_items_own_source_decision_claims_without_force(w, decisions):
    # a definition that cites D-5800 comes first, so A's fallback watermark is 5800 and
    # only its own source citation can keep D-5862 from being newer
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", source="D-5862"), defn("B", description="per D-5800", source="D-5850"))
    _decide(decisions, 5862, "demo:A discussion of the rollout")
    assert _newer(w, "demo:A") == []
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    # another row naming the item, newer than the source, still refuses
    _decide(decisions, 5870, "demo:B scope changed")
    assert "D-5870" in _refuses(w, "demo:B")


def test_a_row_that_is_not_the_source_and_not_reconfirmed_keeps_the_gate(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5858, "OPERATOR order: demo:A ordered first, GO")
    assert "D-5858" in _refuses(w, "demo:A")


def test_reconfirm_clears_exactly_up_to_the_decision_and_is_audited(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    _decide(decisions, 5910, "demo:A scope changed again")
    before = w.store.show(w.p("router"), "demo:A")["description"]
    assert w.store.reconfirm(w.p("router"), "demo:A", "D-5900") == {
        "key": "demo:A", "reconfirmed": "D-5900"}
    assert w.store.show(w.p("router"), "demo:A")["description"] == before
    assert _newer(w, "demo:A") == ["D-5910"]               # exactly up to D-5900
    marks = [e for e in keeper_events.read_events(w.conn)
             if e["key"] == "demo:A" and e["type"] == "premise_reconfirmed"]
    assert len(marks) == 1 and marks[0]["actor"] == "router"
    assert marks[0]["payload"]["decision"] == "D-5900"
    w.store.reconfirm(w.p("router"), "demo:A", "D-5910")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    w.store.release(w.p("l1main"), "demo:A", 1)
    _decide(decisions, 5950, "demo:A changes once more")   # a later decision refuses again
    assert "D-5950" in _refuses(w, "demo:A")
    assert _newer(w, "demo:A") == ["D-5950"]


def test_the_floor_is_per_item(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("B", description="per D-5800"),
                                         defn("GOAL", ["A", "B"], kind="goal")])
    _decide(decisions, 5900, "demo:A and demo:B scope changed")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert _newer(w, "demo:A") == [] and _newer(w, "demo:B") == ["D-5900"]


def test_reconfirm_also_clears_a_retired_premise(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-9")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_a_system_lease_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("PREM", description="per D-5"),
                                         defn("GOAL", ["PREM"], kind="goal")])
    _decide(decisions, 9, "supersedes D-5 for demo")
    with pytest.raises(Refused) as exc:
        w.store.worker_claim_lease(w.p("system"), "demo:PREM")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:PREM", "D-9")
    assert w.store.worker_claim_lease(w.p("system"), "demo:PREM")["epoch"] == 1


def test_a_system_lease_refuses_a_newer_decision_until_reconfirmed_and_again_after_a_later_one(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    with pytest.raises(Refused) as exc:                      # refused before the reconfirm
        w.store.worker_claim_lease(w.p("system"), "demo:A")
    assert "premise outdated: D-5900" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert w.store.worker_claim_lease(w.p("system"), "demo:A")["epoch"] == 1
    w.store.release(w.p("router"), "demo:A", 1)
    _decide(decisions, 5950, "demo:A changes again")
    with pytest.raises(Refused) as exc:                      # and again after a later decision
        w.store.worker_claim_lease(w.p("system"), "demo:A")
    assert "premise outdated: D-5950" in exc.value.msg


def test_ready_reads_the_same_floor(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    groups = w.store._ready_groups(project="demo")
    assert [i["key"] for i in groups["items"]] != ["demo:A"] or groups["needs_reconcile"]
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    groups = w.store._ready_groups(project="demo")
    assert "demo:A" in [i["key"] for i in groups["items"]]
    assert "demo:A" not in [i["key"] for i in groups["needs_reconcile"]]


def test_reconfirm_refuses_an_unknown_decision_a_bad_id_and_a_stranger(w, decisions):
    w.store.create_items(w.p("router"), [defn("A", description="per D-5800"),
                                         defn("GOAL", ["A"], kind="goal")])
    _decide(decisions, 5900, "demo:A scope changed")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "D-77777")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("l1other"), "demo:A", "D-5900")
    with pytest.raises(Refused):                    # the router records it, not the claimant's L1
        w.store.reconfirm(w.p("l1main"), "demo:A", "D-5900")
    with pytest.raises(Refused):
        w.store.reconfirm(w.p("router"), "demo:A", "not-a-decision")
    assert _newer(w, "demo:A") == ["D-5900"]


def test_the_cli_and_the_api_carry_the_verb():
    from plangraph.keeper import api_routes, cli
    assert "reconfirm" in api_routes.WRITE_VERBS and "reconfirm" in api_routes.ROLE_CHECKED_VERBS
    args = cli.build_parser().parse_args(["reconfirm", "demo:A", "D-5900"])
    assert (args.key, args.decision) == ("demo:A", "D-5900")


def test_a_free_text_source_is_not_provenance_of_a_decision(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", source="operator order, see D-5862 and the thread"))
    _decide(decisions, 5862, "demo:A discussion of the rollout")
    assert "D-5862" in _refuses(w, "demo:A")


def test_unconfirmed_order_and_same_session_rows_refuse_claim(w, decisions):
    # the old lane let a terse GO row and a row of the item's own session claim without a
    # reconfirm; nothing is read from the prose of a row, so both refuse until reconfirmed
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("A", session="sess-77"), defn("B"))
    _decide(decisions, 5858, "GO demo:A")
    _decide(decisions, 5859, "(sess-77) demo:B ordered first, GO")
    assert "D-5858" in _refuses(w, "demo:A")
    assert "D-5859" in _refuses(w, "demo:B")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5858")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1


def test_a_metadata_only_edit_does_not_move_the_fallback_anchor(w, decisions):
    _mk(w, defn("C", description="per D-5800"))
    _mk(w, defn("U"))
    _mk(w, defn("E", description="per D-6000"))
    w.store.edit_definition(w.p("router"), "demo:U", {"host": "elsewhere"})   # no premise text
    _decide(decisions, 5950, "demo:U scope changed")
    assert _newer(w, "demo:U") == ["D-5950"]           # still read against the premise at its creation
    w.store.edit_definition(w.p("router"), "demo:U", {"note": "reworded"})    # a real premise change
    assert _newer(w, "demo:U") == []                   # now anchored after D-6000


def test_a_premise_edit_invalidates_the_floor_of_the_earlier_read(w, decisions):
    _mk(w, defn("A", description="per D-5800"))
    _decide(decisions, 5, "an old plan")
    _decide(decisions, 9, "supersedes D-5 for demo")
    _decide(decisions, 5900, "demo:A scope changed")
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 1
    w.store.release(w.p("router"), "demo:A", 1)
    # the router read a premise that did not cite D-5; a later edit introduces the retired id
    w.store.edit_definition(w.p("router"), "demo:A", {"description": "per D-5800 and D-5"})
    with pytest.raises(Refused) as exc:
        w.store.claim(w.p("l1main"), "demo:A")
    assert "premise D-5 superseded by D-9" in exc.value.msg
    w.store.reconfirm(w.p("router"), "demo:A", "D-5900")      # a new read clears it again
    assert w.store.claim(w.p("l1main"), "demo:A")["epoch"] == 2


def test_system_and_phase_worker_leases_read_the_decision_log_fresh_and_strict(w, decisions):
    from plangraph.keeper import auth
    token = auth.add_principal(w.conn, "phase-runner", "phase-worker", ["demo"])
    runner = auth.authenticate(w.conn, token)
    _mk(w, defn("A", description="per D-5800"))
    _decide(decisions, 5900, "demo:Z scope changed")           # same length as the row below
    assert _newer(w, "demo:A") == []                           # caches the book of this file version
    stat = decisions.stat()
    decisions.write_text(decisions.read_text(encoding="utf-8").replace("demo:Z", "demo:A"), encoding="utf-8")
    os.utime(decisions, ns=(stat.st_atime_ns, stat.st_mtime_ns))     # same size, same mtime
    for who in (w.p("system"), runner):
        with pytest.raises(Refused) as exc:
            w.store.worker_claim_lease(who, "demo:A")
        assert "premise outdated: D-5900" in exc.value.msg
    with pytest.raises(Refused):                               # the ordinary claim agrees
        w.store.claim(w.p("l1main"), "demo:A")
    decisions.unlink()                                         # an unreadable log refuses both doors
    for who in (w.p("system"), runner):
        with pytest.raises(Refused) as exc:
            w.store.worker_claim_lease(who, "demo:A")
        assert "cannot read decisions file" in exc.value.msg
