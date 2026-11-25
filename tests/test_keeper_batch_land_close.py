"""LE-BATCH-LAND-CLOSE-PATH-REFUSED: a landed batch closes its parts and its batch item in one step."""
import json

import pytest

from keeper_util import World, defn, seed, to_in_review
from plangraph.keeper import auth, context_sources
from plangraph.keeper.evidence_decisions import DecisionsAdapter
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
from plangraph.keeper.errors import Refused
from test_keeper_deploy_gate_paths import _land_with_unit
from test_keeper_merge_close_auto import Repo, auto, env, git  # noqa: F401 - env is a fixture


def seat_record(root, run, key, head):
    path = root / "fixture" / run / "record.json"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    for parent in (root, root / "fixture"):
        parent.chmod(0o700)
    path.write_text(json.dumps({
        "run_id": run, "host": "fixture", "item_key": key, "role": "review", "status": "completed",
        "exit_code": 0, "served_model": "gemini-test", "finish_reason": "stop", "attempts": [{}],
        "head_sha": head, "completion": {"head_sha": head},
        "stdout_tail": "TEST_GAMING: no\nVERDICT: PASS\n"}), encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.parametrize("dependency_in_train", [False, True])
def test_verified_landing_records_unmet_dependency_and_can_settle(tmp_path, dependency_in_train):
    w = World(tmp_path, backend="sqlite")
    try:
        seed(w, [("PARENT", []), ("CHILD", ["PARENT"])])
        router = w.p("router")
        repo, head, merge = "example/repo", "c" * 40, "d" * 40
        lease = w.store.land(router, dict(repo=repo, action="acquire", owner="fixture"))

        def journal(action, **fields):
            return w.store.land(router, dict(lease, repo=repo, pr=100, action=action, **fields))

        parts = [dict(pr=2, head="b" * 40, items=["demo:CHILD"])]
        if dependency_in_train:
            parts.insert(0, dict(pr=1, head="a" * 40, items=["demo:PARENT"]))
        journal("prepare", base="0" * 40, head=head, mode="train-pr-merge", parts=parts)
        warning = "demo:CHILD cannot land before demo:PARENT"
        if dependency_in_train:
            journal("check_dependencies", head=head)
        else:
            with pytest.raises(Refused, match=warning) as refusal:
                journal("check_dependencies", head=head)
            assert refusal.value.code == "conflict"
        assert journal("show")["train"]["merge"] is None

        # The host already confirms the merge. Only the child actually landed.
        landed = journal("landed", merge=merge, included=[2])
        assert (landed["merge"], landed["included"], landed["dependency_warning"]) == (merge, [2], warning)
        assert journal("landed", merge=merge, included=[2]) == landed
        assert journal("show")["train"] == landed
        assert "evidence" not in journal("advance", merge=merge, step="evidence")["open_steps"]
    finally:
        w.close()


@pytest.fixture
def batch(tmp_path):
    """Parts A, B and C (standard) and the batch item BATCH (tier none), all in review, landed as one batch PR."""
    (tmp_path / "w").mkdir()
    (tmp_path / "repo").mkdir()
    w = World(tmp_path / "w", backend="sqlite")
    w.store.require_seats = True  # the refusal tests below run with the seat gate on
    token = auth.add_principal(w.conn, "spawner", "spawner", [])
    w.principals["spawner"] = auth.authenticate(w.conn, token)
    w.store.create_items(w.p("router"), [
        defn("A", review_tier="standard"), defn("B", review_tier="standard"),
        defn("C", review_tier="standard"), defn("BATCH", review_tier="none", evidence_kinds=("decision",)),
        defn("G", ["A", "B", "C", "BATCH"], kind="goal")])
    for key in "ABC":
        to_in_review(w, f"demo:{key}")
    to_in_review(w, "demo:BATCH")
    repo = Repo(tmp_path / "repo")
    root = repo.root
    parts = {}
    git(root, "checkout", "-q", "-b", "batch")
    for number, key in ((1, "A"), (2, "B"), (3, "C")):
        git(root, "checkout", "-q", "-b", f"part{key}", "main")
        git(root, "commit", "--allow-empty", "-q", "-m", f"part {key}\n\nPlan-Item: demo:{key}\n")
        parts[key] = git(root, "rev-parse", "HEAD")
        git(root, "checkout", "-q", "batch")
        git(root, "merge", "--no-ff", "-q", "-m", f"Merge PR #{number}: part {key}", f"part{key}")
    w.parts, w.repo, w.mirror = parts, repo, tmp_path / "mirror"
    w.batch_head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    w.store.ctx = {"git": GitAdapter({"demo": str(root)}), "spawner_records": SpawnerRecordAdapter(w.mirror)}
    w.tmp = tmp_path
    yield w
    w.close()


def land(w, cited=("A", "B"), body_keys=("A", "B", "C", "BATCH")):
    lines = [f"Plan-Item: demo:{key}" for key in body_keys]
    for key in cited:
        seat_record(w.mirror, f"run-part-{key.lower()}-0", f"demo:{key}", w.parts[key])
        lines.append(f"Review-Run: demo:{key} run-part-{key.lower()}-0 {w.parts[key]}")
    git(w.repo.root, "merge", "--no-ff", "-q", "-m", "Merge pull request #100 from x/batch\n\n" + "\n".join(lines),
        "batch")


def test_a_landed_batch_closes_every_part_and_the_batch_item_without_manual_evidence(batch):
    land(batch)
    result = batch.store.close_merged_auto(batch.p("system"))
    closed = {c["key"] for c in result["closed"]}
    assert closed == {"demo:A", "demo:B", "demo:BATCH"}
    assert [(r["key"], "NO-WITNESS" in r["reason"]) for r in result["listed"]] == [("demo:C", True)]
    for key in ("demo:A", "demo:B", "demo:BATCH"):
        assert batch.store._item(key)["state"] == "done"
    assert batch.store._item("demo:C")["state"] == "in_review"
    assert batch.store.verify_integrity() == []


def test_a_part_without_a_seat_run_still_refuses_with_no_witness(batch):
    land(batch, cited=("A",))
    result = batch.store.close_merged_auto(batch.p("system"))
    assert {c["key"] for c in result["closed"]} == {"demo:A", "demo:BATCH"}
    assert sorted(r["key"] for r in result["listed"]) == ["demo:B", "demo:C"]
    assert all("NO-WITNESS" in r["reason"] for r in result["listed"])


def test_off_closes_a_part_without_a_seat_run_and_flags_it(batch):
    batch.store.require_seats = False
    land(batch, cited=("A",))
    result = batch.store.close_merged_auto(batch.p("system"))
    assert {c["key"] for c in result["closed"]} == {"demo:A", "demo:B", "demo:C", "demo:BATCH"}
    assert result["listed"] == []
    flagged = {c["key"] for c in result["closed"] if "seats_missing" in c.get("flags", {})}
    assert flagged == {"demo:B", "demo:C"}


def test_off_flag_cites_run_ids_from_the_landed_review_run_trailers(batch):
    batch.store.require_seats = False
    land(batch, cited=("A",))
    (batch.mirror / "fixture/run-part-a-0/record.json").unlink()  # the cited review's record is gone
    result = batch.store.close_merged_auto(batch.p("system"))
    flags = {c["key"]: c["flags"]["seats_missing"]["run_ids"] for c in result["closed"] if "flags" in c}
    assert flags["demo:A"] == ["run-part-a-0"]
    assert flags["demo:B"] == []


def test_a_citation_for_another_part_head_is_not_a_seat(batch):
    land(batch, cited=("A",))
    # the merge cites A's run with B's head: the head is no head of A's work
    git(batch.repo.root, "commit", "--amend", "-q", "-m", "Merge pull request #100\n\nPlan-Item: demo:A\n"
        f"Review-Run: demo:A run-part-a-0 {batch.parts['B']}")
    result = batch.store.close_merged_auto(batch.p("system"))
    assert "demo:A" not in {c["key"] for c in result["closed"]}


def test_the_router_verb_closes_a_part_on_the_batch_go_row(batch):
    land(batch)
    path = batch.tmp / "DECISIONS.md"
    path.write_text(f"| D-6070 | demo | GO {batch.batch_head[:8]} |\n", encoding="utf-8")
    batch.store.ctx["decisions"] = DecisionsAdapter(path)
    assert batch.store.close_merged(batch.p("router"), "demo:A", "D-6070")["state"] == "done"
    with pytest.raises(Refused, match="NO-WITNESS"):
        batch.store.close_merged(batch.p("router"), "demo:C", "D-6070")


def test_a_merge_commit_carrying_the_trailer_binds_its_own_second_parent_after_later_merges(batch):
    land(batch)
    git(batch.repo.root, "checkout", "-q", "-b", "later", "main")
    git(batch.repo.root, "commit", "--allow-empty", "-q", "-m", "later")
    git(batch.repo.root, "checkout", "-q", "main")
    git(batch.repo.root, "merge", "--no-ff", "-q", "-m", "Merge pull request #101 from x/later", "later")
    commits = context_sources.plan_item_commits(
        GitAdapter({"demo": str(batch.repo.root)}), "demo", "demo:BATCH", fetch=False, limit=None)
    assert [c["merge"]["pr"] for c in commits] == [100]
    merge = commits[0]["merge"]["sha"]
    assert git(batch.repo.root, "rev-parse", f"{merge}^2") == batch.batch_head


def test_a_later_unreviewed_merge_of_the_same_item_keeps_it_open(batch):
    land(batch)
    git(batch.repo.root, "checkout", "-q", "-b", "follow", "main")
    git(batch.repo.root, "commit", "--allow-empty", "-q", "-m", "follow up\n\nPlan-Item: demo:A\n")
    git(batch.repo.root, "checkout", "-q", "main")
    git(batch.repo.root, "merge", "--no-ff", "-q", "-m", "Merge pull request #101 from x/follow", "follow")
    result = batch.store.close_merged_auto(batch.p("system"))
    assert "demo:A" not in {c["key"] for c in result["closed"]}
    assert any(r["key"] == "demo:A" and "insufficient_seat_families" in r["reason"] for r in result["listed"])


def test_a_cited_head_that_never_landed_is_not_a_seat(batch):
    git(batch.repo.root, "checkout", "-q", "-b", "offshoot", "main")
    git(batch.repo.root, "commit", "--allow-empty", "-q", "-m", "orphan\n\nPlan-Item: demo:A\n")
    orphan = git(batch.repo.root, "rev-parse", "HEAD")
    git(batch.repo.root, "checkout", "-q", "main")
    batch.parts["A"] = orphan          # the seat record and the citation name a branch that is no part of the merge
    land(batch, cited=("A",), body_keys=("A",))
    result = batch.store.close_merged_auto(batch.p("system"))
    assert "demo:A" not in {c["key"] for c in result["closed"]}
    assert any(r["key"] == "demo:A" and "NO-WITNESS" in r["reason"] for r in result["listed"])


def test_a_batch_that_brought_more_of_the_item_than_its_cited_parts_needs_its_own_seat(batch):
    # a second commit of A inside the batch that no citation covers
    git(batch.repo.root, "checkout", "-q", "batch")
    git(batch.repo.root, "commit", "--allow-empty", "-q", "-m", "more of A\n\nPlan-Item: demo:A\n")
    batch.batch_head = git(batch.repo.root, "rev-parse", "HEAD")
    git(batch.repo.root, "checkout", "-q", "main")
    land(batch)
    result = batch.store.close_merged_auto(batch.p("system"))
    assert "demo:A" not in {c["key"] for c in result["closed"]}
    assert any(r["key"] == "demo:A" and "NO-WITNESS" in r["reason"] for r in result["listed"])


def test_a_nested_merge_carrying_the_item_must_be_covered_by_a_cited_part(batch):
    git(batch.repo.root, "checkout", "-q", "-b", "inner", "main")
    git(batch.repo.root, "commit", "--allow-empty", "-q", "-m", "inner work")
    git(batch.repo.root, "checkout", "-q", "batch")
    git(batch.repo.root, "merge", "--no-ff", "-q", "-m", "Merge inner\n\nPlan-Item: demo:A\n", "inner")
    batch.batch_head = git(batch.repo.root, "rev-parse", "HEAD")
    git(batch.repo.root, "checkout", "-q", "main")
    land(batch)
    result = batch.store.close_merged_auto(batch.p("system"))
    assert "demo:A" not in {c["key"] for c in result["closed"]}
    assert any(r["key"] == "demo:A" and "NO-WITNESS" in r["reason"] for r in result["listed"])


# --- TRAIN-DEPLOYMENT-EVIDENCE-GO-ENFORCEMENT: the inventory also covers completed items ---


def _done(env, key, **columns):
    sets = ", ".join(["state='done'"] + [f"{name}=?" for name in columns])
    env.store.conn.execute(f"UPDATE items SET {sets} WHERE key=?", (*columns.values(), key))


def _inventory(env, **kw):
    env.store.include_done_in_scan = True
    env.store.warm_merge_scan(env.store.merge_candidates())
    return env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False, frozen=True, **kw)


def test_a_done_item_that_shipped_a_unit_without_deploy_proof_is_listed(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A", note="Deploy-Hosts: host-a")
    report = _inventory(env, inventory=True)
    rows = {row["key"]: row for row in report["deployment_pending"]}
    assert rows["demo:A"]["missing"] == ["host-a:new.service"]
    assert rows["demo:A"]["state"] == "done" and rows["demo:A"]["age_hours"] is not None
    assert report["closed"] == [] and report["would_close"] == [] and report["listed"] == []
    assert env.store._item("demo:A")["state"] == "done"


def test_a_done_item_with_a_live_clause_and_no_live_proof_is_listed(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:B", 42)
    _done(env, "demo:B", done_criteria="Tests pass. Live: the service answers on host-a")
    rows = {row["key"]: row for row in _inventory(env, inventory=True)["deployment_pending"]}
    assert rows["demo:B"]["missing"] == ["live_evidence_missing"]


def test_a_done_item_without_units_or_live_clause_is_not_listed(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:C", 43)
    _done(env, "demo:C")
    assert _inventory(env, inventory=True)["deployment_pending"] == []


def test_the_ordinary_close_never_looks_at_done_items(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A")
    report = _inventory(env)
    assert report["deployment_pending"] == [] and report["listed"] == []


def test_the_scan_leaves_done_items_out_unless_the_keeper_asks_for_them(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A")
    assert "demo:A" not in {key for key, _ in env.store.merge_candidates()}
    env.store.include_done_in_scan = True
    assert "demo:A" in {key for key, _ in env.store.merge_candidates()}


def test_the_keeper_setting_that_scans_done_items_is_a_boolean_and_off_by_default(tmp_path):
    from plangraph.keeper.config import Config, ConfigError
    base = {"db": str(tmp_path / "k.db")}
    assert Config(dict(base), tmp_path).deploy_inventory_done is False
    assert Config({**base, "deploy_inventory_done": True}, tmp_path).deploy_inventory_done is True
    with pytest.raises(ConfigError, match="deploy_inventory_done"):
        Config({**base, "deploy_inventory_done": "yes"}, tmp_path)


def test_an_inventory_request_is_refused_while_done_items_are_not_scanned(env):  # noqa: F811
    with pytest.raises(Refused, match="deploy_inventory_done"):
        env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False, inventory=True)
    with pytest.raises(Refused, match="dry run"):
        env.store.include_done_in_scan = True
        env.store.close_merged_auto(env.p("system"), require_seats=False, apply=True, inventory=True)


def _deploy_row(env, key, at):
    item = env.store._item(key)
    env.store.conn.execute(
        "INSERT INTO evidence(key,epoch,kind,ref,verified,detail,at) VALUES(?,?,?,?,?,?,?)",
        (key, item["epoch"], "deploy",
         '{"host":"host-a","is_active":"active","unit":"new.service"}', 1, "fixture", at))


def test_only_deploy_proof_recorded_after_the_merge_clears_a_done_item(env):  # noqa: F811
    _land_with_unit(env.repo.root)
    _done(env, "demo:A", note="Deploy-Hosts: host-a")
    _deploy_row(env, "demo:A", "2020-01-01T00:00:00+00:00")  # verified, but before the merge landed
    rows = {row["key"]: row for row in _inventory(env, inventory=True)["deployment_pending"]}
    assert rows["demo:A"]["missing"] == ["host-a:new.service"]
    _deploy_row(env, "demo:A", "2099-01-01T00:00:00+00:00")
    assert _inventory(env, inventory=True)["deployment_pending"] == []


def test_a_sensitive_card_that_shipped_an_undeployed_unit_still_gets_a_deployment_row(env):  # noqa: F811
    env.store.put_card(env.p("router"), "demo:A", {"outcome": "fixture", "risk_tags": ["auth"]})
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _land_with_unit(env.repo.root)
    report = auto(env, require_seats=False, apply=False)
    assert "forbidden" in report["listed"][0]["reason"]
    assert {row["key"]: row["missing"] for row in report["deployment_pending"]} == {
        "demo:A": ["host-a:new.service"]}


def test_proof_from_before_the_merge_does_not_clear_an_open_item(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _land_with_unit(env.repo.root)
    _deploy_row(env, "demo:A", "2020-01-01T00:00:00+00:00")
    report = auto(env, require_seats=False, apply=False)
    assert report["deployment_pending"][0]["missing"] == ["host-a:new.service"]
    _deploy_row(env, "demo:A", "2099-01-01T00:00:00+00:00")
    assert auto(env, require_seats=False, apply=False)["deployment_pending"] == []


def test_proof_older_than_the_merge_a_merge_repo_line_names_does_not_clear_its_unit(env, monkeypatch):  # noqa: F811
    """A tagged docs merge, then a named merge that ships the unit; proof between the two is stale."""
    env.repo.land("docs", "Plan-Item: demo:A", 45)
    root = env.repo.root
    git(root, "checkout", "-q", "-b", "unit", "main")
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/systemd/new.service").write_text("[Service]\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "unit without a trailer")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", "Merge pull request #46 from x/unit", "unit")
    named = git(root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=?, note='Deploy-Hosts: host-a',"
                     " done_criteria='Tests pass. Live: the unit runs' WHERE key='demo:A'",
                     (f"Merge-Repo: demo {named}",))
    monkeypatch.setattr(env.store.ctx["git"], "commit_time", lambda repo, sha: 2_524_608_000.0)  # 2050
    _deploy_row(env, "demo:A", "2040-01-01T00:00:00+00:00")  # after the tagged merge, before the named one
    assert auto(env, require_seats=False, apply=False)["deployment_pending"][0]["missing"] == [
        "host-a:new.service", "live_evidence_missing"]  # the unit and the Live: clause share the cutoff
    _deploy_row(env, "demo:A", "2099-01-01T00:00:00+00:00")
    assert auto(env, require_seats=False, apply=False)["deployment_pending"] == []


def test_the_public_evidence_verb_takes_deploy_proof_for_a_normally_closed_item(env):  # noqa: F811
    from plangraph.keeper.evidence import VERIFIERS
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    env.repo.land("docs", "Plan-Item: demo:A", 40)
    assert "demo:A" in [c["key"] for c in auto(env, require_seats=False)["closed"]]
    assert env.store._item("demo:A")["state"] == "done"  # closed by review_passed: locked for every other write
    _land_with_unit(env.repo.root, name="late", pr=60)  # a unit that ships after the close
    assert _inventory(env, inventory=True)["deployment_pending"][0]["missing"] == ["host-a:new.service"]
    env.store.verifiers["deploy"] = VERIFIERS["deploy"]
    env.clock.t = 4_000_000_000.0
    out = env.store.add_evidence_router(env.p("router"), "demo:A", "deploy", {
        "host": "host-a", "unit": "new.service", "is_active": "active"})
    assert out["verified"] is True and env.store._item("demo:A")["state"] == "done"
    assert _inventory(env, inventory=True)["deployment_pending"] == []
    from plangraph.keeper import events
    assert events.verify_chain(env.conn) == [] and events.verify_integrity(env.conn) == []
    with pytest.raises(Refused):  # no other proof lands on a completed item
        env.store.add_evidence_router(env.p("router"), "demo:A", "decision", {"id": "D-1"})


def test_a_completed_item_whose_only_delivery_is_a_named_merge_is_scanned(env):  # noqa: F811
    _land_with_unit(env.repo.root, tagged=False)
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=?, note='Deploy-Hosts: host-a' WHERE key='demo:A'",
                     (f"Merge-Repo: demo {merge}",))
    _done(env, "demo:A")
    report = _inventory(env, inventory=True)
    assert report["listed"] == []
    assert report["deployment_pending"][0]["missing"] == ["host-a:new.service"]


def test_a_watch_request_cannot_ask_for_the_inventory(env):  # noqa: F811
    from plangraph.keeper import api_routes
    with pytest.raises(Refused, match="watch cannot combine"):
        api_routes._close_merged_auto(env.store, env.p("system"),
                                      {"watch": True, "apply": False, "inventory": True}, None)


def test_an_open_item_delivered_only_by_a_merge_repo_line_still_gets_its_row(env):  # noqa: F811
    _land_with_unit(env.repo.root, tagged=False)
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=?, note='Deploy-Hosts: host-a' WHERE key='demo:A'",
                     (f"Merge-Repo: demo {merge}",))
    report = auto(env, require_seats=False, apply=False)
    assert [row["key"] for row in report["deployment_pending"]] == ["demo:A"]
    assert report["deployment_pending"][0]["missing"] == ["host-a:new.service"]
    assert "merged_on_default_missing" in report["listed"][0]["reason"]
    assert env.store._item("demo:A")["state"] == "open"


def test_a_missing_review_seat_keeps_the_live_evidence_row(env):  # noqa: F811
    env.repo.land("docs", "Plan-Item: demo:A", 41)
    env.conn.execute("UPDATE items SET done_criteria='Tests pass. Live: the service answers' WHERE key='demo:A'")
    env.store.warm_merge_scan(env.store.merge_candidates())
    report = env.store.close_merged_auto(env.p("system"), apply=False, frozen=True)  # seats required, none cited
    assert "NO-WITNESS" in report["listed"][0]["reason"].upper().replace("_", "-")
    assert report["deployment_pending"][0]["missing"] == ["live_evidence_missing"]


def test_the_age_of_a_row_counts_from_the_named_merge_not_only_the_tagged_one(env, monkeypatch):  # noqa: F811
    import time
    _land_with_unit(env.repo.root, tagged=True)  # tagged merge: just now
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=?, note='Deploy-Hosts: host-a' WHERE key='demo:A'",
                     (f"Merge-Repo: demo {merge}",))
    now = time.time()
    env.clock.t = now
    monkeypatch.setattr(env.store.ctx["git"], "commit_time", lambda repo, sha: now - 48 * 3600)
    row = auto(env, require_seats=False, apply=False)["deployment_pending"][0]
    assert row["age_hours"] >= 47


def test_a_landed_unit_keeps_its_row_while_another_lane_of_the_item_is_open(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _land_with_unit(env.repo.root)
    env.repo.land("a-more", "Plan-Item: demo:A", 0, merge=False)  # an unmerged lane of the same item
    env.store.warm_merge_scan(env.store.merge_candidates())
    report = env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False, frozen=True)
    assert "unmerged_work_or_lanes_unknown" in report["listed"][0]["reason"]
    assert report["deployment_pending"][0]["missing"] == ["host-a:new.service"]
    assert env.store._item("demo:A")["state"] == "open"


def test_an_unreadable_named_merge_time_makes_the_age_unknown(env, monkeypatch):  # noqa: F811
    _land_with_unit(env.repo.root, tagged=True)  # a recent tagged merge
    merge = git(env.repo.root, "rev-parse", "HEAD")
    env.conn.execute("UPDATE items SET description=?, note='Deploy-Hosts: host-a' WHERE key='demo:A'",
                     (f"Merge-Repo: demo {merge}",))
    monkeypatch.setattr(env.store.ctx["git"], "commit_time", lambda repo, sha: None)
    row = env.store.close_merged_auto(env.p("system"), require_seats=False, apply=False)["deployment_pending"][0]
    assert row["age_hours"] is None and row["merged_at"] is None


def _unit_branch(root, name, pr, trailers, unit="new.service", status="add"):
    """Merge a PR that adds (or removes) one unit; ``trailers`` go in the commit message."""
    git(root, "checkout", "-q", "-b", name, "main")
    path = root / "deploy/systemd" / unit
    if status == "add":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Service]\n")
        git(root, "add", "-A")
    else:
        git(root, "rm", "-q", f"deploy/systemd/{unit}")
    git(root, "commit", "-q", "-m", f"unit work\n\n{trailers}")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "--no-ff", "-q", "-m", f"Merge pull request #{pr} from x/{name}", name)


def test_a_unit_landed_under_plan_item_part_is_in_the_inventory_of_an_open_item(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _unit_branch(env.repo.root, "part", 61, "Plan-Item-Part: demo:A")
    report = auto(env, require_seats=False, apply=False)
    assert report["would_close"] == [] and report["closed"] == []
    assert [p["key"] for p in report["parts"]] == ["demo:A"]
    assert report["deployment_pending"][0]["key"] == "demo:A"
    assert report["deployment_pending"][0]["missing"] == ["host-a:new.service"]
    assert env.store._item("demo:A")["state"] == "open"


def test_a_part_only_unit_of_a_completed_item_is_listed_too(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _unit_branch(env.repo.root, "part", 61, "Plan-Item-Part: demo:A")
    _done(env, "demo:A")
    assert _inventory(env, inventory=True)["deployment_pending"][0]["missing"] == ["host-a:new.service"]


def test_a_unit_is_aged_from_the_landing_that_made_it_deployable(env, monkeypatch):  # noqa: F811
    import time
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    env.repo.land("docs", "Plan-Item: demo:A", 62)  # the item's older docs merge
    root = env.repo.root
    docs = git(root, "rev-parse", "HEAD")
    _unit_branch(root, "unit", 63, "Plan-Item: demo:A")  # a unit that has just landed
    unit_commit = git(root, "rev-parse", "HEAD^2")
    now = time.time()
    env.clock.t = now
    times = {docs: now - 72 * 3600}
    real = env.store.ctx["git"].commit_time
    monkeypatch.setattr(env.store.ctx["git"], "commit_time", lambda repo, sha: times.get(sha, real(repo, sha)))
    monkeypatch.setattr(
        env.store, "_merged_commit_proof",
        lambda git_, project, commit: (None, commit["sha"],
                                       __import__("datetime").datetime.fromtimestamp(
                                           now - (72 * 3600 if commit["sha"] != unit_commit else 0),
                                           __import__("datetime").timezone.utc).isoformat()))
    row = auto(env, require_seats=False, apply=False)["deployment_pending"][0]
    assert row["missing"] == ["host-a:new.service"] and row["age_hours"] <= 1


def test_a_completed_docs_only_direct_commit_has_no_row_and_no_unknown_age(env):  # noqa: F811
    git(env.repo.root, "commit", "--allow-empty", "-q", "-m", "docs only\n\nPlan-Item: demo:A")  # no PR
    _done(env, "demo:A")
    report = _inventory(env, inventory=True)
    assert report["deployment_pending"] == [] and report["listed"] == []


def test_a_direct_commit_with_a_live_clause_is_aged_from_the_commit(env):  # noqa: F811
    git(env.repo.root, "commit", "--allow-empty", "-q", "-m", "docs only\n\nPlan-Item: demo:A")
    env.conn.execute("UPDATE items SET done_criteria='Tests pass. Live: it runs' WHERE key='demo:A'")
    _done(env, "demo:A")
    row = _inventory(env, inventory=True)["deployment_pending"][0]
    assert row["missing"] == ["live_evidence_missing"] and row["age_hours"] is not None


def test_a_merge_that_only_retires_a_unit_leaves_nothing_to_deploy(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    root = env.repo.root
    _unit_branch(root, "ship", 64, "Plan-Item: demo:C")  # an older delivery adds the unit
    _unit_branch(root, "retire", 65, "Plan-Item: demo:A", status="remove")
    assert auto(env, require_seats=False, apply=False)["deployment_pending"] == []
    assert "demo:A" in [c["key"] for c in auto(env, require_seats=False)["closed"]]


def test_a_unit_added_then_removed_in_one_item_is_retired_at_the_head(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    root = env.repo.root
    _unit_branch(root, "ship", 66, "Plan-Item: demo:A")
    _unit_branch(root, "retire", 67, "Plan-Item: demo:A", status="remove")
    assert auto(env, require_seats=False, apply=False)["deployment_pending"] == []


def test_every_expected_target_is_reported_even_when_the_keeper_holds_a_receipt(env):  # noqa: F811
    env.conn.execute("UPDATE items SET note='Deploy-Hosts: host-a' WHERE key='demo:A'")
    _land_with_unit(env.repo.root)
    _deploy_row(env, "demo:A", "2099-01-01T00:00:00+00:00")
    report = auto(env, require_seats=False, apply=False)
    assert report["deployment_pending"] == []
    row = report["deployment_expected"][0]
    assert row["key"] == "demo:A" and row["expected"] == ["host-a:new.service"]
    assert row["landed"] and row["missing"] == []


def test_the_projects_reply_says_whether_completed_items_are_scanned(env):  # noqa: F811
    import types
    from plangraph.keeper import api_routes
    server = types.SimpleNamespace(store=env.store)
    refresh = api_routes.MergeRefresh(server)
    assert refresh.control("projects")["inventory_done"] is False
    env.store.include_done_in_scan = True
    assert refresh.control("projects")["inventory_done"] is True
