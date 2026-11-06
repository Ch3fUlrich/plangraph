"""Claim authority bypasses stamps; display books retain their cache."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph.keeper import context_sources
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter


@pytest.fixture
def scenario(tmp_path):
    world = World(tmp_path, backend="sqlite")
    path = tmp_path / "decisions.md"
    path.write_text("| D-1 | scope | initial |\n| D-9 | scope | demo:B |\n")
    adapter = DecisionsAdapter(path)
    world.store.ctx = SimpleNamespace(decisions=adapter)
    world.store.create_items(world.p("router"), [
        defn("A", description="per D-1 D-2 D-3"),
        defn("GOAL", ["A"], kind="goal")])
    try:
        yield world, path, adapter
    finally:
        world.close()


def _rewrite(path):
    stamp = path.stat()
    path.write_text(path.read_text().replace("demo:B", "demo:A"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        stamp.st_ino, stamp.st_mtime_ns, stamp.st_size)


def test_claim_refuses_same_stamp_newer_decision(scenario):
    world, path, adapter = scenario
    context_sources.decision_book(adapter)
    _rewrite(path)
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A")
    assert exc.value.code == "bad_state"
    assert str(exc.value) == (
        "bad_state: demo:A stale: premise outdated: D-9 names this item after its definition; "
        "re-confirm (edit the item to cite it) first "
        "(override: force_stale=<reason>, audited)")
    assert world.store._item("demo:A")["state"] == "open"


def test_display_keeps_cached_book_after_rewrite_and_guard(scenario):
    world, path, adapter = scenario
    book = context_sources.decision_book(adapter)
    before = world.store.show(world.p("reader"), "demo:A")
    _rewrite(path)
    override = world.store._claim_guard(world.store._item("demo:A"), " checked ")
    assert override["reason"] == "checked"
    assert override["overrode"] == [
        "premise outdated: D-9 names this item after its definition; "
        "re-confirm (edit the item to cite it) first"]
    assert context_sources.decision_book(adapter) is book
    assert world.store.show(world.p("reader"), "demo:A") == before


def test_one_file_read_per_claim_not_per_cited_id(scenario, monkeypatch):
    world, path, adapter = scenario
    context_sources.decision_book(adapter)
    original = Path.read_text
    reads = []

    def counted(self, *args, **kwargs):
        if self == path:
            reads.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    item = world.store._item("demo:A")
    for count in (1, 2):
        assert world.store._claim_guard(item, None) is None
        assert len(reads) == count
