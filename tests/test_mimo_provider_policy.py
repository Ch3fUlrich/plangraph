import asyncio
import dataclasses
import json
from pathlib import Path
import subprocess
import sys

import pytest

from plangraph.spawner.mimo_policy import MimoPathError, check_mimo_path


GOOD = ("mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro")
BAD = ("omniroute/mimo-v2.6-flash", "mimo-v2.6-flash",
       "opencode/mimo-v2.6-flash-free", "mimo-v2.6-pro", "other/mimo-new")


@pytest.mark.parametrize("model", GOOD + ("other/model", "l2-worker"))
def test_allowed(model):
    assert check_mimo_path(model) == model


@pytest.mark.parametrize("model", BAD)
def test_refused(model):
    with pytest.raises(MimoPathError) as exc:
        check_mimo_path(model)
    assert all(good in str(exc.value) for good in GOOD)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("model", BAD)
def test_spawn_starts_nothing(monkeypatch, dry_run, model):
    from plangraph.spawner.runner import SpawnerRunner
    def forbidden(*args, **kwargs):
        pytest.fail("process started")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    runner = SpawnerRunner()
    with pytest.raises(MimoPathError):
        asyncio.run(runner.spawn("test", model_or_combo=model, dry_run=dry_run))
    assert not runner.jobs and not runner._processes and not runner._tasks


@pytest.mark.parametrize("gateway", [True, False])
def test_command_refuses(gateway):
    from plangraph.spawner.clients import build_command
    with pytest.raises(MimoPathError):
        build_command("test", BAD[0], gateway=gateway)


def test_legs(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS, load_legs
    for leg in DEFAULT_LEGS:
        check_mimo_path(leg.model)
    raw = dataclasses.asdict(DEFAULT_LEGS[0])
    raw["model"] = BAD[0]
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [raw]}))
    with pytest.raises(ValueError, match="mimo/mimo-v2.6-pro"):
        load_legs(path)


def test_catalog_and_filter():
    from plangraph.spawner.catalog_db import CatalogDB
    from plangraph.spawner.resolver import is_model_available
    with CatalogDB() as db:
        db.init_and_seed()
        for row in db.list_models(active_only=False):
            check_mimo_path(row["model_id"])
    for model in BAD:
        assert not is_model_available({"id": model})


def test_sweep(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "config.md").write_text("\n".join(BAD[:3] + GOOD[:1]))
    (root / "history").mkdir()
    (root / "history" / "old.md").write_text(BAD[0])
    allow = tmp_path / "allow.txt"
    allow.write_text("history/old.md\n")
    script = Path(__file__).resolve().parents[1] / "tools/mimo_path_sweep.py"
    def run():
        return subprocess.run([sys.executable, str(script), str(root), "--allow-file", str(allow)], capture_output=True, text=True)
    result = run()
    assert result.returncode == 1, result.stderr
    hits = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(hits) == 3
    assert all(set(hit) == {"root", "file", "line", "text", "suggested"} for hit in hits)
    (root / "config.md").write_text(GOOD[0])
    assert run().returncode == 0


def test_sweep_redacts_secrets(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    script = Path(__file__).resolve().parents[1] / "tools/mimo_path_sweep.py"
    spec = spec_from_file_location("mimo_sweep", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = "sk-test-secret-never-print"
    (tmp_path / "config.json").write_text(json.dumps({"apiKey": secret, "model": BAD[0]}))
    hits = list(module.scan(tmp_path, []))
    assert len(hits) == 1
    assert secret not in json.dumps(hits)
    assert BAD[0] in hits[0]["text"]
