"""CLI end to end against a live in-process API, plus the unreachable fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import api, cli, cli_admin, config


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])], review_tier="high")
    yield world
    world.close()


def make_cfg(tmp_path, world):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
        # Tests call ``w.store`` directly without ``server.lock``; a debounced background
        # snapshot build would share that SQLite connection, so rebuild synchronously.
        "snapshot_debounce_s": 0,
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, config.load_config(path)


@pytest.fixture
def live(w, tmp_path, monkeypatch):
    cfg_path, cfg = make_cfg(tmp_path, w)
    server = api.make_server(w.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_files = {}
    for name in ("l1main", "reviewer", "reviewer2", "workerB", "router", "reader"):
        tf = tmp_path / f"{name}.token"
        tf.write_text(w.tok(name), encoding="utf-8")
        os.chmod(tf, 0o600)
        token_files[name] = tf
    monkeypatch.setenv("PG_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("PG_CONFIG", str(cfg_path))
    yield server, w, token_files, cfg, cfg_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def run(token_file, *argv):
    os.environ["PG_TOKEN_FILE"] = str(token_file)
    return cli.main(list(argv))


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_define_writer_slices_requires_the_sliced_item_key(live, tmp_path, capsys):
    _, w, tokens, _, _ = live
    router = w.p("router")
    w.store.create_items(router, [defn("ROUTING-GOAL", project="plangraph", kind="goal")])
    w.store.create_items(router, [
        defn("LESSONS-ENFORCE", project="plangraph", kind="gate", under="ROUTING-GOAL"),
        defn("ORIGINAL", project="plangraph", under="ROUTING-GOAL"),
        defn("LEGACY", project="plangraph", under="ROUTING-GOAL",
             done_criteria="Slice of plangraph:ORIGINAL: legacy criteria"),
    ])
    path = tmp_path / "slices.json"
    item = defn("FOLLOWUP", project="plangraph", under="LEGACY",
                description="Why: split remaining work\nHow: cover its retry path.")
    argv = ("define", "--file", str(path))
    path.write_text(json.dumps([item]), encoding="utf-8")
    assert run(tokens["router"], *argv) == 2
    assert "plangraph:FOLLOWUP" in (error := capsys.readouterr().err)
    assert "slice_parent" in error
    assert w.store._item("plangraph:FOLLOWUP") is None
    item.update(under="plangraph:LESSONS-ENFORCE", slice_parent="plangraph:LEGACY")
    path.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    assert run(tokens["router"], *argv) == 2
    assert "parent plangraph:LEGACY is already a slice" in capsys.readouterr().err
    item["slice_parent"] = "plangraph:ORIGINAL"
    path.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    assert run(tokens["router"], *argv) == 0
    assert w.store._item("plangraph:LESSONS-ENFORCE")["depends_on"] == ["plangraph:FOLLOWUP"]


def test_cli_full_lifecycle(live, capsys):
    _, w, tokens, _, _ = live
    assert run(tokens["l1main"], "claim", "demo:A") == 0
    assert run(tokens["l1main"], "delegate", "demo:A", "workerB") == 0
    assert run(tokens["l1main"], "evidence", "demo:A", "--kind", "decision",
               "--decision", "D-1") == 0
    assert run(tokens["l1main"], "submit", "demo:A") == 0
    assert run(tokens["reviewer"], "verdict", "demo:A", "--pass") == 0
    assert run(tokens["reviewer2"], "verdict", "demo:A", "--pass") == 0
    capsys.readouterr()  # discard the text output of the earlier steps
    run(tokens["reader"], "show", "demo:A", "--json")
    assert out_json(capsys)["state"] == "done"


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_ready_needs_reconcile_only_with_reasons(live, capsys, monkeypatch, json_mode):
    _, world, tokens, _, _ = live
    clean, dropped, pending = [dict(defn(k), key=f"demo:{k}", context={})
                                for k in ("CLEAN", "DROPPED", "PENDING")]
    dropped.update(ready_status="needs_reconcile", reason="already merged via PR #71")
    pending.update(ready_status="in_progress", prs=[72])
    monkeypatch.setattr(world.store, "_ready_groups", lambda **kw: {
        "items": [clean], "needs_reconcile": [dropped], "in_progress": [pending],
        "unknown": [], "warnings": []})
    flags = ["--json"] if json_mode else []
    assert run(tokens["reader"], "ready", "--needs-reconcile", *flags) == 0
    output = capsys.readouterr().out
    assert "demo:DROPPED" in output and dropped["reason"] in output
    assert "demo:CLEAN" not in output and "demo:PENDING" not in output
    if json_mode:
        assert json.loads(output) == {"needs_reconcile": [dropped]}


def test_cli_refusal_exits_2(live, capsys):
    _, w, tokens, _, _ = live
    code = run(tokens["reader"], "claim", "demo:A")
    err = capsys.readouterr().err
    assert code == 2
    assert "forbidden" in err


def test_cli_unreachable_read_falls_back(live, capsys):
    server, w, tokens, cfg, _ = live
    snap = w.store.snapshot(w.p("router"))
    cfg.snapshot.write_text(json.dumps(snap), encoding="utf-8")
    server.shutdown()
    server.server_close()
    code = run(tokens["reader"], "ready")
    captured = capsys.readouterr()
    assert code == 0
    assert "READ-ONLY fallback (keeper unreachable)" in captured.err
    assert "demo:A" in captured.out


def test_cli_unreachable_write_exits_3(live, capsys):
    server, w, tokens, cfg, _ = live
    server.shutdown()
    server.server_close()
    assert run(tokens["l1main"], "claim", "demo:A") == 3


def test_cli_never_prints_the_token(live, capsys):
    _, w, tokens, _, _ = live
    token = w.tok("l1main")
    assert run(tokens["l1main"], "whoami", "--json") == 0
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err


def test_cli_whoami_without_fcntl(live):
    _, _, tokens, _, _ = live
    code = '''import builtins, runpy, sys
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
sys.argv = ['plangraph.keeper.cli', 'whoami', '--json']
try:
    runpy.run_module('plangraph.keeper.cli', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
assert 'plangraph.keeper.db' not in sys.modules
'''
    env = dict(os.environ, PG_TOKEN_FILE=str(tokens['reader']),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-c', code], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['name'] == 'reader'


def test_cli_renders_snapshot_to_html(live, tmp_path, capsys):
    _, w, tokens, cfg, _ = live
    snap = w.store.snapshot(w.p("router"))
    cfg.snapshot.write_text(json.dumps(snap), encoding="utf-8")
    out = tmp_path / "plan.html"
    assert run(tokens["reader"], "render", "--out", str(out), "--cdn") == 0
    assert out.exists() and "cytoscape" in out.read_text().lower()


class _FakeImporter:
    def __init__(self, items):
        self.items = items
        self.exported = []

    def load_dir(self, path):
        return [dict(item) for item in self.items]

    def export_yaml(self, conn, directory):
        self.exported.append((conn, directory))
        return directory


def test_cli_admin_import_and_export(w, tmp_path, monkeypatch, capsys):
    cfg_path, cfg = make_cfg(tmp_path, w)
    monkeypatch.setenv("PG_CONFIG", str(cfg_path))
    fake = _FakeImporter([
        defn("X", project="demo"),
        defn("GOALX", ["X"], project="demo", kind="goal"),
    ])
    monkeypatch.setattr(cli_admin, "_importer", lambda: fake)
    code = cli.main(["admin", "import", str(tmp_path), "--as", "router"])
    assert code == 0
    row = w.conn.execute("SELECT state FROM items WHERE key='demo:X'").fetchone()
    assert row is not None and row["state"] == "open"
    code = cli.main(["admin", "export", str(tmp_path / "out")])
    assert code == 0
    assert fake.exported and fake.exported[0][1] == str(tmp_path / "out")


def test_cli_admin_init_add_principal_verify(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "fresh.db"
    cfg_path = tmp_path / "keeper.json"
    cfg_path.write_text(json.dumps({
        "db": str(db_path), "snapshot": str(tmp_path / "plan.snapshot.json"),
    }), encoding="utf-8")
    os.chmod(cfg_path, 0o600)
    monkeypatch.setenv("PG_CONFIG", str(cfg_path))
    assert cli.main(["admin", "init", "--router", "router"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1].rsplit(" ", 1)[-1]
    assert token and token not in db_path.read_bytes().decode("latin-1", "ignore")
    assert cli.main(["admin", "add-principal", "l1x", "--role", "l1",
                     "--projects", "demo", "--family", "teamA"]) == 0
    assert cli.main(["admin", "verify"]) == 0
    assert cli.main(["admin", "revoke", "l1x"]) == 0



@pytest.mark.parametrize("record_kind", ["checkout", "run_worktree"])
def test_sweep_claims_reads_real_keeper_run_metadata(live, tmp_path, monkeypatch, capsys, record_kind):
    from plangraph.keeper import auth
    from plangraph.keeper.evidence_git import GitAdapter
    server, w, tokens, cfg, _ = live
    repo = tmp_path / "git-repo"
    repo.mkdir()
    def git(*args, cwd=repo):
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", *args],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-qb", "main")
    git("commit", "--allow-empty", "-qm", "initial")
    writer = tmp_path / "writer"
    git("worktree", "add", "-qb", "writer-topic", str(writer))
    git("commit", "--allow-empty", "-qm", "work", cwd=writer)
    git("merge", "--ff-only", "writer-topic")
    w.store.ctx = {"git": GitAdapter({p: {"path": str(repo), "ref": "main"} for p in ("demo", "other")})}
    assert run(tokens["l1main"], "claim", "demo:A") == 0
    token = auth.add_principal(w.conn, "sweep-spawner", "spawner", [])
    spawner = auth.authenticate(w.conn, token)
    w.store.record_run(spawner, "demo:A", {"run_id": "writer-run", "phase": "started"})
    state = tmp_path / "state"
    record = state / "runs/writer-run/record.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"status": "completed", record_kind: str(writer) if record_kind == "checkout" else {"path": str(writer)}}))
    assert run(tokens["l1main"], "evidence", "demo:A", "--kind", "decision", "--decision", "D-1") == 0
    assert run(tokens["l1main"], "submit", "demo:A") == 0
    capsys.readouterr()
    # No item-name branch and an already merged commit: only the real run binding protects this path.
    assert run(tokens["router"], "sweep-claims", "--state", str(state)) == 0
    assert out_json(capsys) == {"paths": [str(writer)], "branches": []}
    monkeypatch.setenv("PG_SWEEP_STATE", str(state))
    assert run(tokens["router"], "sweep-claims") == 0
    assert out_json(capsys) == {"paths": [str(writer)], "branches": []}
    assert "claim_metadata" not in w.store.show(w.p("reader"), "demo:A")
    assert w.store.show(w.p("reader"), "demo:A")["run_ids"] == ["writer-run"]
    seed(w, [("CLAIMTWO", [])], project="other")
    w.store.claim(w.p("l1other"), "other:CLAIMTWO")
    second = tmp_path / "second"
    git("worktree", "add", "-qb", "lane/claimtwo", str(second))
    git("commit", "--allow-empty", "-qm", "unmerged", cwd=second)
    # show must refresh open lanes even when a previous read populated its ten-second cache.
    assert run(tokens["router"], "sweep-claims", "--state", str(state)) == 0
    answer = out_json(capsys)
    assert answer["paths"] == [str(writer)]
    assert "lane/claimtwo" in answer["branches"]
    for invalid in ("broken JSON", json.dumps({"status": "completed", "checkout": {"branch": "topic"}})):
        record.write_text(invalid)
        assert run(tokens["router"], "sweep-claims", "--state", str(state)) == 3
        assert capsys.readouterr().out == ""
    cfg.snapshot.write_text(json.dumps(w.store.snapshot(w.p("router"))))
    server.shutdown()
    server.server_close()
    assert run(tokens["router"], "sweep-claims", "--state", str(state)) == 3
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("lanes", [None, [], ["lane/x"] * 5])
def test_sweep_claims_incomplete_or_unreachable_never_uses_snapshot(capsys, lanes):
    from plangraph.keeper import cli_verbs
    class Reader:
        def call(self, method, verb, params=None):
            if verb == "whoami": return {"role": "router", "projects": []}
            if verb == "snapshot": return {"head_seq": 1, "items": [{"key": "p:A", "state": "in_review"}]}
            return {"key": "p:A", "state": "in_review", "work_exists": {"lanes": lanes}, "run_ids": []}
    assert cli_verbs.cmd_sweep_claims(None, Reader()) == 3
    assert capsys.readouterr().out == ""
    class Down(Reader):
        def call(self, *args): raise cli_verbs.Unreachable("offline")
    assert cli_verbs.cmd_sweep_claims(None, Down()) == 3
    assert capsys.readouterr().out == ""


def test_sweep_role_allows_sweep_claims_and_refuses_writes_and_other_roles(live, tmp_path, capsys):
    import urllib.error
    import urllib.request
    from plangraph.keeper import auth
    from plangraph.keeper.errors import Refused
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper.evidence_runs import RunRecordAdapter

    server, w, tokens, _, _ = live
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def http(token_text, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token_text}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    repo = tmp_path / "sweep-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
         "commit", "--allow-empty", "-qm", "initial"],
        cwd=repo, check=True,
    )
    state = tmp_path / "state"
    for rid, role in (("run-w1", "writer"), ("run-r1", "reviewer")):
        job = state / "runs" / rid / "job.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text(json.dumps({
            "request": {"item_key": "demo:A", "role": role, "model": "m"},
            "family": "teamR",
        }), encoding="utf-8")
    w.store.ctx = {
        "git": GitAdapter({"demo": {"path": str(repo), "ref": "main"}}),
        "runs": RunRecordAdapter(state / "runs"),
    }

    sweep_tok = auth.add_principal(w.conn, "sweep-host", "sweep", [])
    sweep_tf = tmp_path / "sweep.token"
    sweep_tf.write_text(sweep_tok, encoding="utf-8")
    os.chmod(sweep_tf, 0o600)

    # Scoped sweep tokens cannot be minted via add_principal or CLI admin.
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "sweep-scoped", "sweep", ["demo"])
    assert exc.value.code == "validation"
    assert cli.main(["admin", "add-principal", "sweep-scoped", "--role", "sweep", "--projects", "demo"]) == 2
    capsys.readouterr()

    # Defense in depth: even if a legacy/tampered DB row has scoped sweep projects, runtime checks refuse it.
    scoped_sweep_tok = "scoped-sweep-fixture-token"
    w.conn.execute(
        "INSERT INTO principals(name,role,projects,family,token_sha256,active) VALUES(?,?,?,?,?,1)",
        ("sweep-scoped", "sweep", json.dumps(["demo"]), None, auth.hash_token(scoped_sweep_tok)),
    )
    scoped_tf = tmp_path / "sweep-scoped.token"
    scoped_tf.write_text(scoped_sweep_tok, encoding="utf-8")
    os.chmod(scoped_tf, 0o600)

    assert run(tokens["l1main"], "claim", "demo:A") == 0
    writer_run = w.store.mint_run_token(w.p("l1main"), "demo:A", "writer", "run-w1")["token"]
    writer_tf = tmp_path / "writer-run.token"
    writer_tf.write_text(writer_run, encoding="utf-8")
    os.chmod(writer_tf, 0o600)

    spawner_tok = auth.add_principal(w.conn, "sweep-spawner2", "spawner", [])
    w.store.record_run(auth.authenticate(w.conn, spawner_tok), "demo:A",
                       {"run_id": "writer-run-2", "phase": "started"})
    wt = tmp_path / "wt-a"
    subprocess.run(["git", "worktree", "add", "-qb", "lane/a", str(wt)], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
         "commit", "--allow-empty", "-qm", "wip"],
        cwd=wt, check=True,
    )
    rec = state / "runs/writer-run-2/record.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"status": "running", "checkout": str(wt)}), encoding="utf-8")

    assert run(tokens["l1main"], "evidence", "demo:A", "--kind", "decision", "--decision", "D-1") == 0
    assert run(tokens["l1main"], "submit", "demo:A") == 0
    reviewer_run = w.store.mint_run_token(w.p("l1main"), "demo:A", "reviewer", "run-r1")["token"]
    reviewer_tf = tmp_path / "reviewer-run.token"
    reviewer_tf.write_text(reviewer_run, encoding="utf-8")
    os.chmod(reviewer_tf, 0o600)
    capsys.readouterr()

    # Unscoped sweep token succeeds with sweep-claims.
    assert run(sweep_tf, "sweep-claims", "--state", str(state)) == 0
    assert out_json(capsys) == {"paths": [str(wt)], "branches": ["a", "lane/a"]}

    # Scoped sweep, writer run, reviewer run, worker, reviewer and reader tokens are all refused.
    for bad_tf in (scoped_tf, writer_tf, reviewer_tf, tokens["workerB"], tokens["reviewer"], tokens["reader"]):
        assert run(bad_tf, "sweep-claims", "--state", str(state)) != 0
        assert capsys.readouterr().out == ""

    # Sweep token can call only whoami, snapshot, show (unscoped); scoped sweep cannot read snapshot/show.
    assert http(sweep_tok, "GET", "/v1/whoami") == (
        200, {"name": "sweep-host", "role": "sweep", "projects": [], "family": None}
    )
    assert http(sweep_tok, "GET", "/v1/snapshot")[0] == 200
    assert http(sweep_tok, "GET", "/v1/show?key=demo:A&fmt=json")[0] == 200
    assert http(scoped_sweep_tok, "GET", "/v1/snapshot")[0] == 403
    assert http(scoped_sweep_tok, "GET", "/v1/show?key=demo:A&fmt=json")[0] == 403

    # Negative tests: writes and unrelated reads all return 403 (forbidden).
    forbidden_calls = [
        ("POST", "/v1/claim", {"key": "demo:B"}),
        ("POST", "/v1/close_on_decision", {"key": "demo:A", "batch_ref": "b@" + "0" * 64}),
        ("POST", "/v1/close_merged", {"key": "demo:A", "decision_id": "D-1"}),
        ("POST", "/v1/create_items", {"items": [defn("NEW", project="demo")]}),
        ("POST", "/v1/import_items", {"items": [defn("IMP", project="demo")]}),
        ("POST", "/v1/edit_definition", {"key": "demo:B", "fields": {"title": "hijack"}}),
        ("POST", "/v1/apply_ops", {"ops": [{"op": "priority", "key": "demo:B", "priority": 9}]}),
        ("POST", "/v1/set_priority", {"key": "demo:B", "n": 9}),
        ("POST", "/v1/cancel", {"key": "demo:B", "reason": "nope"}),
        ("POST", "/v1/record_run", {"key": "demo:A", "run": {"run_id": "x", "phase": "started"}}),
        ("POST", "/v1/add_evidence", {"key": "demo:A", "epoch": 1, "kind": "decision", "ref": {"id": "D-1"}}),
        ("POST", "/v1/submit", {"key": "demo:A", "epoch": 1}),
        ("POST", "/v1/verdict", {"key": "demo:A", "ok": True}),
        ("POST", "/v1/ask_question", {"key": "demo:A", "text": "q"}),
        ("POST", "/v1/post_message", {"from": "sweep-host", "to": "router", "body": "hi"}),
        ("POST", "/v1/locks/demo/acquire", {"owner": "sweep-host", "ttl_s": 30}),
        ("GET", "/v1/ready", None),
        ("GET", "/v1/digest", None),
        ("GET", "/v1/alerts", None),
        ("GET", "/v1/hosts", None),
        ("GET", "/v1/messages", None),
        ("GET", "/v1/questions", None),
        ("GET", "/v1/alarms", None),
    ]
    for method, path, body in forbidden_calls:
        status, payload = http(sweep_tok, method, path, body)
        assert status == 403 and payload["error"] == "forbidden", (method, path, status, payload)

    # CLI write verbs exit 2 (refused) with sweep token.
    define_file = tmp_path / "define.json"
    swp = defn("SWP", project="demo")
    swp["description"] = "Why: x\nHow: y"
    define_file.write_text(json.dumps({"items": [swp]}), encoding="utf-8")
    edit_file = tmp_path / "edit.json"
    edit_file.write_text(json.dumps({"fields": {"title": "hijack"}}), encoding="utf-8")
    for argv in (
        ("claim", "demo:B"),
        ("close", "demo:A", "--batch", "b@" + "0" * 64),
        ("define", "--file", str(define_file)),
        ("define", "demo:B", "--edit", "--file", str(edit_file)),
        ("priority", "demo:B", "5"),
    ):
        assert run(sweep_tf, *argv) == 2
        assert "forbidden" in capsys.readouterr().err

    # Direct Store calls also refuse with Refused("forbidden") and audit the denial.
    sweep_p = auth.authenticate(w.conn, sweep_tok)
    for fn in (
        lambda: w.store.claim(sweep_p, "demo:B"),
        lambda: w.store.close_on_decision(sweep_p, "demo:A", "b@" + "0" * 64),
        lambda: w.store.create_items(sweep_p, [defn("NEW2", project="demo")]),
        lambda: w.store.edit_definition(sweep_p, "demo:B", {"title": "x"}),
        lambda: w.store.ready(sweep_p),
        lambda: w.store.messages(sweep_p),
    ):
        with pytest.raises(Refused) as exc:
            fn()
        assert exc.value.code == "forbidden"
    denied_actions = {
        row["action"]
        for row in w.conn.execute(
            "SELECT action FROM audit_log WHERE actor=? AND ok=0 AND code='forbidden'",
            ("sweep-host",),
        ).fetchall()
    }
    assert {"claim", "close_on_decision", "create_items", "edit_definition", "ready", "messages"} <= denied_actions


def test_sweep_transport_and_store_refusal_layers(live, monkeypatch):
    import urllib.error
    import urllib.request
    from plangraph.keeper import api_routes, auth
    from plangraph.keeper.errors import Refused

    server, w, _, _, _ = live
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    sweep_tok = auth.add_principal(w.conn, "sweep-layer", "sweep", [])
    sweep_p = auth.authenticate(w.conn, sweep_tok)

    def http(method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {sweep_tok}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # Transport layer (api_routes.run_gate) refuses directly and before body validation or non-Store routes.
    for verb in ("claim", "hosts", "messages"):
        with pytest.raises(Refused) as exc:
            api_routes.run_gate(sweep_p, verb)
        assert exc.value.code == "forbidden"

    # Empty body on POST /v1/claim would return 400 validation if it reached _write; run_gate refuses 403 first.
    status, payload = http("POST", "/v1/claim", {})
    assert status == 403 and payload["error"] == "forbidden"
    assert w.conn.execute(
        "SELECT count(*) FROM audit_log WHERE actor='sweep-layer' AND action='claim'"
    ).fetchone()[0] == 0

    # GET /v1/hosts bypasses Store._run, so run_gate is the layer that refuses it.
    status, payload = http("GET", "/v1/hosts")
    assert status == 403 and payload["error"] == "forbidden"

    # When transport gate is bypassed, Store._run refuses and records the audited denial.
    monkeypatch.setattr(api_routes, "run_gate", lambda principal, name: None)
    status, payload = http("POST", "/v1/claim", {"key": "demo:B"})
    assert status == 403 and payload["error"] == "forbidden"
    row = w.conn.execute(
        "SELECT ok, code FROM audit_log WHERE actor='sweep-layer' AND action='claim'"
    ).fetchone()
    assert row is not None and row["ok"] == 0 and row["code"] == "forbidden"

    # Direct store.messages() refusal also flows through Store._run and is audited.
    with pytest.raises(Refused) as exc:
        w.store.messages(sweep_p)
    assert exc.value.code == "forbidden"
    msg_row = w.conn.execute(
        "SELECT ok, code FROM audit_log WHERE actor='sweep-layer' AND action='messages'"
    ).fetchone()
    assert msg_row is not None and msg_row["ok"] == 0 and msg_row["code"] == "forbidden"


def test_live_server_has_no_background_snapshot_writer(live):
    server, _, _, cfg, _ = live
    assert cfg.snapshot_debounce_s == 0
    assert server.snapshot_writer is None
    assert not [t for t in threading.enumerate() if t.name == "keeper-snapshot"]


def test_attempts_cli_reads_writer_seat_and_adhoc_rows(live, capsys, monkeypatch):
    _, w, tokens, _, _ = live
    key = "demo:A"
    w.store.put_card(w.p("router"), key, {"outcome": "fixture"})
    route = {"eligible_n": 1, "rejected": [{"leg": "other", "reason": "unavailable"}],
             "top5": [{"leg": "fixture", "effort": "high", "u_core": 0.7,
                       "p_source": "prior", "n": None}], "chosen": "fixture",
             "explore": False, "pin": False,
             "baselines": {"static_v0": "fixture", "strongest": "fixture", "cheapest": "fixture"}}
    monkeypatch.setattr("plangraph.keeper.store_card_updates._validate_spawn_route", lambda value: value)
    for ident, role, item in [("run-writer", "implement", key), ("run-seat", "review", key),
                              ("run-adhoc", "review", None)]:
        row = dict(attempt_id=ident, role=role, card_key=item, harness="codex", leg="fixture",
                   requested_model="request", served_model="served", effort="high", level="L2",
                   level_reason="size_bucket", task_class="medium_change", risk_band="normal",
                   bucket="S2", host="test-host", plan_hash="0" * 64, route=route,
                   started=w.clock()-60, ended=w.clock(), exit_class="ok")
        assert w.store.record_spawn_attempt(w.p("system"), row)["duplicate"] is False
        assert w.store.record_spawn_attempt(w.p("system"), row)["duplicate"] is True
    assert run(tokens["reader"], "attempts", "--since", "1d", "--json") == 0
    rows = {row["attempt_id"]: row for row in out_json(capsys)}
    assert set(rows) == {"run-writer", "run-seat", "run-adhoc"}
    assert rows["run-writer"]["card_key"] == rows["run-seat"]["card_key"] == key
    assert rows["run-adhoc"]["card_key"] is None and rows["run-adhoc"]["item"] == "adhoc"
    assert rows["run-seat"]["route"] == route
    assert rows["run-writer"]["level_reason"] == "size_bucket"
    assert {row["attempt_id"] for row in w.store.spawn_attempts(w.p("l1main"), "1d")} == {"run-writer", "run-seat"}
    assert w.store.spawn_attempts(w.p("reader"), "1s") == []


def test_attempt_telemetry_reads_every_row_and_requires_global_reader(live, monkeypatch):
    import urllib.error
    import urllib.request

    from plangraph.keeper import auth

    _, w, _, _, _ = live
    monkeypatch.setattr("plangraph.keeper.store_card_updates._validate_spawn_route", lambda route: route)
    for project in ("demo", "other"):
        token = auth.add_principal(w.conn, f"{project}-spawner", "spawner", [project])
        writer = auth.authenticate(w.conn, token)
        for n in range(9):
            w.store.record_spawn_attempt(writer, dict(
                attempt_id=f"run-{project}-{n}", role="implement",
                card_key=f"{project}:A", host="fixture", plan_hash="0" * 64,
                route={}, started=w.clock() - 2, ended=w.clock(), exit_class="ok"))
    w.store.record_spawn_attempt(w.p("system"), dict(
        attempt_id="run-adhoc", role="review", host="fixture", plan_hash="0" * 64,
        route={}, started=w.clock() - 2, ended=w.clock(), exit_class="ok"))
    w.conn.execute("INSERT INTO attempts (id, n, started_at, exit_class) VALUES (?, ?, ?, ?)",
                   (99, 1, w.store._iso(w.clock() - 2), "ok"))
    url = os.environ["PG_URL"] + "/v1/attempts?since=1d"

    def read(token, scope=""):
        request = urllib.request.Request(url + scope, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    rows = read(w.tok("reader"), "&scope=telemetry")["rows"]
    assert len(rows) == w.conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 20
    assert {row["item"] for row in rows} == {"demo:A", "other:A", "adhoc"}
    assert [row["attempt_id"] for row in rows if row["route"] is None] == ["legacy:99"]
    assert len(read(w.tok("l1main"))) == 9
    scoped_reader = auth.add_principal(w.conn, "scoped-reader", "reader", ["demo"])
    for token in (scoped_reader, w.tok("l1main"), w.tok("router")):
        with pytest.raises(urllib.error.HTTPError) as refusal:
            read(token, "&scope=telemetry")
        assert refusal.value.code == 403


def test_watchdog_delivers_spawn_through_keeper_http(live, capsys, monkeypatch, tmp_path):
    from plangraph.spawner.notify import KeeperSink, retry_spawn_attempts

    _, w, tokens, _, _ = live
    key = "demo:A"
    w.store.put_card(w.p("router"), key, {"outcome": "fixture"})
    run_id = "run-delivery"
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True)
    events = []
    row = dict(attempt_id=run_id, card_key=key, role="implement", host="fixture",
               level="L1", plan_hash="0" * 64, started=w.clock() - 10,
               ended=w.clock(), exit_class="ok", route={"eligible_n": 1})

    def project(argv, **kwargs):
        if argv[-1] == "fleet-snapshot":
            return subprocess.CompletedProcess(argv, 0, stdout="{}")
        events.append(argv[-1])
        if argv[-1] == "project-spawn-attempt":
            assert json.loads(kwargs["input"])["run_id"] == run_id
            value = row
        else:
            assert argv[-1] == "validate-attempt-route"
            value = row["route"]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value))

    monkeypatch.setattr("plangraph.fleet_snapshot._projection_binary", lambda: "pg-spawnd")
    monkeypatch.setattr(subprocess, "run", project)
    record = {"run_id": run_id, "attempt_capture": 1, "status": "completed", "ended": w.clock()}
    sink = KeeperSink(token_file=str(tokens["router"]))
    assert retry_spawn_attempts(tmp_path, [record], keeper=sink)[0]["reason"] == "KeeperRefused"
    assert not (directory / "attempt.delivered").exists()
    token = tmp_path / "system.token"
    token.write_text(w.tok("system"), encoding="utf-8")
    sink = KeeperSink(token_file=str(token))
    assert retry_spawn_attempts(tmp_path, [record], keeper=sink) == []
    assert retry_spawn_attempts(tmp_path, [record], keeper=sink) == []
    assert (directory / "attempt.delivered").read_text() == "delivered\n"
    assert events == ["project-spawn-attempt", "project-spawn-attempt", "validate-attempt-route"]
    assert run(tokens["reader"], "attempts", "--since", "1d", "--json") == 0
    assert out_json(capsys)[0]["attempt_id"] == run_id


@pytest.mark.parametrize(("ident", "role", "item", "card_key"), [
    ("run-fix", "fix", None, None),
    ("run-cardless", "implement", "demo:A", None),
    ("run-unknown-item", "review", "demo:UNREGISTERED", None),
])
def test_attempts_preserve_fixer_and_cardless_item_identity(
        live, capsys, monkeypatch, ident, role, item, card_key):
    _, w, tokens, _, _ = live
    monkeypatch.setattr("plangraph.keeper.store_card_updates._validate_spawn_route", lambda value: value)
    row = dict(attempt_id=ident, role=role, card_key=item, host="fixture",
               plan_hash="0" * 64, route={}, started=w.clock()-1, ended=w.clock(), exit_class="ok")
    assert w.store.record_spawn_attempt(w.p("system"), row)["duplicate"] is False
    assert run(tokens["reader"], "attempts", "--since", "1d", "--json") == 0
    (stored,) = out_json(capsys)
    assert (stored["role"], stored["item"], stored["card_key"]) == (role, item or "adhoc", card_key)
    if item:
        assert w.store.spawn_attempts(w.p("l1main"), "1d") == [stored]
        assert w.store.spawn_attempts(w.p("l1other"), "1d") == []


@pytest.mark.parametrize(("status", "attempts_per_refusal"), [(400, 1), (422, 1), (404, 2)])
def test_refused_attempts_cannot_starve_newer_deliveries(
        tmp_path, monkeypatch, status, attempts_per_refusal):
    from collections import Counter
    from plangraph.spawner.notify import KeeperRefused, retry_spawn_attempts

    calls = Counter()

    class Keeper:
        url = token_file = "configured"

        def spawn_attempt(self, attempt):
            run_id = attempt["run_id"]
            calls[run_id] += 1
            if run_id.startswith("run-refused-"):
                raise KeeperRefused(status)

    def project(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=kwargs["input"])

    monkeypatch.setattr("plangraph.fleet_snapshot._projection_binary", lambda: "pg-spawnd")
    monkeypatch.setattr(subprocess, "run", project)
    limit = 2
    refused = [f"run-refused-{i}" for i in range(limit + 1)]
    runs = []
    for ended, run_id in enumerate([*refused, "run-valid"], start=1):
        directory = tmp_path / "runs" / run_id
        directory.mkdir(parents=True)
        runs.append(dict(run_id=run_id, attempt_capture=1, status="completed", ended=ended))
    # Completion refusals are independent of attempt delivery.
    completion_journal = tmp_path / "runs" / refused[0] / "keeper.refused"
    completion_journal.write_text(f"{status}\n", encoding="utf-8")

    keeper = Keeper()
    retry_spawn_attempts(tmp_path, runs, keeper=keeper, limit=limit)
    assert (tmp_path / "runs" / refused[0] / "attempt.delivered").exists() == (attempts_per_refusal == 1)
    for _ in range(2 * attempts_per_refusal):
        retry_spawn_attempts(tmp_path, runs, keeper=keeper, limit=limit)

    for run_id in refused:
        marker = tmp_path / "runs" / run_id / "attempt.delivered"
        assert marker.read_text(encoding="utf-8") == f"refused: HTTP {status}\n"
        assert calls[run_id] == attempts_per_refusal
    assert (tmp_path / "runs" / "run-valid" / "attempt.delivered").read_text() == "delivered\n"
    assert calls["run-valid"] == 1


def test_cli_lease_batches_report_each_row_and_refuse_partial_success(live, capsys):
    _, w, tokens, _, _ = live
    token = tokens["l1main"]
    assert run(token, "claim-batch", "demo:A", "demo:missing", "--lease", "2m", "--json") == 2
    claimed = out_json(capsys)["results"]
    assert claimed[0] == {"key": "demo:A", "epoch": 1,
                          "lease_until": w.store._iso(w.clock() + 120)}
    assert (claimed[1]["key"], claimed[1]["error"]) == ("demo:missing", "not_found")

    assert run(token, "heartbeat-batch", "--claim", "demo:A", "1",
               "--claim", "demo:missing", "1", "--json") == 2
    renewed = out_json(capsys)["results"]
    assert renewed[0]["key"] == "demo:A"
    assert renewed[0]["lease_until"] > claimed[0]["lease_until"]
    assert (renewed[1]["key"], renewed[1]["error"]) == ("demo:missing", "not_found")

    assert run(token, "heartbeat-batch", "--claim", "demo:A", "1") == 0
    assert "demo:A epoch 1 lease" in capsys.readouterr().out
    assert run(token, "heartbeat-batch", "--claim", "demo:A", "wrong") == 2
    assert "epoch must be an integer" in capsys.readouterr().err


def test_cli_claim_batch_prints_the_train_base(live, capsys, monkeypatch):
    _, w, tokens, _, _ = live
    monkeypatch.setattr(w.store, "ctx", {
        "spawner_records": SimpleNamespace(review_seats=lambda *args, **options: [{"verdict": "pass"}]),
    })
    router = w.p("router")
    repo, head = "example/repo", "c" * 40
    lease = w.store.land(router, dict(repo=repo, action="acquire", owner="fixture"))
    w.store.land(router, dict(
        lease, repo=repo, action="prepare", pr=10, base="a" * 40, head=head,
        parts=[dict(pr=1, head="b" * 40, items=["demo:A"],
                    review_runs={"demo:A": "run-" + "1" * 32})],
    ))

    assert run(tokens["l1main"], "claim-batch", "demo:B", "demo:A") == 0
    assert w.store.claim_train_base("demo:B") == head
    lease_until = w.store._item("demo:B")["lease_until"]
    assert capsys.readouterr().out.splitlines() == [
        f"demo:B epoch 1 lease {lease_until} | base {head}",
        f"demo:A epoch 1 lease {lease_until}",
    ]
