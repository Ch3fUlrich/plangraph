#!/usr/bin/env python3
"""Regenerate examples/snapshot.json, examples/rendered/demo.html and the batch sha.

Deterministic: fixed clock, in-memory throwaway keeper DB, synthetic plans only.
    python3 tools/split/build_examples.py [--check]
``--check`` exits 1 if the committed files differ from a fresh build.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from plangraph import render  # noqa: E402
from plangraph.keeper import auth, db as kdb  # noqa: E402
from plangraph.keeper.importer import load_dir  # noqa: E402
from plangraph.keeper.store import Store  # noqa: E402

EX = ROOT / "examples"
CLOCK = 1_767_225_600.0  # 2026-01-01T00:00:00Z


def build():
    """``{relative path: text}`` of every generated example file."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = kdb.open_db(Path(tmp) / "k.db", create=True)
        token = auth.add_principal(conn, "router", "router", [])
        store = Store(conn, clock=lambda: CLOCK, verifiers={})
        router = auth.authenticate(conn, token)
        store.import_items(router, load_dir(EX / "plans"))
        snap = store.snapshot(router)
        conn.close()
    snap["head"] = None  # event hashes carry no meaning for the examples
    prev_sha = os.environ.get("PLANGRAPH_BUILD_SHA")
    os.environ["PLANGRAPH_BUILD_SHA"] = "example"  # a git-derived sha would change every commit
    try:
        page = render.render(snap, render.default_vendor_dir(), title="plangraph example", cdn=True)
    finally:
        if prev_sha is None:
            del os.environ["PLANGRAPH_BUILD_SHA"]
        else:
            os.environ["PLANGRAPH_BUILD_SHA"] = prev_sha
    source = (EX / "batch" / "demo-source.md").read_bytes()
    batch = (EX / "batch" / "demo-batch.yaml").read_text()
    sha = hashlib.sha256(source).hexdigest()
    batch = re.sub(r"(?m)^source_sha256: .*$", f"source_sha256: {sha}", batch)
    return {"snapshot.json": json.dumps(snap, indent=1, sort_keys=True) + "\n",
            "rendered/demo.html": page, "batch/demo-batch.yaml": batch}


def main(argv):
    files = build()
    stale = [n for n, t in files.items()
             if not (EX / n).exists() or (EX / n).read_text() != t]
    if "--check" in argv:
        print("stale: " + ", ".join(stale) if stale else "examples up to date")
        return 1 if stale else 0
    for n, t in files.items():
        (EX / n).parent.mkdir(parents=True, exist_ok=True)
        (EX / n).write_text(t)
    print("wrote " + ", ".join(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
