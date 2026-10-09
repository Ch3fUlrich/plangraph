"""PG-KG-EXPORT (D-879): render-time, read-only Omnigraph export. Hermetic (D-852): fake transport, loopback stub."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plangraph import kg_export as kg

TOKEN = "".join(["fake", "-", "bearer", "-", "value", "-", "42"])


def _rows(graph_data, name):
    return graph_data.get(name, [])


class Fake:
    """transport(graph, name, text, params) -> rows, driven by {graph: {query_name: rows}}."""

    def __init__(self, data, fail=()):
        self.data, self.fail, self.calls = data, set(fail), []

    def __call__(self, graph, name, text, params=None):
        self.calls.append((graph, name, text))
        if graph in self.fail:
            raise kg.KgError(f"graph {graph}: boom")
        if graph not in self.data:
            raise kg.KgError(f"graph {graph}: HTTP 404", missing=True)
        return list(_rows(self.data[graph], name))


def item(key, refs, project="p1"):
    return {"key": key, "id": key.split(":")[-1], "project": project, "kg_refs": refs}


GRAPH = {
    "kg_decision": [
        {"n.slug": "d-old", "n.title": "Old way", "n.status": "superseded", "n.rationale": "because\x00 old"},
        {"n.slug": "d-new", "n.title": "New way", "n.status": "accepted", "n.rationale": "x" * 900},
        {"n.slug": "d-other", "n.title": "Other", "n.status": "accepted", "n.rationale": None},
    ],
    "kg_rule": [{"n.slug": "r-1", "n.statement": "Never do X", "n.severity": "must"}],
    "kg_component": [{"n.slug": "c-1", "n.name": "Keeper", "n.kind": "service"}],
    "kg_convention": [{"n.slug": "cv-1", "n.name": "Naming", "n.example": "e"}],
    "kg_preference": [],
    "kg_project": [{"n.slug": "p1", "n.name": "Project one"}],
    "kg_supersedes": [{"a.slug": "d-new", "b.slug": "d-old"}],
    "kg_decided_in": [{"n.slug": "d-new", "p.slug": "p1"}, {"n.slug": "d-old", "p.slug": "p1"}],
    "kg_constrains_project": [{"n.slug": "r-1", "p.slug": "p1"}],
    "kg_part_of": [{"n.slug": "c-1", "p.slug": "p1"}],
    "kg_applies_to": [],
}


def build(items, data=None, **kw):
    return kg.build_export(items, Fake({"g": GRAPH} if data is None else data), **kw)


def test_resolves_supersedes_to_current():
    exp, dangling, _ = build([item("p1:A", ["g:Decision:d-old"])])
    node = exp["nodes"]["g:Decision:d-old"]
    assert node["superseded_by"] == "g:Decision:d-new"
    assert node["current"] == "g:Decision:d-new"
    assert node["status"] == "superseded"
    assert "g:Decision:d-new" in exp["nodes"]
    assert dangling == []


def test_supersedes_chain_and_cycle_guard():
    data = {"g": dict(GRAPH, kg_supersedes=[
        {"a.slug": "d-new", "b.slug": "d-old"}, {"a.slug": "d-other", "b.slug": "d-new"},
    ])}
    exp, _, _ = build([item("p1:A", ["g:Decision:d-old"])], data)
    assert exp["nodes"]["g:Decision:d-old"]["current"] == "g:Decision:d-other"
    cyc = {"g": dict(GRAPH, kg_supersedes=[
        {"a.slug": "d-new", "b.slug": "d-old"}, {"a.slug": "d-old", "b.slug": "d-new"}])}
    exp, _, _ = build([item("p1:A", ["g:Decision:d-old"])], cyc)  # must terminate
    assert exp["nodes"]["g:Decision:d-old"]["current"] in ("g:Decision:d-old", "g:Decision:d-new")


def test_dangling_ref_reported_with_items():
    exp, dangling, _ = build([item("p1:A", ["g:Decision:nope"]), item("p1:B", ["g:Decision:nope", "g:Rule:r-1"])])
    assert dangling == [{"ref": "g:Decision:nope", "items": ["p1:A", "p1:B"]}]
    assert "g:Decision:nope" not in exp["nodes"]
    assert exp["items"]["p1:A"] == ["g:Decision:nope"]  # kept so the UI can show "unresolved"


def test_missing_graph_is_unresolved_not_dangling():
    exp, dangling, errors = build([item("p1:A", ["zz:Decision:d"])])
    assert dangling == []
    assert errors and errors[0]["graph"] == "zz"
    assert exp["unresolved_graphs"] == ["zz"]


def test_reverse_index_and_project_knowledge():
    exp, _, _ = build([item("p1:A", ["g:Decision:d-old"]), item("p2:B", ["g:Decision:d-new"], "p2")],
                      extra_graphs=["g"])
    assert exp["reverse"]["g:Decision:d-old"] == ["p1:A"]
    assert exp["reverse"]["g:Decision:d-new"] == ["p1:A", "p2:B"]  # via supersession + direct
    p1 = set(exp["projects"]["p1"]["nodes"])
    assert {"g:Decision:d-new", "g:Rule:r-1", "g:Component:c-1"} <= p1
    assert "g:Decision:d-other" not in p1  # other project hub: none -> not in p1


def test_node_fields_trimmed_and_clean():
    exp, _, _ = build([item("p1:A", ["g:Decision:d-old", "g:Decision:d-new"])])
    old, new = exp["nodes"]["g:Decision:d-old"], exp["nodes"]["g:Decision:d-new"]
    assert "\x00" not in json.dumps(old)
    assert len(new["excerpt"]) <= kg.EXCERPT_MAX
    assert set(old) >= {"graph", "type", "slug", "title", "status", "project", "superseded_by", "current"}
    assert "embedding" not in json.dumps(exp)
    assert exp["nodes"]["g:Decision:d-old"]["project"] == "p1"


def test_non_decision_types_and_reference_only_conventions():
    exp, _, _ = build([item("p1:A", ["g:Rule:r-1", "g:Convention:cv-1", "g:Component:c-1", "g:Project:p1"])])
    assert exp["nodes"]["g:Rule:r-1"]["title"] == "Never do X"
    assert exp["nodes"]["g:Rule:r-1"]["severity"] == "must"
    assert exp["nodes"]["g:Component:c-1"]["title"] == "Keeper"
    assert exp["nodes"]["g:Convention:cv-1"]["title"] == "Naming"
    assert exp["nodes"]["g:Project:p1"]["title"] == "Project one"


def test_unreferenced_conventions_not_exported():
    exp, _, _ = build([item("p1:A", ["g:Rule:r-1"])])
    assert "g:Convention:cv-1" not in exp["nodes"]


def test_node_cap_truncates_listing_but_keeps_referenced():
    rows = [{"n.slug": f"d{i}", "n.title": f"t{i}", "n.status": "accepted", "n.rationale": None} for i in range(50)]
    data = {"g": dict(GRAPH, kg_decision=rows, kg_supersedes=[], kg_decided_in=[])}
    exp, _, _ = build([item("p1:A", ["g:Decision:d49"])], data, extra_graphs=["g"], max_listed=10)
    assert exp["truncated"] is True
    assert "g:Decision:d49" in exp["nodes"]
    assert sum(1 for n in exp["nodes"].values() if n["type"] == "Decision") <= 11


def test_malformed_refs_and_rows_ignored():
    data = {"g": dict(GRAPH, kg_decision=[{"n.slug": None}, "junk", {"n.slug": "d-ok", "n.title": "ok", "n.status": "accepted"}])}
    exp, dangling, _ = build([item("p1:A", ["bad", 7, "g:Decision:d-ok"])], data)
    assert "g:Decision:d-ok" in exp["nodes"] and dangling == []


def test_html_in_text_stays_data():
    data = {"g": dict(GRAPH, kg_decision=[{"n.slug": "d-x", "n.title": "<img src=x onerror=alert(1)>", "n.status": "accepted", "n.rationale": "</script>"}])}
    exp, _, _ = build([item("p1:A", ["g:Decision:d-x"])], data)
    assert exp["nodes"]["g:Decision:d-x"]["title"] == "<img src=x onerror=alert(1)>"  # raw: UI uses textContent


# ---- run(): enabled switch, files, status -------------------------------------

def _env(tmp_path, **extra):
    tok = tmp_path / "tok"
    tok.write_text(TOKEN + "\n")
    tok.chmod(0o600)
    env = {"PG_KG_URL": "http://127.0.0.1:1", "PG_KG_TOKEN_FILE": str(tok)}
    env.update(extra)
    return env


def test_off_without_token_env(tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    res = kg.run([item("p1:A", ["g:Rule:r-1"])], out, env={}, transport=Fake({}))
    assert res == 0
    assert json.loads((out / "kg-export.json").read_text()) == {"enabled": False}
    assert not (out / "kg-export-status.json").exists() or json.loads((out / "kg-export-status.json").read_text())["enabled"] is False


def test_off_with_missing_or_empty_token_file(tmp_path):
    out = tmp_path
    for content in (None, ""):
        tok = tmp_path / "t"
        if tok.exists():
            tok.unlink()
        if content is not None:
            tok.write_text(content)
        assert kg.run([], out, env={"PG_KG_URL": "http://h", "PG_KG_TOKEN_FILE": str(tok)}, transport=Fake({})) == 0
        assert json.loads((out / "kg-export.json").read_text()) == {"enabled": False}


def test_off_when_no_url(tmp_path):
    env = _env(tmp_path)
    del env["PG_KG_URL"]
    assert kg.run([], tmp_path, env=env, transport=Fake({})) == 0
    assert json.loads((tmp_path / "kg-export.json").read_text()) == {"enabled": False}


def test_run_writes_export_and_status(tmp_path):
    env = _env(tmp_path, PG_KG_GRAPHS="g")
    assert kg.run([item("p1:A", ["g:Decision:d-old", "g:Decision:gone"])], tmp_path, env=env, transport=Fake({"g": GRAPH})) == 0
    exp = json.loads((tmp_path / "kg-export.json").read_text())
    assert exp["enabled"] is True and exp["nodes"]["g:Decision:d-old"]["current"] == "g:Decision:d-new"
    assert "generated" in exp
    st = json.loads((tmp_path / "kg-export-status.json").read_text())
    assert st["enabled"] is True and st["ok"] is True
    assert st["dangling"] == [{"ref": "g:Decision:gone", "items": ["p1:A"]}]
    assert st["nodes"] == len(exp["nodes"])


def test_status_path_override(tmp_path):
    sdir = tmp_path / "state"
    sdir.mkdir()
    env = _env(tmp_path, PG_KG_STATUS_FILE=str(sdir / "s.json"))
    kg.run([], tmp_path, env=env, transport=Fake({}))
    assert (sdir / "s.json").exists() and not (tmp_path / "kg-export-status.json").exists()


def test_unreachable_keeps_previous_export_and_exits_zero(tmp_path):
    env = _env(tmp_path)
    prev = {"enabled": True, "nodes": {"x": {}}}
    (tmp_path / "kg-export.json").write_text(json.dumps(prev))
    assert kg.run([item("p1:A", ["g:Rule:r-1"])], tmp_path, env=env, transport=Fake({"g": GRAPH}, fail=["g"])) == 0
    assert json.loads((tmp_path / "kg-export.json").read_text()) == prev
    st = json.loads((tmp_path / "kg-export-status.json").read_text())
    assert st["ok"] is False and st["errors"]
    assert TOKEN not in json.dumps(st)


def test_no_refs_no_graphs_no_calls(tmp_path):
    f = Fake({})
    assert kg.run([item("p1:A", [])], tmp_path, env=_env(tmp_path), transport=f) == 0
    assert f.calls == []
    assert json.loads((tmp_path / "kg-export.json").read_text())["enabled"] is True


def test_atomic_write_no_tmp_left(tmp_path):
    kg.run([], tmp_path, env=_env(tmp_path), transport=Fake({}))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "tok") == ["kg-export-status.json", "kg-export.json"]


def test_credential_shaped_text_masked(tmp_path):
    secret = "/".join(["user", "x" * 8])  # the D-374 guard shape
    word = "lo" + "gin"
    data = {"g": dict(GRAPH, kg_decision=[{"n.slug": "d-s", "n.title": f"{word} {secret}", "n.status": "accepted"}])}
    kg.run([item("p1:A", ["g:Decision:d-s"])], tmp_path, env=_env(tmp_path), transport=Fake(data))
    assert secret not in (tmp_path / "kg-export.json").read_text()


def test_queries_are_read_only():
    f = Fake({"g": GRAPH})
    kg.build_export([item("p1:A", ["g:Rule:r-1"])], f)
    assert f.calls
    for _g, _n, text in f.calls:
        assert not re.search(r"\b(insert|update|delete|mutation)\b", text, re.I)


# ---- HTTP client (loopback stub; no external network) --------------------------

class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _H.seen.append((self.path, self.headers.get("Authorization"), body))
        if _H.mode == "big":
            payload = b'{"rows":[' + b'{"a":"' + b"x" * 5000 + b'"},' * 1 + b'{"a":"y"}]}' + b" " * 0
            payload = payload + b" " * (kg.MAX_RESPONSE_BYTES + 10)
        elif _H.mode == "401":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif _H.mode == "slow":
            import time
            time.sleep(1.5)
            payload = b'{"rows":[]}'
        else:
            payload = json.dumps({"columns": ["n.slug"], "rows": [{"n.slug": "a"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def stub():
    _H.seen, _H.mode = [], "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_client_posts_graph_query_with_bearer(stub):
    c = kg.OmnigraphClient(stub, TOKEN, timeout=3)
    rows = c("my graph", "kg_x", "query kg_x() { match { $n: Rule } return { $n.slug } }", {"a": 1})
    assert rows == [{"n.slug": "a"}]
    path, auth, body = _H.seen[0]
    assert path == "/graphs/my%20graph/query" and auth == f"Bearer {TOKEN}"
    assert body["name"] == "kg_x" and body["params"] == {"a": 1} and body["branch"] == "main"


def test_client_errors_never_contain_token(stub):
    _H.mode = "401"
    c = kg.OmnigraphClient(stub, TOKEN, timeout=3)
    with pytest.raises(kg.KgError) as e:
        c("g", "kg_x", "q")
    assert TOKEN not in str(e.value) and "401" in str(e.value)


def test_client_response_cap(stub):
    _H.mode = "big"
    with pytest.raises(kg.KgError, match="too large"):
        kg.OmnigraphClient(stub, TOKEN, timeout=3)("g", "kg_x", "q")


def test_client_timeout_bounded(stub):
    _H.mode = "slow"
    with pytest.raises(kg.KgError):
        kg.OmnigraphClient(stub, TOKEN, timeout=0.3)("g", "kg_x", "q")


def test_client_refuses_non_http_url():
    with pytest.raises(kg.KgError):
        kg.OmnigraphClient("file://localhost/none", TOKEN)


def test_token_not_logged_on_run_failure(tmp_path, capsys):
    env = _env(tmp_path)
    kg.run([item("p1:A", ["g:Rule:r-1"])], tmp_path, env=env, transport=Fake({"g": GRAPH}, fail=["g"]))
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err


# ---- pg render wiring ---------------------------------------------------------

def _render_cli(tmp_path, monkeypatch, env):
    import sys as _sys
    from plangraph.keeper import cli_verbs
    snap = {"items": [item("p1:A", ["g:Decision:d-old"])], "generated": "x", "head": "h"}

    class S:
        def call(self, method, verb, params=None):
            return snap

    class A:
        out = str(tmp_path / "site" / "plan.html")
        cdn = True

    for k in ("PG_KG_URL", "PG_KG_TOKEN_FILE", "PG_KG_GRAPHS", "PG_KG_STATUS_FILE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return cli_verbs.cmd_render(A, S()), tmp_path / "site"


def test_render_cli_disabled_without_env(tmp_path, monkeypatch):
    rc, site = _render_cli(tmp_path, monkeypatch, {})
    assert rc == 0 and (site / "plan.html").exists()
    assert json.loads((site / "kg-export.json").read_text()) == {"enabled": False}


def test_render_cli_survives_unreachable_omnigraph(tmp_path, monkeypatch):
    tok = tmp_path / "t"
    tok.write_text(TOKEN)
    rc, site = _render_cli(tmp_path, monkeypatch, {"PG_KG_URL": "http://127.0.0.1:1", "PG_KG_TOKEN_FILE": str(tok)})
    assert rc == 0 and (site / "plan.html").exists()
    st = json.loads((site / "kg-export-status.json").read_text())
    assert st["ok"] is False and TOKEN not in json.dumps(st)


def test_render_cli_survives_exporter_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(kg, "build_export", lambda *a, **k: 1 / 0)
    tok = tmp_path / "t"
    tok.write_text(TOKEN)
    rc, site = _render_cli(tmp_path, monkeypatch, {"PG_KG_URL": "http://127.0.0.1:1", "PG_KG_TOKEN_FILE": str(tok)})
    assert rc == 0 and (site / "plan.html").exists()


# ---- review round 1 fixes ------------------------------------------------------

class _Redir(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        _Redir.hits.append((self.server.server_address[1], self.path, self.headers.get("Authorization")))
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.server.role == "first":
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{self.server.other}/graphs/g/query")
            self.send_header("Content-Length", "0")
        else:
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"rows":[]}')
            return
        self.end_headers()


def test_client_never_follows_redirects_or_forwards_token():
    _Redir.hits = []
    a, b = ThreadingHTTPServer(("127.0.0.1", 0), _Redir), ThreadingHTTPServer(("127.0.0.1", 0), _Redir)
    a.role, b.role, a.other = "first", "second", b.server_address[1]
    for s in (a, b):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with pytest.raises(kg.KgError) as e:
            kg.OmnigraphClient(f"http://127.0.0.1:{a.server_address[1]}", TOKEN, timeout=3)("g", "kg_x", "q")
        assert "307" in str(e.value) and TOKEN not in str(e.value)
        assert [h[0] for h in _Redir.hits] == [a.server_address[1]]  # the redirect target was never contacted
    finally:
        for s in (a, b):
            s.shutdown()
            s.server_close()


def test_deadline_checked_before_every_query():
    f = Fake({"g": GRAPH})
    exp, _d, errors = kg.build_export([item("p1:A", ["g:Rule:r-1"])], f, deadline=0)  # already past
    assert f.calls == [] and errors and "deadline" in errors[0]["error"]


def test_transient_failure_keeps_previous_export_but_missing_graph_does_not(tmp_path):
    env = _env(tmp_path)
    prev = {"enabled": True, "nodes": {"old": {}}}
    (tmp_path / "kg-export.json").write_text(json.dumps(prev))
    refs = [item("p1:A", ["g:Rule:r-1", "h:Rule:r-2"])]
    # g ok, h transient failure -> keep previous
    kg.run(refs, tmp_path, env=env, transport=Fake({"g": GRAPH, "h": GRAPH}, fail=["h"]))
    assert json.loads((tmp_path / "kg-export.json").read_text()) == prev
    # h absent (404) is not transient -> new export written, h unresolved
    kg.run(refs, tmp_path, env=env, transport=Fake({"g": GRAPH}))
    new = json.loads((tmp_path / "kg-export.json").read_text())
    assert "g:Rule:r-1" in new["nodes"] and new["unresolved_graphs"] == ["h"]


def test_node_in_several_projects_listed_under_each():
    data = {"g": dict(GRAPH, kg_constrains_project=[{"n.slug": "r-1", "p.slug": "p1"}, {"n.slug": "r-1", "p.slug": "p2"}])}
    exp, _d, _e = build([], data, extra_graphs=["g"])
    assert "g:Rule:r-1" in exp["projects"]["p1"]["nodes"] and "g:Rule:r-1" in exp["projects"]["p2"]["nodes"]
    assert exp["nodes"]["g:Rule:r-1"]["projects"] == ["p1", "p2"]


def test_single_fixed_tmp_name(tmp_path):
    kg.run([], tmp_path, env=_env(tmp_path), transport=Fake({}))
    kg.run([], tmp_path, env=_env(tmp_path), transport=Fake({}))
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_client_ignores_environment_proxy(stub, monkeypatch):
    other = _H.seen
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), _Redir)  # stands in for a proxy; any hit is a leak
    srv2.role = "second"
    _Redir.hits = []
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{srv2.server_address[1]}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{srv2.server_address[1]}")
    try:
        kg.OmnigraphClient(stub, TOKEN, timeout=3)("g", "kg_x", "q")
        assert _Redir.hits == [] and other
    finally:
        srv2.shutdown()
        srv2.server_close()


def test_non_list_kg_refs_do_not_abort_the_export():
    items = [{"key": "p1:A", "kg_refs": 7}, {"key": "p1:B", "kg_refs": "g:Rule:r-1"}, item("p1:C", ["g:Rule:r-1"])]
    exp, _d, _e = build(items)
    assert exp["items"] == {"p1:C": ["g:Rule:r-1"]}


def test_row_limit_hit_marks_missing_refs_unresolved_not_dangling():
    rows = [{"n.slug": f"d{i}", "n.title": "t", "n.status": "accepted", "n.rationale": None} for i in range(kg.MAX_ROWS)]
    data = {"g": dict(GRAPH, kg_decision=rows, kg_supersedes=[], kg_decided_in=[])}
    exp, dangling, _e = build([item("p1:A", ["g:Decision:beyond-the-cut"])], data, max_listed=3)
    assert dangling == [] and exp["unresolved_graphs"] == ["g"] and exp["truncated"] is True


def test_status_describes_the_kept_export(tmp_path):
    env = _env(tmp_path, PG_KG_GRAPHS="g")
    kg.run([item("p1:A", ["g:Rule:r-1"])], tmp_path, env=env, transport=Fake({"g": GRAPH}))
    good = json.loads((tmp_path / "kg-export.json").read_text())
    kg.run([item("p1:A", ["g:Rule:r-1"])], tmp_path, env=env, transport=Fake({"g": GRAPH}, fail=["g"]))
    st = json.loads((tmp_path / "kg-export-status.json").read_text())
    assert st["kept_previous"] is True and st["ok"] is False
    assert st["nodes"] == len(good["nodes"]) and st["graphs"] == good["graphs"]


def test_chain_exports_every_link():
    data = {"g": dict(GRAPH, kg_supersedes=[{"a.slug": "d-new", "b.slug": "d-old"}, {"a.slug": "d-other", "b.slug": "d-new"}])}
    exp, _d, _e = build([item("p1:A", ["g:Decision:d-old"])], data)
    n = exp["nodes"]
    assert n["g:Decision:d-old"]["superseded_by"] == "g:Decision:d-new" and "g:Decision:d-new" in n
    assert n["g:Decision:d-new"]["superseded_by"] == "g:Decision:d-other" and "g:Decision:d-other" in n


class _Drip(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        import time
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Length", "400000")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b" " * 10000)
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


def test_slow_drip_response_hits_total_budget():
    import time
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t0 = time.monotonic()
        with pytest.raises(kg.KgError, match="too slow"):
            kg.OmnigraphClient(f"http://127.0.0.1:{srv.server_address[1]}", TOKEN, timeout=0.3)("g", "kg_x", "q")
        assert time.monotonic() - t0 < 3.0
    finally:
        srv.shutdown()
        srv.server_close()
