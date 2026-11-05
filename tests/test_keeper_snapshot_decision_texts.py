"""S0b: shared read book, byte-preserving text, uncached authority."""
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph.keeper import changelog, context_sources, events
from plangraph.keeper.evidence_decisions import DecisionsAdapter


def _legacy(adapter, ids):
    return {did: text for did in ids if (text := adapter.text(did))}


@pytest.mark.parametrize("unreadable", [False, True])
def test_snapshot_and_changelog_match_per_id_bytes(tmp_path, monkeypatch, unreadable):
    world = World(tmp_path, backend="sqlite")
    path = tmp_path / "decisions.md"
    path.write_text("| D-1 | scope |  Closed.  Closes: demo:A |\n"
                    "| D-2 | scope | |\n| D-2 | scope | " + "long " * 150 + " |\n")
    adapter = DecisionsAdapter(path)
    world.store.ctx = SimpleNamespace(decisions=adapter)
    try:
        world.store.create_items(world.p("router"), [defn("A"), defn("G", ["A"], kind="goal")])
        ids = ["D-1", "D-2", "D-999", "D-1", "d-0001"]
        for did in ids:
            world.conn.execute("INSERT INTO evidence(key, kind, ref) VALUES (?, ?, ?)",
                               ("demo:A", "decision", json.dumps({"id": did})))
        world.conn.commit()
        if unreadable:
            original = Path.read_text

            def denied(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("fixture")
                return original(self, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", denied)
        expected = _legacy(adapter, ids)
        calls = []
        original_rows = adapter._rows

        def counted_rows():
            calls.append(1)
            return original_rows()

        monkeypatch.setattr(adapter, "_rows", counted_rows)
        now = world.store.now()
        actual = world.store._snapshot(now)
        assert len(calls) == 1
        if not unreadable:
            world.store._snapshot(now)
            assert len(calls) == 1
        assert actual["decision_text"] == {k: v[:600] for k, v in expected.items()}
        # Independent old text loop, not the setbased oracle's shared helper.
        monkeypatch.setattr(world.store, "_decision_text",
                            lambda *a: {k: v[:600] for k, v in _legacy(adapter, ids).items()})
        assert json.dumps(actual).encode() == json.dumps(world.store._snapshot(now)).encode()
        log = [{"seq": 1, "type": "closed", "key": "demo:A",
                "payload": {"decision_id": "D-1"}, "ts": "2026-01-01"}]
        monkeypatch.setattr(events, "read_events", lambda conn: log)
        monkeypatch.setattr(changelog, "referenced_decisions", lambda log: ids)
        expected_log = changelog.build(log, "0", {"demo:A": "title A", "demo:G": "title G"}, expected, 1)
        calls.clear()
        assert json.dumps(world.store._changelog("0")).encode() == json.dumps(expected_log).encode()
        assert len(calls) == 0
    finally:
        world.close()


def test_book_reads_once_per_version_and_enforcement_reads_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "decisions.md"
    old = "| D-1 | scope | Waives: demo:A Closes: demo:A@abcdef1 |\n"
    new = old.replace("demo:A", "demo:B")
    path.write_text(old)
    adapter = DecisionsAdapter(path)
    original = Path.read_text
    reads = []

    def counted(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    for _ in range(3):
        assert context_sources.decision_texts(adapter, ["D-1", "D-1"])["D-1"].startswith("Waives: demo:A")
    assert len(reads) == 1
    stamp = path.stat()
    path.write_text(new)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert adapter.waives("D-1", "demo:B")
    assert not adapter.waives("D-1", "demo:A")
    assert adapter.closes("D-1") == {"demo:B"}
    assert adapter.closes_with_commits("D-1") == {"demo:B": "abcdef1"}
    assert adapter.has_closes_keyword("D-1") and adapter.has("D-1")
    assert adapter.decisions() == {"D-1"}
    assert adapter.text("D-1").startswith("Waives: demo:B")
    # New file version, even with identical size, refreshes the read book.
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    reads.clear()
    for _ in range(2):
        assert context_sources.decision_texts(adapter, ["D-1"])["D-1"].startswith("Waives: demo:B")
    assert len(reads) == 1


def test_anchored_read_call_pins():
    root = Path(__file__).resolve().parents[1] / "src/plangraph/keeper"
    patterns = {
        r"^\s*(?:texts = )?context_sources\.decision_texts\(":
            {"store_read.py", "store_changelog.py"},
        r'^\s*(?:rows = decision_book\(|return \{"book": src\.decision_book\()':
            {"context_sources.py", "store_context.py"},
    }
    for pattern, expected in patterns.items():
        hits = {p.name for p in root.glob("*.py")
                if re.search(pattern, p.read_text(), re.MULTILINE)}
        assert hits == expected
    source = '\n'.join(p.read_text() for p in root.glob("*.py"))
    calls = re.findall(r"\b(?:context_sources|src)\.(decision_texts|decision_book)\(", source)
    assert sorted(calls) == ["decision_book", "decision_texts", "decision_texts"]


@pytest.mark.parametrize("path_kind", ["none", "text_only", "unreadable", "row", "canonical"])
def test_decision_text_failure_paths(tmp_path, monkeypatch, path_kind):
    world = World(tmp_path, backend="sqlite")
    path = tmp_path / "decisions.md"
    path.write_text("| D-1 | scope | first |\n| D-2 | scope | second |\n")
    adapter = DecisionsAdapter(path)
    expected = {"D-1": "first", "D-2": "second"}
    if path_kind == "none":
        adapter, expected = None, {}
    elif path_kind == "text_only":
        adapter = SimpleNamespace(text=lambda did: expected[did])
    elif path_kind == "unreadable":
        path.unlink()
        expected = {}
    else:
        # Warm the book: inject failures in the text view, not book construction.
        context_sources.decision_book(adapter)
        if path_kind == "row":
            original = context_sources._decision_cell

            def cell(row):
                if "D-1" in row:
                    raise ValueError("bad row")
                return original(row)

            monkeypatch.setattr(context_sources, "_decision_cell", cell)
        else:
            original = DecisionsAdapter._canonical

            def canonical(did):
                if did == "D-1":
                    raise ValueError("bad id")
                return original(did)

            monkeypatch.setattr(DecisionsAdapter, "_canonical", staticmethod(canonical))
        expected = {"D-2": "second"}
    world.store.ctx = SimpleNamespace(decisions=adapter)
    try:
        world.store.create_items(world.p("router"), [defn("A", kind="goal")])
        for did in ("D-1", "D-2"):
            world.conn.execute("INSERT INTO evidence(key, kind, ref) VALUES (?, ?, ?)",
                               ("demo:A", "decision", json.dumps({"id": did})))
        world.conn.commit()
        assert context_sources.decision_texts(adapter, ["D-1", "D-2"]) == expected
        assert world.store._snapshot(world.store.now())["decision_text"] == expected
        log = [{"seq": 1, "type": "closed", "key": "demo:A",
                "payload": {"decision_id": "D-1"}, "ts": "2026-01-01"}]
        monkeypatch.setattr(events, "read_events", lambda conn: log)
        monkeypatch.setattr(changelog, "referenced_decisions", lambda log: ["D-1", "D-2"])
        assert world.store._changelog("0") == changelog.build(
            log, "0", {"demo:A": "title A"}, expected, 1)
    finally:
        world.close()
