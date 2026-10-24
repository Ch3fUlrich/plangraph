"""B2: L5 Closes: fail-closed, the ``priority`` batch op and the seat/earlier lows."""
import pytest
import yaml

from keeper_util import World, defn, seed
from plangraph.keeper.errors import Refused
from test_keeper_apply_ops import create, link_goal, snap
from test_keeper_close import DID, KEY, _world


GOAL = "demo:GOAL"


def _row(cell):
    return f"| {DID} | note | {cell} |\n"


# ------------------------------------------------------ L5: Closes: fail-closed
@pytest.mark.parametrize("cell", ["Closes: demo: A", "Closes:", "Closes: ,",
                                  "Closes: ,  ,", "Closes: demo:"])
def test_closes_keyword_without_valid_token_fails_closed(tmp_path, cell):
    w = _world(tmp_path, _row(cell))
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == "evidence_unverified"
    assert w.store._item(KEY)["state"] != "done"
    w.close()


def test_valid_closes_token_closes(tmp_path):
    w = _world(tmp_path, _row("Closes: demo:A"))
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_no_closes_keyword_batch_ref_authorises(tmp_path):
    w = _world(tmp_path, f"| {DID} | recorded, no token |\n")
    assert w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)["state"] == "done"
    w.close()


def test_fail_closed_also_inside_apply_ops(tmp_path):
    w = _world(tmp_path, _row("Closes:"))
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            create("X"), link_goal("A", "X"),
            {"op": "close_on_decision", "key": KEY}], batch_ref=w.batch_ref)
    assert exc.value.code == "evidence_unverified" and snap(w) == before
    w.close()


# ------------------------------------------------- B1c: the priority batch op
@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def prio(key, n):
    return {"op": "priority", "key": key, "priority": n}


def test_priority_op_works_inside_a_batch(w):
    out = w.store.apply_ops(w.p("router"), [create("X"), link_goal("A", "X"),
                                            prio("demo:X", 7), prio("demo:A", 2)])
    assert out["applied"] == 4 and out["results"][2] == {"key": "demo:X",
                                                          "priority": 7}
    assert w.store._item("demo:X")["priority"] == 7
    assert w.store._item("demo:A")["priority"] == 2
    assert w.store.verify_integrity() == []


def test_priority_op_rolls_back_with_the_batch(w):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            prio("demo:A", 9), {"op": "cancel", "key": "demo:NOPE"}])
    assert exc.value.msg.startswith("op 2 cancel demo:NOPE: not_found ")
    assert snap(w) == before and w.store._item("demo:A")["priority"] == 0


def test_priority_op_non_router_forbidden(w):
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("l1main"), [prio("demo:A", 1)])
    assert exc.value.code == "forbidden" and snap(w) == before


@pytest.mark.parametrize("bad", ["x", True, 1.5, None])
def test_priority_op_shape_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [prio("demo:A", bad)])
    assert exc.value.code == "validation"
    assert exc.value.msg.startswith("op 1 priority demo:A: validation ")


# ------------------------------------- apply_batch: priority op, usage, replies
import io  # noqa: E402

from test_apply_batch import (  # noqa: E402,F401
    apply_batch, ops_for, run, srv, state_of, token_file, world,
    write_batch)


def _prio_ops():
    ops = ops_for()[:2]
    return ops + [{"op": "priority", "key": "demo:B", "priority": 5}]


def _priority_of(world, key):
    return world.conn.execute("SELECT priority FROM items WHERE key=?",
                              (key,)).fetchone()[0]


@pytest.mark.parametrize("extra", [(), ("--per-op",)])
def test_batch_priority_op_applies(world, srv, token_file, tmp_path, extra):
    rc, out, err = run(write_batch(tmp_path, ops=_prio_ops()), srv.base_url,
                       token_file, extra=extra)
    assert rc == 0, out + err
    assert "ok 3 priority demo:B" in out and "applied 3 of 3" in out
    assert _priority_of(world, "demo:B") == 5


@pytest.mark.parametrize("bad", ["x", True, None])
def test_dry_run_validates_priority_op(world, srv, token_file, tmp_path, bad):
    ops = _prio_ops()
    ops[2]["priority"] = bad
    rc, out, err = run(write_batch(tmp_path, ops=ops), srv.base_url,
                       token_file, extra=["--dry-run"])
    assert rc == 1 and "op 3 priority demo:B: error" in out
    rc, out, err = run(write_batch(tmp_path, ops=_prio_ops()), srv.base_url,
                       token_file, extra=["--dry-run"])
    assert rc == 0 and "plan 3 priority demo:B" in out


def test_priority_op_rolls_back_in_default_mode(world, srv, token_file,
                                                tmp_path):
    ops = _prio_ops() + [{"op": "cancel", "key": "demo:NOPE"}]
    rc, out, err = run(write_batch(tmp_path, ops=ops), srv.base_url, token_file)
    assert rc == 1 and "op 4 cancel demo:NOPE: not_found" in out
    assert world.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize("extra", [(), ("--per-op",)])
def test_unparseable_2xx_reply_exits_non_zero(world, srv, token_file, tmp_path,
                                              monkeypatch, extra):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b"<html>ok</html>")
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file,
                       extra=extra)
    assert rc != 0 and "applied 6 of 6" not in out


def test_short_reply_exits_non_zero(world, srv, token_file, tmp_path,
                                    monkeypatch):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b'{"applied": 2}')
    rc, out, err = run(write_batch(tmp_path), srv.base_url, token_file)
    assert rc != 0 and "applied 6 of 6" not in out


def test_usage_lists_allow_no_whoami():
    out = io.StringIO()
    apply_batch.print_help(out)
    assert "--allow-no-whoami" in out.getvalue()


# ------------------------------------------- pg ready --brief unblocks column
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402

from plangraph import views  # noqa: E402
from plangraph.keeper import cli  # noqa: E402


def _brief(monkeypatch, srv, token_file, *argv):
    monkeypatch.setenv("PG_URL", srv.base_url)
    monkeypatch.setenv("PG_TOKEN_FILE", token_file)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["ready", *argv])
    return code, out.getvalue()


def test_ready_brief_unblocks_matches_full_graph(world, srv, token_file,
                                                 monkeypatch):
    world.store.create_items(world.p("router"), [
        defn("B", []), defn("GOAL", ["B"], kind="goal")])
    # only B is ready, GOAL (its dependent) is not: the count is 1, not 0
    full = world.store._all_items()
    assert views.unblocks(full)["demo:B"] == 1
    code, text = _brief(monkeypatch, srv, token_file, "--brief")
    assert code == 0
    assert text.strip().startswith("demo:B |") and "unblocks 1" in text


def test_ready_json_carries_full_graph_unblocks(world, srv, token_file):
    import json
    import urllib.request
    world.store.create_items(world.p("router"), [
        defn("B", []), defn("GOAL", ["B"], kind="goal")])
    request = urllib.request.Request(
        f"{srv.base_url}/v1/ready?fmt=json",
        headers={"Authorization": f"Bearer {world.tokens['router']}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as resp:
        body = json.loads(resp.read())
    assert [i["key"] for i in body["items"]] == ["demo:B"]
    assert body["unblocks"]["demo:B"] == 1


# ------------------------------------------------------- seat LOWs and docs
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from plangraph.keeper import importer  # noqa: E402
from plangraph.keeper.api_routes import ROUTER_VERBS, WRITE_VERBS  # noqa: E402
from plangraph.keeper.store_create import KNOWN_ITEM_FIELDS  # noqa: E402

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_op_validate_form_when_no_op_owns_the_problem(w):
    # dropping GOAL's only dependency strands A, which no op of the batch touched
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [link_goal()])
    assert exc.value.msg.startswith("op - validate: ")
    assert snap(w) == before
    assert "`op - validate" in (DOCS / "BATCH.md").read_text(encoding="utf-8")


def test_allow_new_lets_a_cancel_dangle_its_dependents(w):
    out = w.store.apply_ops(w.p("router"), [{"op": "cancel", "key": "demo:A"}])
    assert out["applied"] == 1 and w.store._item("demo:A")["state"] == "cancelled"
    assert w.store._item(GOAL)["state"] == "open"   # depends_on_cancelled allowed


def test_allow_new_does_not_cover_items_other_ops_create(w):
    # a create depending on an item the same batch cancels is refused, exactly
    # as the single create_items verb refuses it
    before = snap(w)
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            {"op": "cancel", "key": "demo:A"}, create("X", deps=["A"]),
            link_goal("X")])
    assert exc.value.code == "validation" and "depends_on_cancelled" in exc.value.msg
    assert snap(w) == before


# mint_run_token is router-or-l1 (own project), covered in test_keeper_run_tokens*
@pytest.mark.parametrize("verb", sorted(set(ROUTER_VERBS) - {"mint_run_token"}))
def test_router_verbs_are_routed_writes(world, srv, verb):
    assert verb in WRITE_VERBS
    import json
    import urllib.error
    import urllib.request
    body = {"key": "demo:A", "batch_ref": "x", "state": "claimed", "ops": []}
    request = urllib.request.Request(
        f"{srv.base_url}/v1/{verb}", data=json.dumps(body).encode(),
        method="POST", headers={"Authorization": f"Bearer {world.tokens['l1main']}",
                                "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 403   # routed to the verb, which refuses non-routers


def test_known_item_fields_cover_every_items_column(w):
    lifecycle = {"epoch", "round", "claimed_by", "delegate", "lease_until",
                 "reviewer", "model", "review_waiver", "created_at",
                 "updated_at", "writer_model", "seat_models", "run_ids",
                 "exec_workflow_id", "exec_run_id", "exec_attempt",
                 "exec_principal", "exec_retired_runs"}
    columns = {row[1] for row in w.conn.execute("PRAGMA table_info(items)")}
    assert columns - lifecycle <= KNOWN_ITEM_FIELDS, (
        "a new items column must be a known create field or a lifecycle column")
    assert {"depends_on", "owner_family"} <= KNOWN_ITEM_FIELDS


def test_importer_refuses_an_oversize_fold_like_the_api(tmp_path):
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({"items": [{
        "project": "demo", "id": "Z", "title": "t", "track": "T0",
        "owner": "o", "description": "d" * 2100}]}), encoding="utf-8")
    with pytest.raises(Refused) as exc:
        importer.load_dir(tmp_path)
    assert exc.value.code == "validation" and "description" in exc.value.msg


def test_runbook_backup_is_a_working_python_one_liner(w, tmp_path):
    text = (DOCS / "RUNBOOK.md").read_text(encoding="utf-8")
    assert 'sqlite3 "$KEEPER' not in text
    line = next(ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("python3 -c 'import sqlite3"))
    keeper = tmp_path / "k"
    keeper.mkdir()
    import shutil
    shutil.copy(w.path, keeper / "keeper.db")
    cmd = line.replace("python3", sys.executable, 1)
    done = subprocess.run(["bash", "-c", cmd], env={"KEEPER": str(keeper),
                                                    "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (keeper / "backup.db").stat().st_size > 0


# --------------------------------- mis-cased Closes:/Waives: keywords (rework)
from types import SimpleNamespace  # noqa: E402

from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402


def _adapter(tmp_path, cell):
    path = tmp_path / "DEC.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                    f"| D-1 | note | {cell} |\n", encoding="utf-8")
    return DecisionsAdapter(path)


@pytest.mark.parametrize("kw", ["closes:", "CLOSES:", "Closes:", "cLoSeS :"])
def test_closes_keyword_is_case_insensitive_key_tokens_exact(tmp_path, kw):
    ad = _adapter(tmp_path, f"{kw} demo:A")
    assert ad.has_closes_keyword("D-1") and ad.closes("D-1") == {"demo:A"}
    assert "demo:a" not in {t for t in ad.closes("D-1")}


@pytest.mark.parametrize("kw", ["waives:", "WAIVES:", "Waives:"])
def test_waives_keyword_is_case_insensitive_key_tokens_exact(tmp_path, kw):
    ad = _adapter(tmp_path, f"{kw} demo:A")
    assert ad.waives("D-1", "demo:A") and not ad.waives("D-1", "demo:a")
    assert not ad.waives("D-1", "demo:B")


@pytest.mark.parametrize("cell", ["closes: demo: A", "CLOSES:", "closes: ,",
                                  "cLoSeS: demo:"])
def test_miscased_closes_with_bad_token_is_an_L5_refusal(tmp_path, cell):
    w2 = _world(tmp_path, _row(cell))
    with pytest.raises(Refused) as exc:
        w2.store.close_on_decision(w2.p("router"), KEY, w2.batch_ref)
    assert exc.value.code == "evidence_unverified"
    w2.close()


def test_miscased_closes_with_valid_token_closes_only_that_key(tmp_path):
    w2 = _world(tmp_path, _row("closes: demo:A"))
    assert w2.store.close_on_decision(w2.p("router"), KEY,
                                      w2.batch_ref)["state"] == "done"
    w2.close()
    w3 = _world(tmp_path / "o", _row("CLOSES: demo:Z"))
    with pytest.raises(Refused) as exc:
        w3.store.close_on_decision(w3.p("router"), KEY, w3.batch_ref)
    assert exc.value.code == "evidence_unverified"
    w3.close()
