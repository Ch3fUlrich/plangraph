"""HTTP API: auth, refusals, forged actor ignored, body cap, health, text."""
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from keeper_util import World, defn, seed, to_done
from plangraph.keeper import api, auth, config, snapshot_io
from plangraph.keeper.cli_verbs import Session, Unreachable


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    yield world
    world.close()


def make_cfg(tmp_path, world=None):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path) if world else "k.db",
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def srv(w, tmp_path):
    cfg = make_cfg(tmp_path, w)
    server = api.make_server(w.store, cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(srv, path, *, method="GET", body=None, token=None, raw=False, idempotency_key=None):
    url = srv.base_url + path
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        if isinstance(data, str):
            data = data.encode()
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if idempotency_key is not None:
        req.add_header("Idempotency-Key", idempotency_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw_body = resp.read()
            return resp.status, resp.headers.get("Content-Type", ""), raw_body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def jbody(raw):
    return json.loads(raw.decode())


def test_health_is_open(srv):
    status, _, raw = call(srv, "/v1/health")
    assert status == 200
    assert jbody(raw)["status"] == "ok"


def test_auth_is_required(srv):
    status, _, raw = call(srv, "/v1/whoami")
    assert status == 401
    assert jbody(raw)["error"] == "forbidden"


def test_unknown_token_is_401(srv):
    status, _, raw = call(srv, "/v1/whoami", token="nope")
    assert status == 401


@pytest.mark.parametrize("payload", [{"verb": "verdict"}, {"verb": "verdict", "id": "invalid"}, b"{"])
def test_malformed_verdict_receipt_requires_authentication(srv, w, monkeypatch, payload):
    audit = []
    monkeypatch.setattr(w.store, "audit_auth_failure", audit.append)
    status, _, raw = call(srv, "/v1/write_receipt", method="POST", body=payload, token="invalid")
    assert (status, jbody(raw)["error"]) == (401, "forbidden")
    assert audit == ["unknown or revoked token"]


def test_unauthenticated_writes_never_parse_or_touch_the_store(srv, w, monkeypatch):
    audit = []

    class AuthenticationOnly:
        conn = w.conn
        audit_auth_failure = audit.append

        def __getattr__(self, name):
            raise AssertionError(f"unauthenticated request touched the store through {name}")

    def read_body(handler):
        raise AssertionError("unauthenticated request read the body")

    with monkeypatch.context() as patch:
        patch.setattr(srv, "store", AuthenticationOnly())
        patch.setattr(api.KeeperHandler, "_read_body", read_body)
        for verb in sorted(api._WRITE_VERBS):
            status, _, raw = call(srv, "/v1/" + verb, method="POST", body=b"{")
            assert (status, jbody(raw)["error"]) == (
                403 if verb == "ingest_host_sample" else 401, "forbidden"), verb
    assert audit == ["missing token"] * len(api._WRITE_VERBS)


def test_refused_maps_to_4xx_with_code(srv, w):
    status, _, raw = call(srv, "/v1/claim", method="POST",
                          body={"key": "demo:A"}, token=w.tok("reader"))
    assert 400 <= status < 500
    assert jbody(raw)["error"] == "forbidden"


def test_forged_actor_in_body_is_ignored(srv, w):
    status, _, raw = call(
        srv, "/v1/claim", method="POST",
        body={"key": "demo:A", "actor": "router", "role": "router"},
        token=w.tok("workerA"))
    assert status == 200, raw
    assert jbody(raw)["key"] == "demo:A"
    status, _, raw = call(srv, "/v1/show?key=demo:A&fmt=json", token=w.tok("reader"))
    assert jbody(raw)["claimed_by"] == "workerA"


def test_the_token_principal_is_used_not_the_body(srv, w):
    # set_priority is router-only; a worker token must be refused even though
    # the body claims to be the router.
    status, _, raw = call(srv, "/v1/set_priority", method="POST",
                          body={"key": "demo:A", "n": 5, "role": "router"},
                          token=w.tok("workerA"))
    assert status == 403
    assert jbody(raw)["error"] == "forbidden"


@pytest.mark.parametrize("verb,actor,fields,different_fields,event", [
    ("set_priority", "router", {"n": 7}, {"n": 8}, "priority_set"),
    ("claim", "workerA", {"lease_s": 600}, {"lease_s": 1200}, "claimed"),
], ids=["priority", "claim"])
def test_retry_after_timeout_replays_committed_write(srv, w, monkeypatch, verb, actor,
                                                     fields, different_fields, event):
    session = Session(url=srv.base_url)
    monkeypatch.setattr(session, "token", lambda: w.tok(actor))
    entered, release = threading.Event(), threading.Event()
    send = api.KeeperHandler._json
    replies = []

    def delayed_response(handler, status, payload):
        if status == 200 and not entered.is_set():
            replies.append(payload)
            entered.set()
            assert release.wait(5)
        return send(handler, status, payload)

    monkeypatch.setattr(api.KeeperHandler, "_json", delayed_response)
    body = {"key": "demo:A", **fields}
    request_key = f"retry-{verb}"
    expected = ({"key": "demo:A", "epoch": 1, "lease_until": w.store._iso(w.store.now() + 600)}
                if verb == "claim" else {"key": "demo:A", "priority": 7})
    try:
        with pytest.raises(Unreachable, match="timed out"):
            session.call("POST", verb, body=body, timeout=.25, idempotency_key=request_key)
        assert entered.is_set()
    finally:
        release.set()
    first, = replies
    assert first == expected
    assert session.call("POST", verb, body=body, idempotency_key=request_key) == first
    status, _, response = call(
        srv, f"/v1/{verb}", method="POST", body={**body, **different_fields},
        token=w.tok(actor), idempotency_key=request_key)
    assert status == 409
    assert jbody(response)["error"] == "conflict"
    assert "idempotency key" in jbody(response)["msg"]
    rows = w.conn.execute("SELECT COUNT(*) FROM events WHERE type=? AND key=?",
                          (event, "demo:A")).fetchone()
    assert rows[0] == 1
    if verb == "claim":
        item = w.store._item("demo:A")
        assert (item["state"], item["claimed_by"], item["epoch"], item["lease_until"]) == (
            "claimed", actor, first["epoch"], first["lease_until"])


def test_claim_retry_replays_receipt_after_project_grants_change(srv, w, monkeypatch):
    token = w.tok("workerA")
    body = {"key": "demo:A", "lease_s": 600}
    status, _, first = call(srv, "/v1/claim", method="POST", body=body, token=token,
                            idempotency_key="same-claim")
    assert status == 200
    assert jbody(first)["epoch"] == 1

    authenticate = auth.authenticate

    def authenticate_with_new_grants(conn, bearer):
        return replace(authenticate(conn, bearer), projects=["other"])

    monkeypatch.setattr(auth, "authenticate", authenticate_with_new_grants)

    status, _, replayed = call(srv, "/v1/claim", method="POST", body=body, token=token,
                               idempotency_key="same-claim")
    assert status == 200, replayed
    assert jbody(replayed) == jbody(first)

    status, _, refused = call(srv, "/v1/claim", method="POST", body=body, token=token,
                              idempotency_key="new-claim")
    assert status == 403
    assert jbody(refused)["error"] == "forbidden"
    with srv.lock:
        claims = w.conn.execute("SELECT COUNT(*) FROM events WHERE type='claimed' AND key=?",
                                ("demo:A",)).fetchone()[0]
    assert claims == 1


def test_slow_snapshot_publication_does_not_block_reads(srv, w, monkeypatch):
    started, release = threading.Event(), threading.Event()
    atomic_write = snapshot_io._atomic_write

    def slow_write(path, body):
        started.set()
        assert release.wait(5)
        atomic_write(path, body)

    monkeypatch.setattr(snapshot_io, "_atomic_write", slow_write)
    replies = []
    writer = threading.Thread(target=lambda: replies.append(call(
        srv, "/v1/set_priority", method="POST", body={"key": "demo:A", "n": 7},
        token=w.tok("router"))))
    writer.start()
    reader = Session(url=srv.base_url)
    monkeypatch.setattr(reader, "token", lambda: w.tok("reader"))
    try:
        assert started.wait(3)
        assert reader.call("GET", "show", {"key": "demo:A"}, timeout=.25)["priority"] == 7
    finally:
        release.set()
        writer.join(5)
    assert replies[0][0] == 200


@pytest.mark.parametrize("brief", [None, 1])
def test_show_loads_only_the_item_neighborhood(srv, w, monkeypatch, brief):
    router = w.p("router")
    description = "Why: Verify the dependency.\nHow: Run the fixture."
    w.store.edit_definition(router, "demo:B", {"description": f"Full context.\n{description}"})
    to_done(w, "demo:A")
    to_done(w, "demo:B")
    w.store.create_items(router, [
        defn("FIX-A", corrects="demo:A"),
        defn("FIX-B", corrects="demo:B"),
        defn("REPAIRS", ["FIX-A", "FIX-B"], kind="goal"),
    ])
    session = Session(url=srv.base_url)
    monkeypatch.setattr(session, "token", lambda: w.tok("reader"))

    def full_plan():
        raise AssertionError("show loaded the full plan")

    monkeypatch.setattr(w.store, "_all_items", full_plan)
    result = session.call("GET", "show", {"key": "demo:B", "brief": brief})
    assert result["depends_on"] == [{"key": "demo:A", "state": "done"}]
    assert result["key"] == "demo:B"
    assert result["title"] == "title B"
    assert result["state"] == "done"
    assert result["done_criteria"] == "criteria"
    if brief:
        assert result["description"] == description
        assert "unblocks" not in result and "evidence" not in result
    else:
        assert result["description"] == f"Full context.\n{description}"
        assert result["unblocks"] == ["demo:GOAL"]
        assert result["corrected_by"] == ["demo:FIX-B"]
        assert result["dep_states"] == [
            {"key": "demo:A", "state": "done", "reason": "corrected by demo:FIX-A"},
        ]


def test_oversize_body_is_413(srv, w):
    big = b"x" * (api.MAX_BODY + 1)
    status, _, raw = call(srv, "/v1/claim", method="POST", body=big,
                          token=w.tok("workerA"))
    assert status == 413
    assert jbody(raw)["error"] == "too_large"


def test_ready_is_compact_text_by_default(srv, w):
    status, ctype, raw = call(srv, "/v1/ready", token=w.tok("reader"))
    assert status == 200
    assert "text/plain" in ctype
    text = raw.decode()
    assert "demo:A" in text and "demo:B" not in text
    assert "{" not in text


def test_ready_json_when_fmt_json(srv, w):
    status, ctype, raw = call(srv, "/v1/ready?fmt=json", token=w.tok("reader"))
    assert status == 200
    assert "application/json" in ctype
    keys = [it["key"] for it in jbody(raw)["items"]]
    assert keys == ["demo:A"]


def test_show_text_and_json(srv, w):
    _, ctype, raw = call(srv, "/v1/show?key=demo:A", token=w.tok("reader"))
    assert "text/plain" in ctype
    _, ctype, raw = call(srv, "/v1/show?key=demo:A&fmt=json",
                         token=w.tok("reader"))
    assert "application/json" in ctype
    assert jbody(raw)["key"] == "demo:A"


@pytest.mark.parametrize("failure", ["driver setup", "database open"])
def test_committed_read_connection_failure_returns_503(srv, w, monkeypatch, capsys, failure):
    from plangraph.keeper import db_errors

    def unavailable():
        error = RuntimeError if failure == "driver setup" else db_errors.OPERATIONAL[0]
        raise error("private connection detail")

    monkeypatch.setattr(srv, "read_store", unavailable)
    status, _, raw = call(srv, "/v1/show/demo:A", token=w.tok("reader"))
    assert status == 503
    assert jbody(raw) == {"error": "unavailable", "msg": "keeper read connection unavailable"}
    assert call(srv, "/v1/health")[0] == 200
    log = capsys.readouterr().err
    assert log.count("keeper: request failed:") == 1
    assert "private connection detail" not in log


def test_unexpected_exception_is_500_without_internals(srv, w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SECRET-INTERNAL-DETAIL")
    monkeypatch.setattr(w.store, "waves", boom)
    status, _, raw = call(srv, "/v1/waves", token=w.tok("reader"))
    assert status == 500
    text = raw.decode()
    assert "SECRET-INTERNAL-DETAIL" not in text
    assert jbody(raw)["error"] == "internal"


def test_snapshot_rewritten_after_write(srv, w, tmp_path):
    snap = tmp_path / "plan.snapshot.json"
    assert not snap.exists()
    call(srv, "/v1/claim", method="POST", body={"key": "demo:A"},
         token=w.tok("l1main"))
    srv.flush_snapshot()          # KEEPER-SNAPSHOT-PG: the file is rebuilt after writes go quiet
    assert snap.exists()
    data = json.loads(snap.read_text())
    assert data["head"] and {it["key"] for it in data["items"]} >= {"demo:A"}
    # atomic: no temp leftovers
    assert not list(tmp_path.glob("*.tmp*"))


def test_server_start_stop_loop_is_clean(w, tmp_path):
    """Regression: ``Reaper`` shadowed ``threading.Thread._stop``.

    Starting and stopping 20 servers must never raise from the reaper's
    ``join``; before the rename to ``_halt`` this was ``TypeError: 'Event'
    object is not callable`` on every stopped thread.
    """
    cfg = make_cfg(tmp_path, w)
    for _ in range(20):
        server = api.make_server(w.store, cfg, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_auth_failure_is_audited_never_token(srv, w, monkeypatch):
    """A rejected token is audited by reason only, and the token never leaks."""
    calls = []
    monkeypatch.setattr(w.store, "audit_auth_failure",
                        lambda reason: calls.append(reason), raising=False)
    token = "not-a-real-token"
    status, _, _ = call(srv, "/v1/whoami", token=token)
    assert status == 401
    assert calls == ["unknown or revoked token"]
    status, _, _ = call(srv, "/v1/whoami")  # missing Authorization header
    assert status == 401
    assert calls[-1] == "missing token"
    assert all(token not in reason for reason in calls)


def test_ready_json_separates_reconciliation_rows(srv, w, monkeypatch):
    from plangraph.keeper import context_sources
    monkeypatch.setattr(context_sources, "work_index", lambda *a, **kw: {
        "commits": {"demo:A": [{"sha": "a" * 40, "date": "2026-11-13",
                                  "subject": "done"}]},
        "prs": {}, "branches": []})
    status, _, raw = call(srv, "/v1/ready?fmt=json", token=w.tok("reader"))
    data = jbody(raw)
    assert status == 200 and data["items"] == [] and data["in_progress"] == []
    assert [r["key"] for r in data["needs_reconcile"]] == ["demo:A"]
    assert "already merged" in data["needs_reconcile"][0]["reason"]


def test_ready_unknown_index_warning_is_visible_in_json_and_text(srv, w, monkeypatch):
    from plangraph.keeper import context_sources
    import time
    monkeypatch.setattr(context_sources, "work_index", lambda *a, **kw: {
        "commits": {}, "prs": None, "branches": [], "stale": True,
        "origin_stale": True, "verified_at": time.time() - 60,
        "reason": "origin fetch failed or timed out"})
    status, _, raw = call(srv, "/v1/ready?fmt=json&n=0", token=w.tok("reader"))
    data = jbody(raw)
    assert status == 200 and data["items"] == []
    assert [r["key"] for r in data["unknown"]] == ["demo:A"]
    assert len(data["warnings"]) == 1
    assert data["warnings"][0]["age_seconds"] >= 60
    assert data["warnings"][0]["reason"] == "origin fetch failed or timed out"
    status, _, raw = call(srv, "/v1/ready?fmt=text&n=0", token=w.tok("reader"))
    assert status == 200
    text = raw.decode()
    assert text.count("WARNING:") == 1 and "origin fetch failed" in text
    assert "Unknown:\n  demo:A" in text


def test_incident_log_across_item_states(srv, w):
    from plangraph.keeper import cli

    key = "demo:A"
    l1 = w.p("l1main")
    token = w.tok("l1main")

    def append(text, who=token, **fields):
        return call(srv, "/v1/incident", method="POST",
                    body={"key": key, "text": text, **fields}, token=who)

    w.store.put_card(w.p("router"), key, {"outcome": "finish the work"})
    status, _, raw = append("initial report", evidence="https://example.org/report")
    assert status == 200, raw
    assert append("wrong project", w.tok("l1other"))[0] == 403
    assert append("worker", w.tok("workerA"))[0] == 403
    epoch = w.store.claim(l1, key)["epoch"]
    w.clock.advance(1)
    assert append("claimed report")[0] == 200
    card = w.store.get_card(l1, key)
    assert [entry["text"] for entry in card["incidents"]] == [
        "initial report", "claimed report"]
    with pytest.raises(api.Refused, match="claimed, not open"):
        w.store.edit_definition(l1, key, {"title": "new title"})
    w.store.add_evidence(l1, key, epoch, "decision", {"id": "D-123"})
    w.store.submit(l1, key, epoch)
    w.clock.advance(1)
    assert append("review report")[0] == 200
    assert append("router report", w.tok("router"))[0] == 200
    w.store.verdict(w.p("reviewer"), key, True)
    status, _, raw = append("too late")
    assert status >= 400
    assert "done" in jbody(raw)["msg"]

    status, _, raw = call(srv, "/v1/show?key=demo:A", token=token)
    assert status == 200, raw
    assert "INCIDENT 2023-11-14T22:13:21Z l1main claimed report" in raw.decode()
    status, _, raw = call(srv, "/v1/snapshot?fmt=text", token=token)
    assert status == 200, raw
    assert "demo:A INCIDENT 2023-11-14T22:13:22Z l1main review report" in raw.decode()
    item = next(i for i in w.store.snapshot(l1)["items"] if i["key"] == key)
    assert [entry["text"] for entry in item["incidents"]] == [
        "initial report", "claimed report", "review report", "router report"]
    assert item["incidents"][0]["evidence"] == "https://example.org/report"
    args = cli.build_parser().parse_args(["incident", key, "--text", "new report"])
    assert (args.key, args.text, args.evidence) == (key, "new report", None)
    assert w.store.verify_integrity() == []


def test_incident_migration_on_existing_sqlite_store(tmp_path):
    import sqlite3

    from plangraph.keeper import db

    path = tmp_path / "existing.db"
    conn = db.open_db(path, create=True, backend="sqlite")
    conn.close()
    with sqlite3.connect(path) as old:
        old.execute("DROP INDEX idx_events_incidents")
    conn = db.open_db(path, backend="sqlite")
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_events_incidents'"
        ).fetchone()[0] == "idx_events_incidents"
    finally:
        conn.close()


def _claim_checkouts(tmp_path, names):
    from subprocess import run
    from plangraph.keeper.evidence_git import GitAdapter

    repos = {name: tmp_path / name for name in names}
    for repo in repos.values():
        repo.mkdir()
        run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
             "commit", "--allow-empty", "-qm", "base"], cwd=repo, check=True)
    return GitAdapter({name: str(repo) for name, repo in repos.items()})


@pytest.mark.parametrize("failure", ["refusal", "timeout"])
def test_parallel_refresh_refuses_if_any_requested_work_index_fails(
        w, tmp_path, monkeypatch, failure):
    """A refresh of several repositories at once fails whole if one repository fails.

    The claim guard asks for one repository per item; a caller that asks for several
    gets them in parallel under the one preflight deadline.
    """
    from types import SimpleNamespace
    from keeper_util import Clock
    from plangraph.keeper import context_sources, timing
    from plangraph.keeper.errors import Refused

    git = _claim_checkouts(tmp_path, ("demo", "related"))
    w.store.ctx = {"git": git}
    clock = Clock(0)
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=clock))
    slow_started, good_finished = threading.Event(), threading.Event()
    read_index = context_sources.work_index
    share = timing.share_inflight
    step = "work index for related"

    def read(git, project, **kwargs):
        if project == "demo":
            assert slow_started.wait(8)
            return read_index(git, project, **kwargs)
        slow_started.set()
        assert good_finished.wait(8)
        if failure == "refusal":
            raise Refused("conflict", f"{step}: refresh refused")
        clock.advance(w.store.claim_preflight_s + 1)
        return read_index(git, project, **kwargs)

    def observe_share(key, fetch, **kwargs):
        result = share(key, fetch, **kwargs)
        if key == ("work", id(git), "demo"):
            assert result is not None and not result["stale"]
            good_finished.set()
        return result

    monkeypatch.setattr(context_sources, "work_index", read)
    monkeypatch.setattr(timing, "share_inflight", observe_share)
    message = (f"{step}: refresh refused" if failure == "refusal"
               else f"claim preflight exceeded its deadline during {step}")
    with timing.io_budget(w.store.claim_preflight_s):
        with pytest.raises(Refused) as refused:
            w.store._work_indexes({"demo", "related"}, fresh=True, shared={})
    assert (refused.value.code, refused.value.msg) == ("conflict", message)
    assert good_finished.is_set()


def test_parallel_claims_and_reads_stay_within_client_bound(srv, w, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from time import perf_counter, sleep
    from plangraph.keeper import context_sources, timing

    names = ("demo", *(f"related-{i}" for i in range(4)))
    w.store.ctx = {"git": _claim_checkouts(tmp_path, names)}

    ids = [f"LOAD-{i}" for i in range(16)]
    w.store.create_items(w.p("router"), [*(defn(key) for key in ids),
                                           defn("LOAD-GOAL", ids, kind="goal")])
    keys = [f"demo:{key}" for key in ids]
    original = context_sources.work_index
    phases = {name: [] for name in ("index", "dependency", "transaction", "event", "snapshot")}

    def measure(name, fn):
        def run(*args, **kwargs):
            start = perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                phases[name].append(perf_counter() - start)
        return run

    index_lock = threading.Lock()
    active_indexes = peak_indexes = 0

    def work_index(git, project, **kwargs):
        nonlocal active_indexes, peak_indexes
        assert 0 < timing.io_timeout(60) <= w.store.claim_preflight_s
        with index_lock:
            active_indexes += 1
            peak_indexes = max(peak_indexes, active_indexes)
        try:
            sleep(.3)  # Keep real refreshes in flight while requests overlap.
            return original(git, project, **kwargs)
        finally:
            with index_lock:
                active_indexes -= 1

    @contextmanager
    def transaction():
        start = perf_counter()
        with write():
            yield
        phases["transaction"].append(perf_counter() - start)

    write = w.store._write
    monkeypatch.setattr(w.store, "_write", transaction)
    monkeypatch.setattr(w.store, "_claim_train_for", measure("dependency", w.store._claim_train_for))
    monkeypatch.setattr(w.store, "_append", measure("event", w.store._append))
    monkeypatch.setattr(context_sources, "work_index", measure("index", work_index))
    monkeypatch.setattr(api.KeeperHandler, "_snapshot", measure("snapshot", api.KeeperHandler._snapshot))
    barrier = threading.Barrier(12)

    def request(i):
        is_writer = i < 8
        session = Session(url=srv.base_url)
        session._token = w.tok("l1main" if is_writer else "reader")
        barrier.wait(timeout=10)
        start = perf_counter()
        if is_writer:
            assert session.call("POST", "claim", body={"key": keys[i]}, timeout=10)["key"] == keys[i]
        else:
            assert session.call("GET", f"show/{keys[i]}", timeout=10)["key"] == keys[i]
        return perf_counter() - start

    with ThreadPoolExecutor(max_workers=12) as pool:
        samples = sorted(pool.map(request, range(12)))
    print("HTTP p95", round(samples[-1], 3),
          "phase maxima", {name: round(max(values), 4) for name, values in phases.items()})
    assert samples[-1] < 5
    # Every claim is in the item's repository, which is refreshed once for all of them;
    # each other repository is built once and then served from its cache.
    assert len(phases["index"]) == len(names)
    assert peak_indexes <= 2


def test_batch_claim_and_heartbeat_commit_once_and_replay(srv, w, monkeypatch):
    ids = [f"BATCH-{i}" for i in range(100)]
    w.store.create_items(w.p("router"), [*(defn(key) for key in ids),
                                           defn("BATCH-GOAL", ids, kind="goal")])
    keys = [f"demo:{key}" for key in ids]
    commits = []
    from contextlib import contextmanager

    @contextmanager
    def transaction():
        with write():
            yield
        commits.append("commit")

    write = w.store._write
    monkeypatch.setattr(w.store, "_write", transaction)
    session = Session(url=srv.base_url)
    session._token = w.tok("l1main")

    request = {"keys": [*keys, "demo:missing"]}
    claimed = session.call("POST", "claim_batch", body=request, idempotency_key="claim-fixture")
    assert [row["key"] for row in claimed["results"]] == request["keys"]
    assert all(row["epoch"] == 1 for row in claimed["results"][:-1])
    assert claimed["results"][-1]["error"] == "not_found"
    assert commits == ["commit"]
    assert session.call("POST", "claim_batch", body=request,
                        idempotency_key="claim-fixture") == claimed
    assert commits == ["commit"]

    w.clock.advance(60)
    claims = [{"key": key, "epoch": 1} for key in keys]
    claims[-1]["epoch"] = 2
    renewed = session.call("POST", "heartbeat_batch", body={"claims": claims})
    assert [row["key"] for row in renewed["results"]] == keys
    assert all(row["lease_until"] > claimed["results"][0]["lease_until"]
               for row in renewed["results"][:-1])
    assert renewed["results"][-1]["error"] == "epoch_stale"
    assert commits == ["commit", "commit"]


def _slow_claim_request(srv, w, monkeypatch, verb, budget=None):
    from types import SimpleNamespace
    from keeper_util import Clock
    from plangraph.keeper import context_sources, timing

    clock = Clock(0)
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=clock))
    if budget is not None:
        w.store.claim_preflight_s = budget

    def work_index(*args, **kwargs):
        clock.advance(4)
        return None

    monkeypatch.setattr(context_sources, "work_index", work_index)
    body = {"key": "demo:A"} if verb == "claim" else {"keys": ["demo:A"]}
    status, _, raw = call(srv, f"/v1/{verb}", method="POST", body=body,
                          token=w.tok("l1main"))
    return status, jbody(raw)


@pytest.mark.parametrize("verb", ["claim", "claim_batch"])
def test_claim_preflight_deadline_refuses_without_claiming(srv, w, monkeypatch, verb):
    status, body = _slow_claim_request(srv, w, monkeypatch, verb, budget=3)
    result = {"error": "conflict", "msg": "claim preflight exceeded its deadline during work index for demo"}
    if verb == "claim_batch":
        assert (status, body) == (200, {"results": [{"key": "demo:A", **result}]})
    else:
        assert (status, body) == (409, result)
    assert w.store._item("demo:A")["state"] == "open"
    assert not w.conn.execute("SELECT 1 FROM events WHERE type='claimed'").fetchone()


@pytest.mark.parametrize("verb", ["claim", "claim_batch"])
def test_four_second_claim_preflight_succeeds(srv, w, monkeypatch, verb):
    status, body = _slow_claim_request(srv, w, monkeypatch, verb)
    assert status == 200
    row = body["results"][0] if verb == "claim_batch" else body
    assert row["epoch"] == 1
    assert w.store._item("demo:A")["state"] == "claimed"


@pytest.mark.parametrize("verb", ["claim_batch", "heartbeat_batch"])
@pytest.mark.parametrize("keys", [["demo:A", "demo:A"], [f"demo:KEY-{i}" for i in range(201)]],
                         ids=["duplicate", "over-limit"])
def test_lease_batch_validates_keys_before_writing(srv, w, monkeypatch, verb, keys):
    def must_not_write():
        pytest.fail("invalid batch entered a write transaction")

    monkeypatch.setattr(w.store, "_write", must_not_write)
    body = ({"keys": keys} if verb == "claim_batch"
            else {"claims": [{"key": key, "epoch": 1} for key in keys]})
    status, _, raw = call(srv, f"/v1/{verb}", method="POST", body=body,
                          token=w.tok("l1main"))
    assert (status, jbody(raw)) == (400, {
        "error": "validation", "msg": "expected 1 to 200 distinct item keys"})


def test_claim_rechecks_item_after_waiting_on_authority(srv, w, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from plangraph.keeper import context_sources

    started, finish = threading.Event(), threading.Event()

    def work_index(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return None

    monkeypatch.setattr(context_sources, "work_index", work_index)
    with ThreadPoolExecutor(max_workers=1) as pool:
        claim = pool.submit(call, srv, "/v1/claim", method="POST",
                            body={"key": "demo:A"}, token=w.tok("l1main"))
        try:
            assert started.wait(5)
            with srv.lock:
                w.store.set_priority(w.p("router"), "demo:A", 9)
        finally:
            finish.set()
        status, _, raw = claim.result(timeout=5)
    assert (status, jbody(raw)) == (409, {
        "error": "conflict", "msg": "demo:A changed while checking claim eligibility"})
    assert w.store._item("demo:A")["state"] == "open"
    assert not w.conn.execute("SELECT 1 FROM events WHERE type='claimed'").fetchone()

