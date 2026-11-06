"""Unreadable claim authority refuses without changing best-effort reads."""
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph.keeper import context_sources as src
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter


@pytest.fixture
def scenario(tmp_path):
    world = World(tmp_path, backend="sqlite")
    path = tmp_path / "decisions.md"
    path.write_text("| D-1 | scope | initial |\n")
    adapter = DecisionsAdapter(path)
    world.store.ctx = SimpleNamespace(decisions=adapter)
    world.store.create_items(world.p("router"), [
        defn("A", description="per D-1"), defn("GOAL", ["A"], kind="goal")])
    try:
        yield world, path, adapter
    finally:
        world.close()


@pytest.mark.parametrize("failure", ["directory", "encoding", "missing", "read"])
@pytest.mark.parametrize("force", [None, "checked"])
def test_unreadable_refuses_without_writes(scenario, monkeypatch, failure, force):
    world, path, adapter = scenario
    if failure == "read":
        original = type(path).read_text

        def unreadable(self, *args, **kwargs):
            if self == path:
                raise OSError("PRIVATE FILE CONTENT")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(path), "read_text", unreadable)
    else:
        path.unlink()
        if failure == "directory":
            path.mkdir()
        elif failure == "encoding":
            path.write_bytes(b"PRIVATE FILE CONTENT\xff")
    def state():
        return [row for row in world.conn.iterdump()
                if not row.startswith('INSERT INTO "audit_log"')
                and not row.startswith('INSERT INTO "sqlite_sequence" VALUES(\'audit_log\'')]

    before = state()
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A", force_stale=force)
    assert exc.value.code == "bad_state"
    assert str(exc.value) == f"bad_state: cannot read decisions file: {path}"
    assert world.store._item("demo:A")["state"] == "open"
    assert state() == before  # Refusal audit is allowed; events, leases and state are not.
    assert src.decision_book(adapter)["rows"] == {}
    assert src.decision_texts(adapter, ["D-1"]) == {}
    world.store.show(world.p("reader"), "demo:A")
    if failure == "encoding":
        with pytest.raises(UnicodeDecodeError):
            adapter._rows()
    else:
        assert adapter._rows() == {}


@pytest.mark.parametrize("configured", [False, True])
def test_readable_or_unconfigured_claims(scenario, configured):
    world, path, adapter = scenario
    if not configured:
        adapter = DecisionsAdapter(None)
        world.store.ctx.decisions = adapter
    assert adapter._rows_strict() == adapter._rows()
    world.store.claim(world.p("l1main"), "demo:A")
    assert world.store._item("demo:A")["state"] == "claimed"


def test_injected_rows_failure_propagates(scenario):
    world, path, adapter = scenario

    def unreadable():
        raise OSError("PRIVATE FILE CONTENT")

    injected = SimpleNamespace(_rows=unreadable, _path=path)
    with pytest.raises(src.DecisionsUnreadable):
        src.decision_book(injected, fresh=True)
    assert src.decision_book(injected)["rows"] == {}


def test_failed_guard_preserves_display_cache(scenario, monkeypatch):
    world, path, adapter = scenario
    book = src.decision_book(adapter)
    before = world.store.show(world.p("reader"), "demo:A")
    monkeypatch.setattr(type(path), "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(Refused):
        world.store.claim(world.p("l1main"), "demo:A")
    assert src.decision_book(adapter) is book
    assert world.store.show(world.p("reader"), "demo:A") == before


@pytest.mark.parametrize("bad", [b"\xff", None])
def test_display_unreadable_matches_unconfigured(scenario, bad):
    world, path, adapter = scenario
    path.unlink()
    if bad is not None:
        path.write_bytes(bad)
    before = world.store.show(world.p("reader"), "demo:A")
    world.store.ctx.decisions = DecisionsAdapter(None)
    assert world.store.show(world.p("reader"), "demo:A") == before


@pytest.mark.parametrize("error", [OSError("task input unavailable"),
                                  UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")])
@pytest.mark.parametrize("force", [None, "checked"])
def test_non_decisions_input_failure_propagates(scenario, monkeypatch, error, force):
    world, path, adapter = scenario

    def unreadable():
        raise error

    monkeypatch.setattr(world.store, "_decision_clock", unreadable)
    with pytest.raises(type(error)) as exc:
        world.store._claim_guard(world.store._item("demo:A"), force)
    assert exc.value is error
    assert world.store._item("demo:A")["state"] == "open"


def test_claim_non_decisions_oserror_is_not_refused(scenario, monkeypatch):
    world, path, adapter = scenario
    error = OSError("task clock unavailable")

    def unavailable():
        raise error

    monkeypatch.setattr(world.store, "_decision_clock", unavailable)
    with pytest.raises(OSError) as exc:
        world.store.claim(world.p("l1main"), "demo:A", force_stale="checked")
    assert exc.value is error
    assert not isinstance(exc.value, Refused)
