"""Config loader: JSON, mode 600 required, fields per contract, no secrets out."""
import json
import os

import pytest

from plangraph.keeper import config


def write_config(tmp_path, data, mode=0o600):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_mode_not_600_is_refused(tmp_path):
    path = write_config(tmp_path, {"db": "k.db"}, mode=0o644)
    with pytest.raises(config.ConfigError) as exc:
        config.load_config(path)
    assert "600" in str(exc.value)


def test_mode_600_loads_and_resolves_paths(tmp_path):
    path = write_config(tmp_path, {
        "db": "state/keeper.db", "snapshot": "state/plan.snapshot.json",
        "repos": {"demo": "/srv/demo"}, "runs_dir": "/srv/runs",
        "decisions_file": "/srv/DECISIONS.md", "ci": {"demo": "none"},
        "bind": "127.0.0.1:8765", "default_lease": 7200,
    })
    cfg = config.load_config(path)
    assert cfg.host == "127.0.0.1" and cfg.port == 8765
    assert cfg.db == (tmp_path / "state/keeper.db").resolve()
    assert cfg.snapshot == (tmp_path / "state/plan.snapshot.json").resolve()
    assert cfg.repos == {"demo": "/srv/demo"}
    assert cfg.runs_dir == "/srv/runs"
    assert cfg.decisions_file == "/srv/DECISIONS.md"
    assert cfg.ci == {"demo": "none"}
    assert cfg.default_lease == 7200


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path / "nope.json")


def test_bad_json_is_refused(tmp_path):
    path = tmp_path / "keeper.json"
    path.write_text("{not json", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(config.ConfigError):
        config.load_config(path)


def test_repr_never_leaks_values(tmp_path):
    path = write_config(tmp_path, {
        "db": "k.db",
        "tokens": {"admin": "SUPER-SECRET-TOKEN"},
        "semaphore_token": "ANOTHER-SECRET",
    })
    cfg = config.load_config(path)
    text = repr(cfg)
    assert "SUPER-SECRET-TOKEN" not in text
    assert "ANOTHER-SECRET" not in text
    assert "db" in text  # key names are fine


def test_verifier_context_uses_configured_adapters(tmp_path):
    path = write_config(tmp_path, {
        "db": "k.db", "repos": {"demo": "/srv/demo"},
        "runs_dir": "/srv/runs", "decisions_file": "/srv/DECISIONS.md",
        "ci": {"demo": "none"},
    })
    cfg = config.load_config(path)
    ctx = config.verifier_context(cfg)
    assert ctx.git is not None and ctx.decisions is not None
    assert ctx.runs is not None
