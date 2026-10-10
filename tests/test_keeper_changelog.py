"""PG-CHANGELOG-GEN: ``pg changelog --since <date|seq>`` from events + decisions.

Mutations killed: --since seq not exclusive / date not inclusive; entries out of seq order;
unstable output (set/dict order, now()-dependent text); a close without its decision text;
restarts (outage_recorded) missing; creates/cancels/answers missing; a malformed --since
accepted; a reader without a project scope refused; HTTP/CLI doors dropping the filter.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from door_util import Doors
from keeper_util import World, defn, to_done
from plangraph import keeper  # noqa: F401
from plangraph.keeper import changelog
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_decisions import DecisionsAdapter

DAY = 86400


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    path = tmp_path / "DECISIONS.md"
    path.write_text("| ID | Note | Scope |\n|---|---|---|\n"
                    "| D-9 | answer row | Blue option chosen. Closes: demo:Q |\n",
                    encoding="utf-8")
    world.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(path))
    store, router = world.store, world.p("router")
    store.create_items(router, [
        defn("A"), defn("X"), defn("Q", kind="question", operator=True, wait="answer"),
        defn("G", ["A", "X", "Q"], kind="goal")])             # day 0: 4 creates
    world.clock.advance(DAY)
    to_done(world, "demo:A")                                   # day 1: review close
    store.answer(router, "demo:Q", "blue", "D-9")              # day 1: answer
    world.clock.advance(DAY)
    store.record_outage(world.p("system"), 120, world.clock() - 300, world.clock() - 180)
    store.cancel(router, "demo:X", "not needed")               # day 2
    yield world
    world.close()


def _text(w, since, who="router"):
    return changelog.render(w.store.changelog(w.p(who), since))


def test_full_log_lists_every_kind_in_seq_order(w):
    data = w.store.changelog(w.p("router"), "0")
    seqs = [e["seq"] for e in data["entries"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    types = [e["type"] for e in data["entries"]]
    assert types.count("created") == 4
    for kind in ("closed", "answered", "cancelled", "restart"):
        assert types.count(kind) == 1, kind
    assert data["head_seq"] >= seqs[-1]


def test_text_is_stable_and_has_the_expected_lines(w):
    first = _text(w, "0")
    assert first == _text(w, "0")                      # byte-identical across calls
    w.clock.advance(DAY * 30)                          # the clock must not leak into the text
    assert first == _text(w, "0")
    lines = first.splitlines()
    assert lines[0].startswith("changelog since seq 0 | head #")
    assert "## 2023-11-14" in lines and "## 2023-11-15" in lines and "## 2023-11-16" in lines
    body = "\n".join(lines)
    assert "created   demo:A | title A | by router" in body
    assert "closed    demo:A | title A | review by reviewer" in body
    assert ("answered  demo:Q | title Q | D-9: Blue option chosen. Closes: demo:Q") in body
    assert "cancelled demo:X | title X | not needed" in body
    assert "restart   keeper | down 120s | leases 0, tokens 0" in body
    assert lines[-1] == "totals: created 4, closed 1, answered 1, cancelled 1, restarts 1"


def test_since_seq_is_exclusive(w):
    all_entries = w.store.changelog(w.p("router"), "0")["entries"]
    cut = all_entries[3]["seq"]
    part = w.store.changelog(w.p("router"), str(cut))["entries"]
    assert [e["seq"] for e in part] == [e["seq"] for e in all_entries if e["seq"] > cut]
    assert w.store.changelog(w.p("router"), str(all_entries[-1]["seq"]))["entries"] == []
    assert _text(w, str(all_entries[-1]["seq"])).splitlines()[-1] == "no changes"


def test_since_date_is_inclusive_and_accepts_iso_datetimes(w):
    on_day1 = w.store.changelog(w.p("router"), "2023-11-15")["entries"]
    assert {e["type"] for e in on_day1} == {"closed", "answered", "cancelled", "restart"}
    assert all(e["ts"] >= "2023-11-15" for e in on_day1)
    later = w.store.changelog(w.p("router"), "2023-11-16")["entries"]
    assert {e["type"] for e in later} == {"cancelled", "restart"}
    exact = w.store.changelog(w.p("router"), "2023-11-14T22:13:20Z")["entries"]
    assert len(exact) == len(w.store.changelog(w.p("router"), "2023-11-14")["entries"])
    after = w.store.changelog(w.p("router"), "2023-11-14T22:13:21Z")["entries"]
    assert after and "created" not in {e["type"] for e in after}   # the creates happened at :20


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2023-13-40", "-5", "1.5", "12abc",
                                 "9" * 5000, "1" * 16, True, -1, 10 ** 15])
def test_malformed_since_is_refused(w, bad):
    with pytest.raises(Refused) as exc:
        w.store.changelog(w.p("router"), bad)
    assert exc.value.code == "validation"


def test_since_edges(w):
    assert w.store.changelog(w.p("router"), 0)["since"] == "seq 0"                # an int is a seq
    assert w.store.changelog(w.p("router"), "2023-11-15T00:00:00")["entries"]    # tz-naive = UTC
    assert w.store.changelog(w.p("router"), "9" * 15)["entries"] == []
    assert w.store.changelog(w.p("router"), "  3 ")["since"] == "seq 3"


def test_run_tokens_may_not_reach_the_read():
    from plangraph.keeper import api_routes
    with pytest.raises(Refused) as exc:
        api_routes.run_gate(SimpleNamespace(token_id=7), "changelog")
    assert exc.value.code == "forbidden"


def test_an_unreadable_decisions_file_leaves_ids_bare(w):
    class Broken:
        def text(self, decision_id):
            raise OSError("gone")
    w.store.ctx = SimpleNamespace(decisions=Broken())
    assert "answered  demo:Q | title Q | D-9\n" in _text(w, "0") + "\n"


def test_any_authenticated_role_may_read(w):
    for who in ("reader", "l1main", "workerA"):
        assert w.store.changelog(w.p(who), "0")["entries"]


def test_decision_text_missing_from_the_file_still_renders(w):
    w.store.ctx = SimpleNamespace(decisions=None)
    out = _text(w, "0")
    assert "answered  demo:Q | title Q | D-9" in out


def test_titles_and_keys_cannot_inject_lines():
    ev = [{"seq": 1, "ts": "2026-10-10T08:00:00Z", "actor": "r\nrestart", "key": "p:A",
           "type": "item_created", "payload": {}}]
    evil = "A\nrestart   keeper | down 0s | leases 0, tokens 0"
    text = changelog.render(changelog.build(ev, "0", {"p:A": evil}, {}, 1))
    assert len(text.splitlines()) == 4                 # header, day, one entry, totals
    assert "restarts 0" in text


def test_answered_without_a_decision_id_and_keyless_events_do_not_crash():
    ev = [{"seq": 1, "ts": "2026-01-01T00:00:00Z", "actor": "r", "key": "p:Q",
           "type": "answered", "payload": {}},
          {"seq": 2, "ts": "2026-01-01T00:00:01Z", "actor": "system", "key": None,
           "type": "item_created", "payload": {}},
          {"seq": 3, "ts": "2026-01-01T00:00:02Z", "actor": "r", "key": None,
           "type": "cancelled", "payload": {}}]
    data = changelog.build(ev, "0", {}, {}, 3)
    assert [e["type"] for e in data["entries"]] == ["answered"]
    assert "answered  p:Q |  | by r" in changelog.render(data)


def test_closed_on_decision_entry_lists_decision_ids_and_text():
    events = [
        {"seq": 7, "ts": "2026-10-10T08:00:00Z", "actor": "router", "key": "p:B",
         "type": "closed_on_decision",
         "payload": {"decision_ids": ["D-3", "D-4"], "batch_ref": "x@y", "sha256": "z"}}]
    data = changelog.build(events, "0", {"p:B": "Bee"}, {"D-3": "three", "D-4": "four"}, 7)
    assert data["entries"][0] == {
        "seq": 7, "ts": "2026-10-10T08:00:00Z", "type": "closed", "key": "p:B",
        "title": "Bee", "by": "router", "via": "decision",
        "decisions": [{"id": "D-3", "text": "three"}, {"id": "D-4", "text": "four"}]}
    assert "closed    p:B | Bee | decision D-3: three; D-4: four" in changelog.render(data)


def test_long_decision_text_is_cut_and_single_line():
    events = [{"seq": 1, "ts": "2026-10-10T08:00:00Z", "actor": "r", "key": "p:B",
               "type": "answered", "payload": {"text": "t", "decision_id": "D-1"}}]
    data = changelog.build(events, "0", {}, {"D-1": "x\ny " * 80}, 1)
    line = [ln for ln in changelog.render(data).splitlines() if "answered" in ln][0]
    assert "\n" not in line and len(line) < 220 and line.endswith("…")
    assert line.endswith("D-1: " + " ".join(("x y " * 80).split())[:100].rstrip() + "…")   # the 100-char cap


# ------------------------------------------------------------------ doors
@pytest.fixture
def doors(w, tmp_path):
    d = Doors(w, tmp_path)
    yield d
    d.close()


def test_http_door_text_and_json(w, doors):
    status, text = doors.http("reader", "GET", "/v1/changelog?since=2023-11-16")
    assert status == 200 and "cancelled demo:X" in text and "demo:A" not in text
    status, data = doors.http("reader", "GET", "/v1/changelog?since=2023-11-16&fmt=json")
    assert status == 200 and {e["type"] for e in data["entries"]} == {"cancelled", "restart"}
    status, _ = doors.http("reader", "GET", "/v1/changelog")
    assert status == 400
    status, _ = doors.http("reader", "GET", "/v1/changelog?since=junk")
    assert status == 400


def test_cli_door(w, doors):
    code, out, err = doors.pg("reader", "changelog", "--since", "2023-11-16")
    assert code == 0, err
    assert out.splitlines()[0].startswith("changelog since 2023-11-16 | head #")
    assert "cancelled demo:X | title X | not needed" in out
    assert out == _text(w, "2023-11-16") + "\n"
    code, out, _ = doors.pg("reader", "changelog", "--since", "0", json_out=True)
    assert code == 0 and '"entries"' in out
    code, _, err = doors.pg("reader", "changelog", "--since", "junk")
    assert code != 0
