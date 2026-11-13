"""Standalone worker lock conformance to its existing worker-core path dependency."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).parents[1]


def test_worker_lock_includes_core_manifest_dependencies():
    manifest = tomllib.loads((ROOT / "rust/worker-core/Cargo.toml").read_text())
    lock = tomllib.loads((ROOT / "rust/worker/Cargo.lock").read_text())
    core = next(p for p in lock["package"] if p["name"] == "pg-worker-core")
    assert set(manifest["dependencies"]) <= {d.split()[0] for d in core["dependencies"]}
    assert {"clock", "std"} <= set(manifest["dependencies"]["chrono"]["features"])
    assert "use chrono::" in (ROOT / "rust/worker-core/src/writer_docs_map.rs").read_text()
