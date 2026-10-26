"""``python3 -m plangraph.projector`` driven in-process against a tmp keeper config.

Runs on whichever backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``).
"""
import importlib.util
import json
import os
import threading
from pathlib import Path

import pytest

from keeper_util import World, seed
from plangraph.projector import cli

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", []), ("B", [])])
    yield w
    w.close()


@pytest.fixture
def cfg(world, tmp_path):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({"db": str(world.path),
                                "snapshot": str(tmp_path / "plan.snapshot.json")}),
                    encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def run(capsys, cfg, *argv):
    code = cli.main(["--config", str(cfg), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def last_json(out):
    return json.loads(out.strip().splitlines()[-1])


def mapped_dir(tmp_path):
    """One recorded run whose workflow id maps to the seeded item ``demo:A``."""
    d = tmp_path / "hist"
    d.mkdir()
    doc = json.loads((FIXTURES / "01_ok_single.json").read_text())
    doc["workflow_id"] = "card:demo:A"
    started = doc["history"]["events"][0]
    started["workflowExecutionStartedEventAttributes"].pop("memo", None)
    (d / "run.json").write_text(json.dumps(doc))
    return d


def test_run_once_then_watermark_prints_a_row(capsys, cfg):
    code, out, _ = run(capsys, cfg, "run-once", "--history-dir", str(FIXTURES))
    assert code == 0
    summary = last_json(out)
    assert summary["ok"] is True and summary["processed"] == 14
    code, out, _ = run(capsys, cfg, "watermark")
    assert code == 0
    row = last_json(out)
    assert row["workflow_id"] == "*" and row["updated_at"]


def test_watermark_before_any_pass_is_null(capsys, cfg):
    code, out, _ = run(capsys, cfg, "watermark")
    assert (code, last_json(out)) == (0, None)


def test_run_once_projects_attempts_into_the_keeper(capsys, cfg, world, tmp_path):
    hist = mapped_dir(tmp_path)
    code, out, _ = run(capsys, cfg, "run-once", "--history-dir", str(hist))
    assert code == 0
    summary = last_json(out)
    assert summary["attempts_written"] >= 1 and summary["missing_item"] == 0
    rows = world.conn.execute("SELECT workflow_id FROM attempts WHERE key=?",
                              ("demo:A",)).fetchall()
    assert [r[0] for r in rows] == ["card:demo:A"] * summary["attempts_written"]


def test_rebuild_twice_changes_nothing(capsys, cfg, world, tmp_path):
    hist = mapped_dir(tmp_path)

    def snapshot():
        return [
            [tuple(r) for r in world.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("attempts", "projector_applied")]

    assert run(capsys, cfg, "rebuild", "--history-dir", str(hist))[0] == 0
    first = snapshot()
    code, out, _ = run(capsys, cfg, "rebuild", "--history-dir", str(hist))
    second = last_json(out)
    assert code == 0 and snapshot() == first
    assert second["attempts_written"] == 0 and second["states_written"] == 0
    assert second["attempts_existing"] >= 1


def test_open_only_skips_closed_runs(capsys, cfg):
    code, out, _ = run(capsys, cfg, "run-once", "--open-only", "--history-dir", str(FIXTURES))
    assert code == 0 and last_json(out)["processed"] == 0


def test_run_loops_until_stopped_and_prints_one_line_per_pass(capsys, cfg, monkeypatch):
    passes = []
    real = cli.project_source

    def counting(*a, **kw):
        passes.append(1)
        if len(passes) == 2:
            threading.Timer(0, lambda: os.kill(os.getpid(), 15)).start()   # SIGTERM
        return real(*a, **kw)

    monkeypatch.setattr(cli, "project_source", counting)
    code, out, _ = run(capsys, cfg, "run", "--interval", "0.05", "--history-dir", str(FIXTURES))
    assert code == 0 and len(passes) >= 2
    lines = out.strip().splitlines()
    assert len(lines) == len(passes) and all(json.loads(l)["ok"] for l in lines)


@pytest.mark.parametrize("argv", [
    [],
    ["bogus"],
    ["run-once", "--no-such-flag"],
    ["run", "--interval", "0"],
    ["run", "--interval", "abc"],
    ["run-once", "--history-dir", "/nonexistent-projector-dir"],
])
def test_bad_args_exit_2(capsys, cfg, argv):
    assert run(capsys, cfg, *argv)[0] == 2


def test_missing_or_unreadable_config_exits_2(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("PG_CONFIG", raising=False)
    assert cli.main(["watermark"]) == 2
    loose = tmp_path / "loose.json"
    loose.write_text("{}")
    os.chmod(loose, 0o644)
    assert cli.main(["--config", str(loose), "watermark"]) == 2
    assert "projector: config:" in capsys.readouterr().err


def test_runtime_failure_exits_1_without_leaking_a_dsn(capsys, cfg, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("could not connect postgresql://user:hunter2@db.example.invalid/x")

    monkeypatch.setattr(cli, "project_source", boom)
    code, _, err = run(capsys, cfg, "run-once", "--history-dir", str(FIXTURES))
    assert code == 1 and "hunter2" not in err and "postgresql://" not in err


@pytest.mark.skipif(importlib.util.find_spec("plangraph.projector.reconcile") is None,
                    reason="reconcile.py not merged yet")
def test_reconcile_prints_a_json_summary(capsys, cfg):
    assert run(capsys, cfg, "run-once", "--history-dir", str(FIXTURES))[0] == 0
    code, out, _ = run(capsys, cfg, "reconcile", "--history-dir", str(FIXTURES))
    summary = last_json(out)
    assert code == 0 and summary["ok"] is True and "open_alarms" in summary
