"""Host bench slice invariants: record host (C1), catalog DB path/provision (D1/D3),
leg-id -> benchmark model_id mapping (D2). All state stays under tmp_path."""
import json
import socket
from pathlib import Path

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.routing_support import catalog_db
from plangraph.spawner.bench_entry import benchmark_options
from plangraph.spawner.service import SpawnService

CARD = {"estimate_tokens": 1000}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Pin HOME and catalog/state paths into tmp; never touch ~/.cache or ~/.local/state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("PLANGRAPH_HOST", raising=False)
    default_db = tmp_path / "spawner_catalog.db"
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(default_db))
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", default_db)


def test_record_host_field(tmp_path, monkeypatch):
    """I1/C1: every new record carries host; PLANGRAPH_HOST overrides; old records load."""
    service = SpawnService(state=tmp_path / "state")
    record = service.save_refusal(SpawnRefused("host field probe"))
    stored = service.result(record["run_id"])
    assert stored["host"] == socket.gethostname()
    assert json.loads(service._path(record["run_id"]).read_text())["host"] == socket.gethostname()
    # A spawn-shaped record is stamped at the same write site.
    run_id = "run-" + "a1b2c3d4"
    service._save(dict(run_id=run_id, role="implement", status="running", started=1.0,
                       ended=None, attempts=[], stdout_tail="", exit_code=None,
                       cwd=str(tmp_path)))
    assert service.result(run_id)["host"] == socket.gethostname()
    # PLANGRAPH_HOST overrides; no other env does.
    monkeypatch.setenv("HOSTNAME", "not-the-host")
    monkeypatch.setenv("PLANGRAPH_HOST", "bench-host-override")
    stamped = service.save_refusal(SpawnRefused("override probe"))
    assert service.result(stamped["run_id"])["host"] == "bench-host-override"
    monkeypatch.delenv("PLANGRAPH_HOST")
    plain = service.save_refusal(SpawnRefused("no override"))
    assert service.result(plain["run_id"])["host"] == socket.gethostname()
    # Old records without a host still read via result().
    old_id = "run-oldrecord01"
    path = service._path(old_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": old_id, "status": "completed"}))
    old = service.result(old_id)
    assert old["run_id"] == old_id and "host" not in old


def test_catalog_db_path_both_orders(tmp_path):
    """I2/D1: a global --db-path reaches the subcommand in either position."""
    default = tmp_path / "spawner_catalog.db"
    first, second = tmp_path / "first.db", tmp_path / "second.db"
    assert catalog_db.main(["--db-path", str(first), "init-and-seed"]) == 0
    assert catalog_db.main(["init-and-seed", "--db-path", str(second)]) == 0
    assert first.is_file() and second.is_file()
    assert not default.exists()
