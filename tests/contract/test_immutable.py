"""Contract 5: DONE (and cancelled) items are immutable; refusals leave no trace."""
import pytest

from contract_world import (claim, err, head, make_plan, need, ok, show, to_done,
                            uid)

MUTATIONS = {
    "claim": ("worker2", {}),
    "heartbeat": ("worker1", {"epoch": 1}),
    "release": ("router", {"epoch": 1}),
    "submit": ("worker1", {"epoch": 1}),
    "add_evidence": ("worker1", {"epoch": 1, "kind": "decision", "ref": {"id": "D-1"}}),
    "add_evidence_router": ("router", {"kind": "decision", "ref": {"id": "D-1"}}),
    "verdict": ("reviewer2", {"ok": True}),
    "delegate": ("worker1", {"worker": "worker2"}),
    "cancel": ("router", {"reason": "late"}),
    "edit_definition": ("router", {"fields": {"title": "renamed"}}),
    "set_priority": ("router", {"n": 9}),
}


def _all_refused(kp, key, before):
    for verb, (who, body) in MUTATIONS.items():
        err(kp.post(verb, who, {"key": key, **body}), 409, "locked")
    assert head(kp) == before, "a refused write must not append an event"


def test_done_item_refuses_every_mutation(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    to_done(kp, plan.key)
    frozen = show(kp, plan.key)
    _all_refused(kp, plan.key, head(kp))
    assert show(kp, plan.key) == frozen


def test_cancelled_item_refuses_every_mutation(kp):
    plan = make_plan(kp)
    ok(kp.post("cancel", "router", {"key": plan.key, "reason": "dropped"}), "Cancel")
    frozen = show(kp, plan.key)
    _all_refused(kp, plan.key, head(kp))
    assert show(kp, plan.key) == frozen


def test_recreating_an_existing_key_is_refused(kp):
    need(kp, "decisions")
    plan = make_plan(kp, 2)
    done, live = plan.lanes
    to_done(kp, done)
    for key, code in ((done, "locked"), (live, "conflict")):
        project, ident = key.split(":", 1)
        item = {"project": project, "id": ident, "kind": "lane", "title": "again"}
        err(kp.post("create_items", "router", {"items": [item]}), 409, code)
        err(kp.post("import_items", "router", {"items": [item]}), 409, code)


def test_done_work_unblocks_dependents_and_can_be_depended_upon(kp):
    need(kp, "decisions")
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    to_done(kp, first)
    ready = [r["key"] for r in ok(kp.get("/v1/ready?all=1&fmt=json", "reader"), "Ready")["items"]]
    assert second in ready
    claim(kp, second)
    tag = uid()
    ok(kp.post("create_items", "router", {"items": [
        {"project": "demo", "id": "N-" + tag, "kind": "goal", "title": "after " + tag,
         "depends_on": [first]}]}), "Created")


def test_refused_writes_do_not_move_the_event_head(kp):
    plan = make_plan(kp)
    before = head(kp)
    err(kp.post("claim", "reader", {"key": plan.key}), 403, "forbidden")
    err(kp.post("set_priority", "worker1", {"key": plan.key, "n": 3}), 403, "forbidden")
    err(kp.post("claim", "worker1", {"key": "demo:NOPE-" + uid()}), 404, "not_found")
    err(kp.post("create_items", "router", {"items": [{"project": "demo"}]}), 400, "validation")
    assert head(kp) == before


@pytest.mark.parametrize("fields", [{"id": "X"}, {"state": "done"}, {"epoch": 3}, {"claimed_by": "x"},
                                    {"kind": "goal"}])
def test_only_definition_fields_are_editable(kp, fields):
    plan = make_plan(kp)
    err(kp.post("edit_definition", "router", {"key": plan.key, "fields": fields}), 400, "validation")
    shown = show(kp, plan.key)
    assert shown["state"] == "open" and shown["epoch"] == 0
