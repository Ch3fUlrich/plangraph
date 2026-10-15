"""Contract 9: definition verbs (create/import/edit/priority/kind/rehome/legacy)."""
from contract_world import claim, err, make_plan, ok, show, uid


def _item(tag, name="A", kind="lane", deps=(), project="demo", **extra):
    base = {"project": project, "id": f"{name}-{tag}", "kind": kind, "title": f"{name} {tag}",
            "description": "d", "depends_on": list(deps)}
    base.update(extra)
    return base


def test_create_items_both_body_forms_and_the_missing_description_warning(kp):
    tag = uid()
    plain = {"project": "demo", "id": "W-" + tag, "kind": "goal", "title": "no description"}
    out = ok(kp.post("create_items", "router", {"items": [plain]}), "Created")
    assert out["created"] == ["demo:W-" + tag] and out["warnings"]
    listed = ok(kp.call("POST", "/v1/create_items", "router", body=[_item(tag, "G", "goal")]), "Created")
    assert listed == {"created": ["demo:G-" + tag]}
    assert show(kp, "demo:G-" + tag)["kind"] == "goal"


def test_create_items_validation_matrix(kp):
    tag = uid()
    bad = [
        ({"project": "demo"}, "validation"),                                     # no id
        (_item(tag, kind="spaceship"), "validation"),
        (_item(tag, deps=["MISSING-" + tag]), "validation"),                      # unknown dep
        (_item(tag, title="x" * 201), "validation"),                              # cap
        (_item(tag, host="Bad Host"), "validation"),
        (_item(tag, review_tier="platinum"), "validation"),
        (_item(tag, wait="forever"), "validation"),
        (_item(tag, title="login with admin" + "/" + "x" * 12), "validation"),  # credential-shaped
        (_item(tag, review_tier="none", evidence_kinds=["commit+ci"]), "validation"),
    ]
    for item, code in bad:
        err(kp.post("create_items", "router", {"items": [item]}), 400, code)
    err(kp.post("create_items", "router", {"items": "nope"}), 400, "validation")
    err(kp.post("create_items", "router", {"items": [_item(tag)] * 2}), 400, "validation")  # dup key
    many = [_item(tag, f"M{i}") for i in range(501)]
    err(kp.post("create_items", "router", {"items": many}), 400, "validation")


def test_cycles_and_unreachable_work_are_refused_with_their_codes(kp):
    tag = uid()
    cyc = [_item(tag, "A", deps=[f"B-{tag}"]), _item(tag, "B", deps=[f"A-{tag}"]),
           _item(tag, "G", "goal", deps=[f"A-{tag}"])]
    err(kp.post("create_items", "router", {"items": cyc}), 400, "cycle")
    orphan = [_item(tag, "A"), _item(tag, "B", "goal", deps=[f"A-{tag}"]), _item(tag, "O")]
    body = err(kp.post("create_items", "router", {"items": orphan}), 400, "validation")
    assert "unreachable_goal" in body["msg"]


def test_unknown_fields_fold_into_note_and_tier_defaults_follow_keywords(kp):
    tag = uid()
    ok(kp.post("create_items", "router", {"items": [
        _item(tag, "A", budget="3h"), _item(tag, "S", title="rotate the signing secret " + tag),
        _item(tag, "G", "goal", deps=[f"A-{tag}", f"S-{tag}"])]}), "Created")
    snap = {i["key"]: i for i in ok(kp.get("/v1/snapshot", "reader"), "Snapshot")["items"]}
    assert "3h" in snap[f"demo:A-{tag}"]["note"]
    assert snap[f"demo:A-{tag}"]["review_tier"] == "standard"
    assert snap[f"demo:S-{tag}"]["review_tier"] == "high"        # risky keyword in the title


def test_l1_scope_on_create(kp):
    tag = uid()
    other = _item(tag, project="other")
    err(kp.post("create_items", "lead", {"items": [other]}), 403, "forbidden")
    err(kp.post("create_items", "lead", {"items": [_item(tag, deps=["other:SOMETHING"])]}), 403, "forbidden")
    err(kp.post("create_items", "worker1", {"items": [_item(tag)]}), 403, "forbidden")
    err(kp.post("create_items", "reader", {"items": [_item(tag)]}), 403, "forbidden")
    err(kp.post("create_items", "lead", {"items": [_item(tag, review_tier="none",
                                                          evidence_kinds=["decision"])]}), 403, "forbidden")
    own = [_item(tag, "A"), _item(tag, "G", "goal", deps=[f"A-{tag}"])]
    assert ok(kp.post("create_items", "lead", {"items": own}), "Created")["created"][0] == f"demo:A-{tag}"


def test_import_items_is_router_only_and_honours_legacy_state(kp):
    tag = uid()
    legacy = _item(tag, "A", state="claimed", owner="somebody")
    goal = _item(tag, "G", "goal", deps=[f"A-{tag}"])
    err(kp.post("import_items", "lead", {"items": [legacy, goal]}), 403, "forbidden")
    err(kp.post("import_items", "router", {"items": [_item(tag, "Z", state="done")]}), 400, "validation")
    ok(kp.post("import_items", "router", {"items": [legacy, goal]}), "Created")
    row = show(kp, f"demo:A-{tag}")
    assert row["state"] == "claimed" and row["epoch"] == 1 and row["claimed_by"] == "somebody"


def test_edit_definition_rules(kp):
    plan = make_plan(kp, 2, chain=True)
    first, second = plan.lanes
    ok(kp.post("edit_definition", "lead", {"key": first, "fields": {
        "title": "retitled", "description": "new text", "done_criteria": "c2"}}), "KeyOnly")
    row = show(kp, first)
    assert (row["title"], row["description"], row["done_criteria"]) == ("retitled", "new text", "c2")
    err(kp.post("edit_definition", "lead_other", {"key": first, "fields": {"title": "x"}}), 403, "forbidden")
    err(kp.post("edit_definition", "worker1", {"key": first, "fields": {"title": "x"}}), 403, "forbidden")
    err(kp.post("edit_definition", "lead", {"key": first, "fields": {"priority": 4}}), 403, "forbidden")
    ok(kp.post("edit_definition", "router", {"key": first, "fields": {"priority": 4}}), "Priority")
    assert show(kp, first)["priority"] == 4
    err(kp.post("edit_definition", "router", {"key": first, "fields": {"nope": 1}}), 400, "validation")
    err(kp.post("edit_definition", "router", {"key": first, "fields": "x"}), 400, "validation")
    err(kp.post("edit_definition", "router", {"key": first, "fields": {"depends_on": [second.split(":")[1]]}}),
        400, "cycle")
    claim(kp, first)
    err(kp.post("edit_definition", "lead", {"key": first, "fields": {"title": "late"}}), 400, "bad_state")


def test_router_may_retier_an_active_item_but_nobody_else(kp):
    plan = make_plan(kp)
    claim(kp, plan.key)
    err(kp.post("edit_definition", "lead", {"key": plan.key, "fields": {"review_tier": "high"}}),
        403, "forbidden")
    ok(kp.post("edit_definition", "router", {"key": plan.key, "fields": {"review_tier": "high"}}), "KeyOnly")
    assert show(kp, plan.key)["review_tier"] == "high"


def test_set_priority_is_router_only_and_integer_only(kp):
    plan = make_plan(kp)
    for who in ("lead", "worker1", "reader", "reviewer1"):
        err(kp.post("set_priority", who, {"key": plan.key, "n": 3}), 403, "forbidden")
    assert ok(kp.post("set_priority", "router", {"key": plan.key, "n": -4}), "Priority")["priority"] == -4
    assert show(kp, plan.key)["priority"] == -4


def test_set_kind_turns_an_open_lane_into_an_operator_item(kp):
    plan = make_plan(kp, 2)
    first, second = plan.lanes
    for who in ("lead", "worker1"):
        err(kp.post("set_kind", who, {"key": first, "kind": "question"}), 403, "forbidden")
    err(kp.post("set_kind", "router", {"key": first, "kind": "goal"}), 400, "validation")
    out = ok(kp.post("set_kind", "router", {"key": first, "kind": "question"}), "SetKind")
    assert out == {"key": first, "kind": "question", "was": "lane"}
    row = show(kp, first)
    assert row["kind"] == "question" and row["waits_on_operator"] is True
    err(kp.post("set_kind", "router", {"key": first, "kind": "operator_task"}), 400, "validation")
    claim(kp, second)
    err(kp.post("set_kind", "router", {"key": second, "kind": "question"}), 400, "bad_state")


def test_rehome_moves_an_open_item_and_leaves_an_alias(kp):
    plan = make_plan(kp, 1)
    old = plan.key
    ident = old.split(":", 1)[1]
    for who in ("lead", "worker1"):
        err(kp.post("rehome", who, {"key": old, "new_project": "moved"}), 403, "forbidden")
    err(kp.post("rehome", "router", {"key": old, "new_project": "a:b"}), 400, "validation")
    err(kp.post("rehome", "router", {"key": old, "new_project": "demo"}), 400, "validation")
    out = ok(kp.post("rehome", "router", {"key": old, "new_project": "moved", "host": "box1"}), "Rehome")
    assert out["old"] == old and out["new"] == f"moved:{ident}" and plan.goal in out["repointed"]
    aliases = ok(kp.get("/v1/aliases?fmt=json", "reader"), "Aliases")
    assert {"old": old, "new": out["new"]} == {k: a[k] for a in aliases if a["old"] == old for k in ("old", "new")}
    assert show(kp, old)["key"] == out["new"] and show(kp, out["new"])["host"] == "box1"
    assert show(kp, plan.goal)["depends_on"][0]["key"] == out["new"]
    snap = ok(kp.get("/v1/snapshot", "reader"), "Snapshot")
    assert snap["aliases"][old] == out["new"]


def test_set_legacy_state_moves_an_open_item_into_an_active_state(kp):
    plan = make_plan(kp, 2, owner="legacy-owner")
    first, second = plan.lanes
    for who in ("lead", "worker1"):
        err(kp.post("set_legacy_state", who, {"key": first, "state": "claimed"}), 403, "forbidden")
    err(kp.post("set_legacy_state", "router", {"key": first, "state": "done"}), 400, "validation")
    out = ok(kp.post("set_legacy_state", "router", {"key": first, "state": "claimed", "note": "n"}), "Legacy")
    assert out == {"key": first, "state": "claimed", "claimed_by": "legacy-owner"}
    row = show(kp, first)
    assert row["state"] == "claimed" and row["epoch"] == 1 and row["lease_until"] is None
    err(kp.post("set_legacy_state", "router", {"key": first, "state": "in_review"}), 400, "bad_state")
    assert ok(kp.post("set_legacy_state", "router", {"key": second, "state": "in_review"}), "Legacy")["state"] == "in_review"


def test_migrate_descriptions_is_router_only(kp):
    make_plan(kp)
    for who in ("lead", "worker1", "reader"):
        err(kp.post("migrate_descriptions", who, {}), 403, "forbidden")
    ok(kp.post("migrate_descriptions", "router", {}), "Migrate")
