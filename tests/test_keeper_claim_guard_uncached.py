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


@pytest.mark.parametrize("key,decision,kind", [
    ("PROD-FROM-MAIN-ONLY", "D-5862", "ORDERED"),
    ("SPAWNER-DEFAULT-EFFORT-MEDIUM", "D-5858", "GO"),
    ("PHASE-LINE-GREEN-1", "D-5842", "DEFINED"),
    ("LESSON-ITEM", "D-5853", "ORDERED")])
def test_operator_order_is_not_a_changed_premise(tmp_path, key, decision, kind):
    world = World(tmp_path, backend="sqlite")
    try:
        path = tmp_path / "decisions.md"
        path.write_text(f"| {decision} | demo | {kind} demo:{key}; scope unchanged |\n")
        world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
        world.store.create_items(world.p("router"), [
            defn(key, description="premise D-1"), defn("GOAL", [key], kind="goal")])
        world.store.claim(world.p("l1main"), f"demo:{key}")
    finally:
        world.close()


def test_same_session_two_hour_grace_does_not_hide_scope_change(scenario):
    world, path, _ = scenario
    at = world.store._iso(world.clock() + 60)
    path.write_text(f"| D-9 | demo | demo:A order; Session: router; At: {at} |\n")
    assert world.store._claim_guard(world.store._item("demo:A"), None) is None
    path.write_text(f"| D-9 | demo | demo:A changes scope; Session: router; At: {at} |\n")
    with pytest.raises(Refused, match="premise outdated"):
        world.store._claim_guard(world.store._item("demo:A"), None)


def test_reconfirm_audits_without_changing_definition(scenario):
    world, path, _ = scenario
    _rewrite(path)
    before = world.store._item("demo:A")
    world.store.reconfirm(world.p("l1main"), "demo:A", "D-9")
    assert world.store._item("demo:A") == before
    assert world.store._claim_guard(before, None) is None
    event = world.conn.execute("SELECT payload FROM events WHERE type='premise_reconfirmed'").fetchone()
    assert '"D-9"' in str(event["payload"])
    path.write_text(path.read_text().replace("demo:A", "demo:A changed scope"))
    with pytest.raises(Refused, match="premise outdated"):
        world.store._claim_guard(before, None)


def test_reconfirm_refuses_reader_missing_decision_and_definition_edit(scenario):
    world, path, _ = scenario
    _rewrite(path)
    with pytest.raises(Refused, match="forbidden"):
        world.store.reconfirm(world.p("reader"), "demo:A", "D-9")
    with pytest.raises(Refused, match="recorded decision"):
        world.store.reconfirm(world.p("l1main"), "demo:A", "D-404")
    world.store.reconfirm(world.p("router"), "demo:A", "D-9")
    world.store.edit_definition(world.p("router"), "demo:A", {"title": "new premise"})
    with pytest.raises(Refused, match="premise outdated"):
        world.store._claim_guard(world.store._item("demo:A"), None)
    assert world.store.verify_integrity() == []


@pytest.mark.parametrize("session,delay", [("other", 60), ("router", 7201), ("router", -1)])
def test_grace_requires_same_session_and_window(scenario, session, delay):
    world, path, _ = scenario
    at = world.store._iso(world.clock() + delay)
    path.write_text(f"| D-9 | demo | demo:A order; Session: {session}; At: {at} |\n")
    with pytest.raises(Refused, match="premise outdated"):
        world.store._claim_guard(world.store._item("demo:A"), None)


def test_reconfirm_refuses_concurrent_definition_change(scenario, monkeypatch):
    world, _, _ = scenario
    original = context_sources.decision_book
    def changed(*args, **kwargs):
        book = original(*args, **kwargs)
        world.store.edit_definition(world.p("router"), "demo:A", {"title": "changed during read"})
        return book
    monkeypatch.setattr(context_sources, "decision_book", changed)
    with pytest.raises(Refused, match="definition changed"):
        world.store.reconfirm(world.p("l1main"), "demo:A", "D-9")


def test_item_source_decision_is_its_own_premise(tmp_path):
    world = World(tmp_path, backend="sqlite")
    try:
        path = tmp_path / "decisions.md"
        path.write_text("| D-9 | demo | demo:A source order |\n")
        world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
        world.store.create_items(world.p("router"), [
            defn("A", source="D-9", description="premise D-1"), defn("GOAL", ["A"], kind="goal")])
        world.store.claim(world.p("l1main"), "demo:A")
    finally:
        world.close()


def test_retired_confirmation_does_not_exempt_the_premise(scenario):
    world, path, _ = scenario
    _rewrite(path)
    world.store.reconfirm(world.p("l1main"), "demo:A", "D-9")
    path.write_text(path.read_text() + "| D-10 | demo | supersedes D-9 |\n")
    with pytest.raises(Refused, match="premise outdated"):
        world.store._claim_guard(world.store._item("demo:A"), None)


def test_unreadable_reconfirmation_is_refused_and_audited(scenario):
    world, path, _ = scenario
    path.unlink()
    with pytest.raises(Refused, match="decisions authority unavailable"):
        world.store.reconfirm(world.p("l1main"), "demo:A", "D-9")
    row = world.conn.execute("SELECT ok,code FROM audit_log WHERE action='reconfirm'").fetchone()
    assert row["ok"] == 0 and row["code"] == "evidence_unverified"
