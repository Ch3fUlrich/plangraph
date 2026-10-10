"""PG-CLI-READY-FIX: ``pg ready`` prints the server's items as-is.

The server already filtered (dependencies, owner, agent) and capped the list;
re-running ``views.ready`` over the reply subset dropped every item whose done
dependency was not itself in the reply.  Measured: ``-n 5`` printed 3 of 5.
"""
import io
from contextlib import redirect_stderr, redirect_stdout

from keeper_util import defn, to_done
from plangraph import views
from plangraph.keeper import cli, fmt
from test_apply_batch import srv, token_file, world   # noqa: F401


def _ready(monkeypatch, srv, token_file, *argv):
    monkeypatch.setenv("PG_URL", srv.base_url)
    monkeypatch.setenv("PG_TOKEN_FILE", token_file)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["ready", *argv])
    return code, out.getvalue().strip().splitlines()


def _seed(world):
    world.store.create_items(world.p("router"), [
        defn("DONE1", []),
        defn("B", ["DONE1"], priority=5), defn("C", ["DONE1"], priority=4),
        defn("D", ["DONE1"], priority=3), defn("E", [], priority=1),
        defn("GOAL", ["B", "C", "D", "E"], kind="goal")])
    to_done(world, "demo:DONE1")


def test_cli_ready_keeps_items_whose_done_dep_is_outside_the_reply(
        world, srv, token_file, monkeypatch):
    _seed(world)
    code, lines = _ready(monkeypatch, srv, token_file, "--brief", "-n", "5")
    assert code == 0
    assert [ln.split(" | ")[0] for ln in lines] == [
        "demo:B", "demo:C", "demo:D", "demo:E"]
    code, lines = _ready(monkeypatch, srv, token_file, "--brief", "-n", "3")
    assert [ln.split(" | ")[0] for ln in lines] == ["demo:B", "demo:C", "demo:D"]


def test_cli_ready_matches_api_text_and_unblocks(world, srv, token_file, monkeypatch):
    _seed(world)
    full = views.unblocks(world.store._all_items())
    _, lines = _ready(monkeypatch, srv, token_file, "--brief", "-n", "3")
    assert "\n".join(lines) == world.store.ready(world.p("router"), n=3)
    assert f"unblocks {full['demo:B']}" in lines[0]


def test_ready_text_server_items_not_refiltered():
    items = [{"key": "p:X", "title": "x", "state": "open", "depends_on": ["p:GONE"],
              "estimate_h": 1, "priority": 1}]
    assert fmt.ready_text(items, unblocks={"p:X": 2}, server_items=True) == \
        "p:X | x | est 1h | unblocks 2 | prio 1"
    assert fmt.ready_text(items, unblocks={"p:X": 2}) == ""   # snapshot path still filters
