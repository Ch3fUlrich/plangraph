"""PG-READY-OWNER (D-953): heartbeats pull the next ready item for THEIR owner, never an operator item."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

from plangraph import views

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_views import item  # noqa: E402


def _items():
    a = item("demo:A", prio=5); a["owner"] = "l1"
    b = item("demo:B", prio=9); b["owner"] = "other-L1"
    q = item("demo:Q", prio=9, kind="question"); q["owner"] = "l1"
    op = item("demo:OP", prio=9); op["owner"] = "l1"; op["operator"] = True
    c = item("demo:C", prio=1); c["owner"] = "l1"
    return [a, b, q, op, c]


def keys(rows):
    return [r["key"] for r in rows]


def test_owner_filter():
    assert keys(views.ready(_items(), owner="l1")) == ["demo:OP", "demo:Q", "demo:A", "demo:C"]
    assert keys(views.ready(_items(), owner="other-L1")) == ["demo:B"]


def test_agent_filter_drops_operator_items_and_kinds():
    assert keys(views.ready(_items(), owner="l1", agent=True)) == ["demo:A", "demo:C"]
    assert "demo:Q" not in keys(views.ready(_items(), agent=True))


def test_brief_passes_filters_and_n():
    text = views.brief(_items(), owner="l1", agent=True, n=1)
    assert text.startswith("demo:A |") and "\n" not in text


def test_default_unchanged():
    assert keys(views.ready(_items())) == ["demo:B", "demo:OP", "demo:Q", "demo:A", "demo:C"]


# ------------------------------------------------------------ store / HTTP
from keeper_util import World, defn  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402


@pytest.fixture
def srv(tmp_path):
    w = World(tmp_path)
    r = w.p("router")
    w.store.create_items(r, [
        defn("MINE", owner="l1main", priority=5),
        defn("THEIRS", owner="l1other", priority=9),
        defn("Q", owner="l1main", kind="question", evidence_kinds=["decision"], priority=9),
        defn("GOAL", kind="goal", deps=["demo:MINE", "demo:THEIRS", "demo:Q"])])
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path), "snapshot": str(tmp_path / "s.json"),
                               "bind": "127.0.0.1:0", "reap_interval": 3600}))
    os.chmod(cfg, 0o600)
    server = api.make_server(w.store, config.load_config(cfg), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, w
    server.shutdown()
    server.server_close()


def _get(server, w, query, who="l1main"):
    req = urllib.request.Request(server.base_url + "/v1/ready?" + query,
                                 headers={"Authorization": f"Bearer {w.tok(who)}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode()


def test_api_mine_and_agent(srv):
    server, w = srv
    text = _get(server, w, "project=demo&mine=1&agent=1")
    assert text.splitlines()[0].startswith("demo:MINE |") and "THEIRS" not in text and "demo:Q" not in text
    text = _get(server, w, "project=demo&owner=l1other")
    assert text.startswith("demo:THEIRS |")
    data = json.loads(_get(server, w, "project=demo&mine=1&agent=1&fmt=json"))
    assert [i["key"] for i in data["items"]] == ["demo:MINE"]


def test_store_ready_mine_resolves_to_principal(srv):
    _, w = srv
    assert w.store.ready(w.p("l1other"), project="demo", mine=True, agent=True).startswith("demo:THEIRS |")


def _cli(server, w, tmp_path, *args, who="l1main"):
    import subprocess
    tok = tmp_path / f"{who}.tok"
    tok.write_text(w.tok(who))
    tok.chmod(0o600)
    env = dict(os.environ, PG_URL=server.base_url, PG_TOKEN_FILE=str(tok),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    return subprocess.run([sys.executable, "-m", "plangraph.keeper.cli", "ready", *args],
                          env=env, capture_output=True, text=True, timeout=30)


def test_cli_mine_agent(srv, tmp_path):
    server, w = srv
    p = _cli(server, w, tmp_path, "--project", "demo", "--mine", "--agent", "-n", "1")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip().startswith("demo:MINE |")
    p = _cli(server, w, tmp_path, "--project", "demo", "--owner", "l1other")
    assert p.stdout.strip().startswith("demo:THEIRS |")


def test_mcp_schema_has_filters():
    from plangraph.keeper import mcp
    tool = next(t for t in mcp.TOOLS if t["name"] == "ready")
    assert {"owner", "mine", "agent"} <= set(tool["inputSchema"]["properties"])
