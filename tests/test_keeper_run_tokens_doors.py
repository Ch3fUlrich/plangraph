"""M2B-RUN-TOKENS through the HTTP API, the pg CLI and the MCP route table."""
import json

import pytest

from door_util import Doors
from keeper_util import World, seed, to_claimed
from plangraph.keeper import api_routes, mcp
from plangraph.keeper.api import KeeperHandler  # noqa: F401 - import smoke
from test_keeper_run_tokens import FakeRuns


@pytest.fixture
def d(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", [])])
    world.store.ctx = {"runs": FakeRuns()}
    doors = Doors(world, tmp_path)
    yield doors
    doors.close()
    world.close()


def body(key="demo:A", role="writer", run="w1"):
    return {"key": key, "role": role, "run_id": run}


def test_http_mint_and_use(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    status, out = d.http("l1main", "POST", "/v1/mint_run_token", body())
    assert status == 200 and out["token"] and out["epoch"] == epoch
    w.tokens["runw"] = out["token"]
    assert d.http("runw", "POST", "/v1/heartbeat",
                  {"key": "demo:A", "epoch": epoch})[0] == 200
    snap = d.server.config.snapshot
    import os
    d.server.flush_snapshot()                         # the debounced file exists before it is removed
    os.unlink(snap)
    assert d.http("runw", "POST", "/v1/add_evidence",
                  {"key": "demo:A", "epoch": epoch, "kind": "decision",
                   "ref": {"id": "D-1"}})[0] == 200
    d.server.flush_snapshot()
    assert os.path.exists(snap)                      # snapshot still refreshed
    assert d.http("runw", "POST", "/v1/submit",
                  {"key": "demo:A", "epoch": epoch, "source_round": 0})[0] == 200
    # replay after submit: the revoked token no longer authenticates at all
    status, err = d.http("runw", "POST", "/v1/submit",
                         {"key": "demo:A", "epoch": epoch})
    assert status == 401 and err["error"] == "forbidden"


def test_http_run_token_is_403_everywhere_else(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    w.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token",
                              body())[1]["token"]
    for verb in sorted(api_routes.READ_VERBS - {"write_context"}):
        status, _ = d.http("runw", "GET", f"/v1/{verb}?key=demo:A")
        assert status == 403, verb
    for verb in sorted(api_routes.WRITE_VERBS - {"heartbeat", "add_evidence",
                                                  "submit", "replay", "write_receipt", "bind_seat_verdict"}):  # replay: M2B-OUTBOX
        status, _ = d.http("runw", "POST", f"/v1/{verb}",
                           {"key": "demo:A", "epoch": epoch, "role": "writer",
                            "run_id": "w1", "ok": True})  # malformed or not: 403
        assert status == 403, verb


def test_http_mint_authorisation(d):
    to_claimed(d.world, "demo:A")
    for who in ("workerA", "reviewer", "l1other", "reader"):
        assert d.http(who, "POST", "/v1/mint_run_token", body())[0] == 403
    assert d.http("router", "POST", "/v1/mint_run_token", body())[0] == 200
    assert d.http("l1main", "POST", "/v1/mint_run_token",
                  body(run="nope"))[0] == 400
    assert d.http("l1main", "POST", "/v1/mint_run_token", {"key": "demo:A"})[0] == 400


@pytest.fixture(scope="session")
def native_client_binary(tmp_path_factory):
    import shutil
    import subprocess
    from pathlib import Path
    from _spawnd import built_rust_binary
    from _split_ci_environment import cargo_fixture_deadline

    manifest = Path(__file__).resolve().parents[1] / "rust/Cargo.toml"
    with cargo_fixture_deadline(600):
        subprocess.run(["cargo", "build", "--manifest-path", str(manifest),
                        "-p", "plangraph-cli", "--bin", "plangraph"], check=True)
    binary = built_rust_binary("plangraph")
    # Other lanes can replace the shared executable while these tests run.
    return shutil.copy2(binary, tmp_path_factory.mktemp("native-client") / binary.name)


@pytest.fixture
def native_client(d, monkeypatch, native_client_binary):
    import os

    monkeypatch.setenv("PATH", os.path.dirname(native_client_binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PG_OUTBOX", str(d.tmp / "outbox.jsonl"))
    return d


def test_cli_token_stdout_only_and_env_epoch(native_client, monkeypatch):
    d = native_client
    w = d.world
    epoch = to_claimed(w, "demo:A")
    code, out, err = d.pg("l1main", "token", "demo:A", "--role", "writer",
                          "--run", "w1")
    assert code == 0
    token = out.strip()
    assert token.startswith("pgr1_") and "\n" not in token
    assert token not in err and "pgr1_" not in err
    assert d.pg("workerA", "token", "demo:A", "--role", "writer",
                "--run", "w1")[0] == 2
    w.tokens["runw"] = token
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    assert d.pg("runw", "heartbeat", "demo:A")[0] == 0
    assert d.pg("runw", "whoami")[0] == 2
    files = list((d.tmp / "outbox.jsonl.d" / "done").glob("*.json"))
    assert len(files) == 1 and token not in files[0].read_text()
    if __import__("os").name != "nt":
        assert files[0].stat().st_mode & 0o777 == 0o600


def test_native_replay_recovers_submit_after_socket_loses_reply(native_client, monkeypatch):
    import socket

    d = native_client
    epoch = to_claimed(d.world, "demo:A")
    d.world.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token", body())[1]["token"]
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    assert d.pg("runw", "heartbeat", "demo:A")[0] == 0
    d.world.store.add_evidence(d.world.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    handler = d.server.RequestHandlerClass
    send_json = handler._json
    dropped = []

    def drop_first_submit(self, status, payload):
        if self.path == "/v1/write_receipt" and isinstance(payload, dict) \
                and payload.get("reply", {}).get("state") == "in_review" and not dropped:
            dropped.append(payload["id"])
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        return send_json(self, status, payload)

    monkeypatch.setattr(handler, "_json", drop_first_submit)
    assert d.pg("runw", "submit", "demo:A")[0] == 4
    assert len(dropped) == 1
    status, output, _ = d.pg("runw", "replay")
    assert status == 0 and output.strip() == f"{dropped[0]} duplicate"
    assert d.world.conn.execute("SELECT COUNT(*) FROM events WHERE type='submitted'").fetchone()[0] == 1


def test_old_round_submit_is_refused_on_live_and_receipt_routes(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    w.store.verdict(w.p("reviewer"), "demo:A", False, "review changes")
    minted = d.http("l1main", "POST", "/v1/mint_run_token", body(run="w2"))[1]
    w.tokens["runw"] = minted["token"]
    old = {"key": "demo:A", "epoch": epoch, "source_round": 0}
    assert d.http("runw", "POST", "/v1/submit", old)[0] == 409
    submit = {"id": "01" * 16, "verb": "submit", "body": old}
    assert d.http("runw", "POST", "/v1/write_receipt", submit)[1]["result"] == "round_stale"
    assert d.http("runw", "POST", "/v1/write_receipt", {
        **submit, "body": {**old, "source_round": 1}})[0] == 409
    assert d.http("runw", "POST", "/v1/write_receipt", {
        **submit, "id": "02" * 16, "body": {**old, "source_round": 1}})[1]["result"] == "ok"


def test_mcp_tool_routes():
    names = {t["name"] for t in mcp.TOOLS}
    assert "mint_run_token" in names
    assert mcp._ROUTES["mint_run_token"] == ("POST", "mint_run_token")


def test_native_queue_acknowledges_receipt_quota(native_client, monkeypatch):
    from plangraph.keeper import store_outbox

    d = native_client
    epoch = to_claimed(d.world, "demo:A")
    d.world.tokens["runw"] = d.http("l1main", "POST", "/v1/mint_run_token", body())[1]["token"]
    monkeypatch.setenv("PG_EPOCH", str(epoch))
    monkeypatch.setattr(store_outbox, "MAX_SEEN_PER_TOKEN", 1)
    assert d.pg("runw", "heartbeat", "demo:A")[0] == 0
    code, _, err = d.pg("runw", "heartbeat", "demo:A")
    assert code == 2 and "outbox limit" in err
    queue = d.tmp / "outbox.jsonl.d"
    assert not list(queue.glob("*.json"))
    assert len(list((queue / "done").glob("*.json"))) == 2


def test_seat_binding_refuses_late_same_head_on_direct_and_replay(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    head = "a" * 40
    from evidence_fakes import FakeGit
    w.store.ctx["git"] = FakeGit(trailers_by_sha={head: {"Writer": "openai/gpt-5 run-writer"}})
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "commit+ci", {"commit": head})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    binding = {"key": "demo:A", "epoch": epoch, "round": 1, "head": head, "seat_run": "run-early"}
    assert d.http("router", "POST", "/v1/admit_seat_verdict", binding) == (200, binding)
    seat = {"key": "demo:A", "ok": True, "reason": "seat witnessed result", "binding": binding}
    assert d.http("reviewer", "POST", "/v1/bind_seat_verdict", seat)[0] == 200
    w.store.verdict(w.p("reviewer2"), "demo:A", False, "manual changes")
    w.store.submit(w.p("l1main"), "demo:A", epoch)

    assert d.http("reviewer", "POST", "/v1/verdict", seat)[0] == 409
    entry = {"id": "01" * 16, "verb": "verdict", "body": seat}
    assert d.http("reviewer", "POST", "/v1/write_receipt", entry)[0] == 409
    assert d.http("reviewer", "POST", "/v1/replay", {"entries": [entry]})[1]["results"][0]["result"] == "round_stale"
    assert w.store.show(w.p("reader"), "demo:A")["round"] == 2


def test_seat_payload_immutable_and_unbound_ingresses_refused(d):
    w = d.world
    epoch = to_claimed(w, "demo:A")
    head = "b" * 40
    from evidence_fakes import FakeGit
    w.store.ctx["git"] = FakeGit(trailers_by_sha={head: {"Writer": "openai/gpt-5 run-writer"}})
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "commit+ci", {"commit": head})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    seat = {"key": "demo:A", "ok": False, "reason": "seat witnessed changes"}
    entry = {"id": "01" * 16, "verb": "verdict", "body": seat}
    for path, payload in [("verdict", seat), ("write_receipt", entry), ("bind_seat_verdict", seat)]:
        assert d.http("reviewer", "POST", "/v1/" + path, payload)[0] == 403
    assert d.http("reviewer", "POST", "/v1/replay", {"entries": [entry]})[1]["results"][0]["result"] == "forbidden"
    binding = {"key": "demo:A", "epoch": epoch, "round": 1, "head": head, "seat_run": "run-payload"}
    assert d.http("router", "POST", "/v1/admit_seat_verdict", binding)[0] == 200
    bound = dict(seat, binding=binding)
    invalid = dict(entry, body=dict(bound, ok="false"))
    assert d.http("reviewer", "POST", "/v1/replay", {"entries": [invalid]})[1]["results"][0]["result"] == "validation"
    assert w.store.show(w.p("reader"), "demo:A")["state"] == "in_review"
    assert d.http("reviewer", "POST", "/v1/bind_seat_verdict", bound)[0] == 200
    changed = dict(bound, reason="seat other changes")
    assert d.http("reviewer", "POST", "/v1/bind_seat_verdict", changed)[0] == 409
    assert d.http("reviewer", "POST", "/v1/verdict", changed)[0] == 409
    status, replay = d.http("reviewer", "POST", "/v1/replay", {"entries": [dict(entry, body=bound)]})
    assert status == 200 and replay["results"][0]["result"] == "ok", replay
    verdict = replay["results"][0]["reply"]
    assert verdict["state"] == "claimed"
    assert d.http("reviewer", "POST", "/v1/verdict", bound) == (200, verdict)


def test_repost_sending_seat_reads_original_receipt(d, monkeypatch):
    from pathlib import Path
    from evidence_fakes import FakeGit
    from plangraph.spawner.notify import KeeperSink

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    import seat_verdict
    from seat_backfill import save_verdict_payload

    world = d.world
    epoch = to_claimed(world, "demo:A")
    head = "a" * 40
    world.store.ctx["git"] = FakeGit(trailers_by_sha={head: {"Writer": "openai/gpt-5 run-writer"}})
    world.store.add_evidence(world.p("l1main"), "demo:A", epoch, "commit+ci", {"commit": head})
    world.store.submit(world.p("l1main"), "demo:A", epoch)
    run_dir = d.tmp / "run-repost"
    run_dir.mkdir()
    record = {"run_id": run_dir.name, "item_key": "demo:A",
              "given_writer_families": ["openai"], "completion": {"head_sha": head}}
    router = d.tmp / "router.token"
    router.write_text(world.tokens["router"])
    monkeypatch.setattr("plangraph.spawner.notify.subprocess.check_output", lambda *a, **kw: head)
    KeeperSink(d.url, router).admit_seat_verdict(record, run_dir)
    binding = record["request"]["verdict_binding"]
    assert binding == {"key": "demo:A", "epoch": epoch, "round": 1, "head": head, "seat_run": run_dir.name}
    (run_dir / "record.json").write_text(json.dumps(record))
    (run_dir / "record.json").chmod(0o600)
    payload = {"key": "demo:A", "ok": False, "reason": "seat requested changes", "binding": binding}
    saved = save_verdict_payload(run_dir, "deepseek", payload)
    assert saved["payload"] == payload
    (run_dir / "verdict.json").write_text('{"status":"sending","attempts":0}')
    assert d.http("reviewer", "POST", "/v1/bind_seat_verdict", payload)[0] == 200
    assert d.http("reviewer", "POST", "/v1/verdict", payload)[0] == 200

    (d.tmp / "reviewer-deepseek.token").write_text(world.tokens["reviewer"])
    assert seat_verdict.main(["--repost-transport", "--key", "demo:A", "--seat-dir", str(run_dir),
                              "--token-dir", str(d.tmp), "--url", d.url]) == 0
    assert json.loads((run_dir / "verdict.json").read_text())["status"] == "delivered"
    assert world.conn.execute("SELECT COUNT(*) FROM events WHERE type='changes_requested'").fetchone()[0] == 1


def test_reviewer_run_token_cannot_cross_seats_and_replays_exact_receipt(d):
    from evidence_fakes import FakeGit

    class SeatRuns(FakeRuns):
        def job(self, run_id):
            if run_id in ("run-seat", "run-other"):
                return {"request": {"item_key": "demo:A", "role": "review", "family": "deepseek"}}
            return super().job(run_id)

    w = d.world
    epoch = to_claimed(w, "demo:A")
    head = "c" * 40
    w.store.ctx = {"runs": SeatRuns(),
                   "git": FakeGit(trailers_by_sha={head: {"Writer": "openai/gpt-5 run-writer"}})}
    w.store.add_evidence(w.p("l1main"), "demo:A", epoch, "commit+ci", {"commit": head})
    w.store.submit(w.p("l1main"), "demo:A", epoch)
    binding = {"key": "demo:A", "epoch": epoch, "round": 1, "head": head, "seat_run": "run-seat"}
    assert d.http("router", "POST", "/v1/admit_seat_verdict", binding) == (200, binding)
    for actor, run in (("seat", "run-seat"), ("other", "run-other")):
        status, minted = d.http("l1main", "POST", "/v1/mint_run_token", body(role="reviewer", run=run))
        assert status == 200, minted
        w.tokens[actor] = minted["token"]

    decision = {"key": "demo:A", "ok": False, "reason": "Please update the tests"}
    assert d.http("seat", "POST", "/v1/verdict", decision)[0] == 403
    decision["binding"] = binding
    for route in ("/v1/bind_seat_verdict", "/v1/verdict"):
        status, rejected = d.http("other", "POST", route, decision)
        assert status == 403 and "seat run does not match" in rejected["msg"]
    assert d.http("seat", "POST", "/v1/bind_seat_verdict", decision)[0] == 200
    status, committed = d.http("seat", "POST", "/v1/verdict", decision)
    assert status == 200 and committed["state"] == "claimed", committed
    assert d.http("seat", "POST", "/v1/verdict", {"key": "demo:A", "ok": False})[0] == 401

    # Receipt SQL stores the item key separately from the optional request hash.
    rows = w.conn.execute("SELECT verb,key,request_hash FROM op_receipts WHERE key=? ORDER BY verb",
                          ("demo:A",)).fetchall()
    assert [(row["verb"], row["key"], row["request_hash"]) for row in rows] == [
        ("seat_admission", "demo:A", None), ("seat_outcome", "demo:A", None),
        ("seat_payload", "demo:A", None)]
    entry = {"id": "03" * 16, "verb": "verdict", "body": decision}
    receipt = {"id": entry["id"], "result": "ok", "reply": committed}
    assert d.http("seat", "POST", "/v1/verdict", decision) == (200, committed)
    assert d.http("seat", "POST", "/v1/write_receipt", entry) == (200, receipt)
    assert d.http("seat", "POST", "/v1/write_receipt", {"verb": "verdict"})[0] == 401
    assert d.http("seat", "POST", "/v1/replay", {"entries": [entry]}) == (
        200, {"results": [receipt], "recovery": []})
    assert d.http("other", "POST", "/v1/replay", {"entries": [entry]})[0] == 401
    changed = dict(entry, body=dict(decision, reason="A different decision"))
    assert d.http("seat", "POST", "/v1/replay", {"entries": [changed]})[0] == 409
    assert w.conn.execute("SELECT COUNT(*) FROM events WHERE type='changes_requested'").fetchone()[0] == 1
