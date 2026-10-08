"""Keeper end-to-end: real HTTP daemon, MCP stdio, CLI snapshot fallback.

The DB is a temp keeper built from ``tests/fixtures/import_mini`` through
``importer.load_dir`` + ``Store.import_items`` under a router principal, then
driven through the real threaded API on 127.0.0.1:0.  Nothing here leaves
loopback and no real verifier runs (``decision`` is a fake).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from keeper_util import Clock, FakeVerifier
from plangraph.keeper import api, auth, cli, config, db as kdb, importer
from plangraph.keeper.store import Store

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURE = ROOT / "tests" / "fixtures" / "import_mini"
OUT = ROOT / "tests" / "out"

KEY = "routing:R1"
PRINCIPALS = (
    ("router", "router", [], None),
    ("l1", "l1", ["routing", "autoos"], "teamA"),
    ("worker", "worker", ["routing", "autoos"], "teamB"),
    ("reviewer", "reviewer", ["routing", "autoos"], "teamC"),  # other family
    ("reviewer2", "reviewer", ["routing", "autoos"], "teamR"),  # D-219 seat 2
)
EVIDENCE = ("decision", "commit+ci", "run-record", "semaphore-task")


class E2E:
    """A temp keeper DB, principals and a live API server on port 0."""

    def __init__(self, tmp_path, *, with_sizes=False):
        self.tmp = Path(tmp_path)
        self.cfg_path = self.tmp / "keeper.json"
        self.snapshot = self.tmp / "plan.snapshot.json"
        self.db = self.tmp / "keeper.db"
        self.cfg_path.write_text(json.dumps({
            "db": str(self.db), "snapshot": str(self.snapshot),
            "bind": "127.0.0.1:0", "reap_interval": 3600,
        }), encoding="utf-8")
        os.chmod(self.cfg_path, 0o600)
        self.cfg = config.load_config(self.cfg_path)
        self.conn = kdb.open_db(self.db, create=True)
        self.clock = Clock()
        self.verifiers = {kind: FakeVerifier() for kind in EVIDENCE}
        self.store = Store(self.conn, clock=self.clock, verifiers=self.verifiers)
        self.tokens, self.principals = {}, {}
        for name, role, projects, family in PRINCIPALS:
            token = auth.add_principal(self.conn, name, role, projects,
                                       family=family)
            self.tokens[name] = token
            self.principals[name] = auth.authenticate(self.conn, token)
        self.store.import_items(self.principals["router"],
                                importer.load_dir(FIXTURE))
        if with_sizes:
            self._add_size_items()
        self.server = self.thread = None
        self.base = ""

    def _add_size_items(self):
        """Ten more ready lanes so ``ready`` has a full ten-item page."""
        items = [{"project": "size", "id": f"S{i}", "title": f"size lane {i}",
                  "kind": "lane", "evidence_kinds": ["any"], "operator": False}
                 for i in range(1, 11)]
        items.append({"project": "size", "id": "SGOAL", "kind": "goal",
                      "depends_on": [f"S{i}" for i in range(1, 11)],
                      "evidence_kinds": ["any"], "operator": False})
        self.store.create_items(self.principals["router"], items)

    def start(self):
        self.server = api.make_server(self.store, self.cfg, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def close(self):
        self.stop()
        self.conn.close()

    def token_file(self, name):
        path = self.tmp / f"{name}.token"
        path.write_text(self.tokens[name], encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def head(self):
        return json.loads(self.snapshot.read_text(encoding="utf-8"))["head"]


def call(base, method, path, *, token=None, body=None):
    """One HTTP request; returns ``(status, decoded_json)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def run_cli(argv):
    """Run the ``pg`` CLI in-process; returns ``(code, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def mcp_lines(e2e, requests):
    """Drive ``python -m plangraph.keeper.mcp`` over pipes; return stdout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PG_URL"] = e2e.base
    env["PG_TOKEN_FILE"] = str(e2e.token_file("router"))
    payload = "".join(json.dumps(line) + "\n" for line in requests)
    proc = subprocess.run([sys.executable, "-m", "plangraph.keeper.mcp"],
                          input=payload, capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# --------------------------------------------------------------------------- #
# (a) concurrent claim
# --------------------------------------------------------------------------- #

def test_claim_race_over_http(tmp_path):
    e2e = E2E(tmp_path).start()
    try:
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(3)

        def attempt():
            barrier.wait()
            outcome = call(e2e.base, "POST", "/v1/claim",
                           token=e2e.tokens["worker"], body={"key": KEY})
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        winners = [r for r in results if r[0] == 200]
        losers = [r for r in results if 400 <= r[0] < 500]
        assert len(results) == 3
        assert len(winners) == 1, results
        assert len(losers) == 2, results
        assert all(body["error"] == "conflict" for _, body in losers)
        item = e2e.store._item(KEY)
        assert item["state"] == "claimed" and item["epoch"] == 1
    finally:
        e2e.close()


# --------------------------------------------------------------------------- #
# (b) + (c): lifecycle, DONE lockout, snapshot rewrite
# --------------------------------------------------------------------------- #

def test_full_lifecycle_done_is_locked_and_audited(tmp_path):
    e2e = E2E(tmp_path).start()
    try:
        l1, worker, reviewer = (e2e.tokens["l1"], e2e.tokens["worker"],
                                e2e.tokens["reviewer"])
        reviewer2 = e2e.tokens["reviewer2"]
        # D-228: this test walks the two-reviewer path, so R1 is high-tier.
        e2e.store.edit_definition(e2e.principals["router"], KEY,
                                  {"review_tier": "high"})
        status, body = call(e2e.base, "POST", "/v1/claim", token=l1,
                            body={"key": KEY})
        assert status == 200, body
        epoch = body["epoch"]
        heads = [e2e.head()]
        for path, token, payload in (
                ("/v1/delegate", l1, {"key": KEY, "worker": "worker"}),
                ("/v1/add_evidence", l1, {"key": KEY, "epoch": epoch,
                                          "kind": "decision", "ref": {"id": "D-1"}}),
                ("/v1/submit", l1, {"key": KEY, "epoch": epoch}),
                ("/v1/verdict", reviewer,
                 {"key": KEY, "ok": True, "reason": "looks good"}),
                ("/v1/verdict", reviewer2,
                 {"key": KEY, "ok": True, "reason": "agreed"})):
            status, body = call(e2e.base, "POST", path, token=token, body=payload)
            assert status == 200, (path, body)
            json.loads(e2e.snapshot.read_text(encoding="utf-8"))  # always valid
            heads.append(e2e.head())
        assert heads[-1] and len(set(heads)) == len(heads)  # rewritten each write

        status, body = call(e2e.base, "GET", "/v1/show?key=" + KEY + "&fmt=json",
                            token=l1)
        assert body["state"] == "done" and body["reviewer"] == "reviewer2"

        router = e2e.tokens["router"]
        attempts = [
            ("/v1/create_items", router, {"items": [{"project": "routing",
                                                     "id": "R1"}]}),
            ("/v1/import_items", router, {"items": [{"project": "routing",
                                                     "id": "R1"}]}),
            ("/v1/edit_definition", router,
             {"key": KEY, "fields": {"title": "changed"}}),
            ("/v1/set_priority", router, {"key": KEY, "n": 3}),
            ("/v1/claim", l1, {"key": KEY}),
            ("/v1/delegate", l1, {"key": KEY, "worker": "worker"}),
            ("/v1/heartbeat", l1, {"key": KEY, "epoch": epoch}),
            ("/v1/release", l1, {"key": KEY, "epoch": epoch}),
            ("/v1/add_evidence", l1, {"key": KEY, "epoch": epoch,
                                      "kind": "decision", "ref": {"id": "D-2"}}),
            ("/v1/add_evidence_router", router, {"key": KEY, "kind": "decision",
                                                 "ref": {"id": "D-3"}}),
            ("/v1/submit", l1, {"key": KEY, "epoch": epoch}),
            ("/v1/verdict", reviewer, {"key": KEY, "ok": True}),
            ("/v1/cancel", router, {"key": KEY, "reason": "no"}),
        ]
        for path, token, payload in attempts:
            status, body = call(e2e.base, "POST", path, token=token, body=payload)
            assert 400 <= status < 500, (path, status, body)
            assert body.get("error") == "locked", (path, body)

        rows = e2e.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE ok=0 AND code='locked'"
        ).fetchone()[0]
        assert rows >= len(attempts), rows
        keyed = e2e.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE key=? AND ok=0"
            " AND code='locked'", (KEY,)).fetchone()[0]
        assert keyed >= len(attempts) - 2  # create/import audit key=None
        assert e2e.head() == heads[-1]  # refusals append no events
    finally:
        e2e.close()


# --------------------------------------------------------------------------- #
# (c) offline fallback
# --------------------------------------------------------------------------- #

def test_cli_snapshot_fallback_read_only(tmp_path, monkeypatch):
    e2e = E2E(tmp_path).start()
    try:
        status, _ = call(e2e.base, "POST", "/v1/set_priority",
                         token=e2e.tokens["router"], body={"key": KEY, "n": 1})
        assert status == 200 and e2e.snapshot.is_file()
        e2e.stop()
        monkeypatch.setenv("PG_URL", e2e.base)
        monkeypatch.setenv("PG_CONFIG", str(e2e.cfg_path))
        monkeypatch.setenv("PG_TOKEN_FILE", str(e2e.token_file("l1")))
        code, out, err = run_cli(["ready", "--brief"])
        assert code == 0
        assert "READ-ONLY fallback (keeper unreachable)" in err
        assert KEY in out  # answered from plan.snapshot.json, not the API
        code, out, err = run_cli(["claim", "routing:R2"])
        assert code == 3 and "unreachable" in err
    finally:
        e2e.close()


# --------------------------------------------------------------------------- #
# (d) MCP stdio against the live API
# --------------------------------------------------------------------------- #

def test_mcp_stdio_against_live_api(tmp_path):
    e2e = E2E(tmp_path).start()
    try:
        out = mcp_lines(e2e, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "ready", "arguments": {"n": 50}}},
        ])
        listing, ready = [json.loads(line) for line in out.splitlines() if line]
        assert len(listing["result"]["tools"]) == 22
        assert ready["result"]["isError"] is False
        assert KEY in ready["result"]["content"][0]["text"]
        assert e2e.tokens["router"] not in out
    finally:
        e2e.close()


# --------------------------------------------------------------------------- #
# (e) response sizes
# --------------------------------------------------------------------------- #

def _estimate(label, text):
    raw = text.encode("utf-8")
    words = len(text.split())
    return (f"{label}: {len(raw)} bytes | {words} words | "
            f"~{words / 4:.1f} tokens (words/4)")


def test_record_response_sizes(tmp_path, monkeypatch):
    e2e = E2E(tmp_path, with_sizes=True).start()
    try:
        monkeypatch.setenv("PG_URL", e2e.base)
        monkeypatch.setenv("PG_CONFIG", str(e2e.cfg_path))
        monkeypatch.setenv("PG_TOKEN_FILE", str(e2e.token_file("l1")))
        code, ready, _ = run_cli(["ready", "--brief"])
        assert code == 0 and len(ready.strip().splitlines()) == 10
        code, show, _ = run_cli(["show", KEY])
        assert code == 0 and show.strip()
        mcp = mcp_lines(e2e, [{"jsonrpc": "2.0", "id": 1,
                               "method": "tools/list"}]).rstrip("\n")
        assert json.loads(mcp)["result"]["tools"]

        report = "\n".join([
            _estimate("pg ready --brief (10 items)", ready),
            _estimate("pg show " + KEY, show),
            _estimate("MCP tools/list (full)", mcp),
        ]) + "\n"
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "sizes.txt").write_text(report, encoding="utf-8")
        print(report, end="", file=sys.__stdout__)
    finally:
        e2e.close()
