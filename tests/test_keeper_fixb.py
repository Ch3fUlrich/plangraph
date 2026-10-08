"""X7-FIX-B: review round 2 on ``close_on_decision`` and ``batch_roots``.

Three rules under test:

1. ``config.batch_roots`` drops blank/whitespace-only entries *before*
   resolving (``""`` used to resolve to the config dir and silently authorise
   it) and refuses non-str entries with ``ConfigError``.
2. The configured roots are authoritative: ``make_server`` overwrites stale or
   empty store roots, and the daemon hands ``cfg.batch_roots`` to ``Store``.
3. ``close_on_decision`` re-runs every file/tier check *inside* the write
   transaction, so swapping the batch file, dropping a decision row, dropping
   its ``Closes:`` token or flipping the tier between the pre-lock check and
   the commit refuses with the pre-lock code (``conflict`` for the tier).
"""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_claimed
from plangraph.keeper import api, config, daemon
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

KEY = "demo:A"
DID = "D-300"
HEADER = "| ID | Note | Scope |\n|---|---|---|\n"
CLOSES = f"| {DID} | legacy batch close | Closes: demo:A |\n"


def _cfg(tmp_path, extra, world=None):
    path = tmp_path / "keeper.json"
    data = {"db": str(world.path) if world else "keeper.db",
            "bind": "127.0.0.1:8765"}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(str(path), 0o600)
    return config.load_config(path)


def _world(tmp_path):
    """A tier-``none`` item in ``claimed`` with a verified decision attached, a
    batch file under ``tmp_path/plan`` and the root configured on the store."""
    w = World(tmp_path)
    w.store.create_items(w.p("router"), [
        defn("A", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["A"], kind="goal"),
    ])
    w.decisions = tmp_path / "DECISIONS.md"
    w.decisions.write_text(HEADER + CLOSES, encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(w.decisions))
    root = tmp_path / "plan"
    root.mkdir(parents=True, exist_ok=True)
    w.batch_path = root / "batch.md"
    w.batch_path.write_text("batch body\n", encoding="utf-8")
    digest = hashlib.sha256(w.batch_path.read_bytes()).hexdigest()
    w.batch_ref = f"{w.batch_path}@{digest}"
    w.store.batch_roots = [str(root)]
    to_claimed(w, KEY)
    w.store.add_evidence_router(w.p("router"), KEY, "decision", {"id": DID})
    return w


def _hook_once(store, name, action, monkeypatch):
    """Run ``action`` once, right after the pre-lock call of ``store.name``."""
    original = getattr(store, name)
    fired = []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired:
            fired.append(1)
            action()
        return result

    monkeypatch.setattr(store, name, wrapper)


def _close_refused(w, code):
    with pytest.raises(Refused) as exc:
        w.store.close_on_decision(w.p("router"), KEY, w.batch_ref)
    assert exc.value.code == code, f"{exc.value.code} != {code}: {exc.value.msg}"
    assert w.store._item(KEY)["state"] == "claimed"
    w.close()


# ------------------------------------------------------------------- config
def test_config_blank_batch_root_dropped(tmp_path):
    cfg = _cfg(tmp_path, {"batch_roots": ["", "   ", "/srv/plan"]})
    assert cfg.batch_roots == ["/srv/plan"]


def test_config_non_str_batch_root_refused(tmp_path):
    for bad in [1, None, ["nested"], 3.5]:
        with pytest.raises(config.ConfigError):
            _cfg(tmp_path, {"batch_roots": [bad]})


# -------------------------------------------------------------------- wiring
def test_make_server_config_roots_override_store_roots(tmp_path):
    w = World(tmp_path)
    w.store.batch_roots = ["/stale/plan"]
    cfg = _cfg(tmp_path, {"batch_roots": [str(tmp_path / "plan")]}, world=w)
    server = api.make_server(w.store, cfg, port=0)
    try:
        assert w.store.batch_roots == [str(tmp_path / "plan")]
    finally:
        server.server_close()
    w.close()


def test_daemon_passes_config_roots_to_store(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, {"batch_roots": [str(tmp_path / "plan")]})
    captured = {}

    def fake_store(conn, ctx=None, batch_roots=None):
        captured["roots"] = list(batch_roots or [])
        raise RuntimeError("stop before serving")

    monkeypatch.setattr(daemon.kdb, "lock_keeper",
                        lambda path: SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(daemon.kdb, "open_db",
                        lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(daemon, "Store", fake_store)
    with pytest.raises(RuntimeError):
        daemon.run(cfg)
    assert captured["roots"] == cfg.batch_roots


# -------------------------------------------------------------------- toctou
def test_batch_file_swapped_under_transaction_refused(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _hook_once(w.store, "_read_batch",
               lambda: w.batch_path.write_text("swapped\n", encoding="utf-8"),
               monkeypatch)
    _close_refused(w, "evidence_unverified")


def test_decision_row_dropped_under_transaction_refused(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _hook_once(w.store, "_decision_evidence",
               lambda: w.decisions.write_text(
                   HEADER + "| D-301 | someone else |\n", encoding="utf-8"),
               monkeypatch)
    _close_refused(w, "evidence_unverified")


def test_closes_token_dropped_under_transaction_refused(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _hook_once(w.store, "_decision_evidence",
               lambda: w.decisions.write_text(
                   HEADER + f"| {DID} | token gone |\n", encoding="utf-8"),
               monkeypatch)
    _close_refused(w, "evidence_unverified")


def test_tier_flipped_under_transaction_refused(tmp_path, monkeypatch):
    w = _world(tmp_path)

    def flip():
        w.conn.execute("UPDATE items SET review_tier='standard' WHERE key=?",
                       (KEY,))

    _hook_once(w.store, "_read_batch", flip, monkeypatch)
    _close_refused(w, "conflict")


def test_happy_path_still_closes(tmp_path):
    w = _world(tmp_path)
    assert w.store.close_on_decision(w.p("router"), KEY,
                                     w.batch_ref)["state"] == "done"
    w.close()
