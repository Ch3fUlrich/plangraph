"""CLI end to end against a live in-process API, plus the unreachable fallback."""
import json
import os
import threading

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import api, cli, cli_admin, config


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", []), ("B", ["A"])])
    yield world
    world.close()


def make_cfg(tmp_path, world):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
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
