"""KEEPER-SNAP-S0-TIMINGS: the decisions file is parsed once per snapshot build.

``_cached_rows`` (stamped on ``(mtime_ns, size)``) backs the snapshot
``decision_text`` read path only; the write-path enforcement methods
(``has``/``text``/``waives``/``closes``) must re-read the file on every call,
so a missed same-size edit can never mis-authorise a close or a waiver.
"""
from __future__ import annotations

import os
from pathlib import Path

from keeper_snap_s0_util import DECISIONS_DOC, EXPECTED_TEXT, build, body
from plangraph.keeper.evidence_decisions import DecisionsAdapter, snapshot_texts

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "keeper_snapshot_s0_golden.json"


def _count_reads(monkeypatch):
    calls = []
    real = DecisionsAdapter._rows

    def counting(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(DecisionsAdapter, "_rows", counting)
    return calls


def _snap(w):
    return w.store.snapshot(w.p("router"))


# (a) decisions file parsed once per snapshot build
def test_decisions_file_parsed_once_per_snapshot_build(tmp_path, monkeypatch):
    w, adapter = build(tmp_path)
    calls = _count_reads(monkeypatch)
    snap = _snap(w)
    assert snap["decision_text"] == EXPECTED_TEXT      # 8 ids cited, texts exact
    assert len(calls) == 1                             # one parse, not one per id
    _snap(w)                                           # unchanged stamp: no re-parse
    assert len(calls) == 1


# (b) cache invalidated on mtime or size change
def test_cache_invalidated_on_mtime_or_size_change(tmp_path, monkeypatch):
    w, adapter = build(tmp_path)
    path = tmp_path / "decisions.md"
    calls = _count_reads(monkeypatch)
    assert _snap(w)["decision_text"]["D-210"] == "second row text for 210"
    assert len(calls) == 1
    st = path.stat()
    edited = DECISIONS_DOC.replace("second row text for 210",
                                   "second row text for 211")
    # same size, new mtime -> re-parse (the mtime is part of the key)
    path.write_text(edited)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    snap = _snap(w)
    assert snap["decision_text"]["D-210"] == "second row text for 211"
    assert len(calls) == 2
    # same (cached) mtime, new size -> re-parse (the size is part of the key):
    # only a cited id can show the re-parse, so change D-210's text by one char
    path.write_text(edited.replace("second row text for 211",
                                   "second row text for 2111")
                    + "| D-700 | demo | size changed text |\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    snap = _snap(w)
    assert snap["decision_text"]["D-210"] == "second row text for 2111"
    assert "D-700" not in snap["decision_text"]     # uncited ids never leak in
    assert len(calls) == 3


# (c) enforcement path (has/text via the startup adapter) re-reads after a same-size edit
def test_enforcement_re_reads_after_a_same_size_edit(tmp_path):
    w, adapter = build(tmp_path)
    path = tmp_path / "decisions.md"
    st = path.stat()
    _snap(w)                                           # warm the snapshot cache
    path.write_text(DECISIONS_DOC.replace("| D-209 | demo | sentinel decision text |",
                                          "| D-301 | demo | sentinel decision text |"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))   # same size AND same mtime
    # the snapshot cache is blind to that edit (documented: read path only) ...
    assert snapshot_texts(adapter, ["D-209"]) == {"D-209": "sentinel decision text"}
    # ... but enforcement never reads the cache: it re-reads every call
    assert adapter.has("D-209") is False
    assert adapter.has("D-301") is True
    assert adapter.text("D-209") is None
    assert adapter.text("D-301") == "sentinel decision text"
    assert adapter.closes("D-301") == set()


# (d) unfiltered snapshot body byte-identical to the pre-change code path
def test_unfiltered_snapshot_body_matches_the_pre_change_golden(tmp_path):
    w, _ = build(tmp_path)
    assert body(w) == GOLDEN.read_bytes()


def test_fake_adapter_keeps_the_per_id_text_loop():
    class Fake:
        def __init__(self):
            self.calls = []

        def text(self, did):
            self.calls.append(did)
            return f"text {did}"

    fake = Fake()
    assert snapshot_texts(fake, ["D-1", "x", "D-1"]) == {
        "D-1": "text D-1", "x": "text x"}
    assert fake.calls == ["D-1", "x", "D-1"]

    class NoText:
        pass

    assert snapshot_texts(NoText(), ["D-1"]) == {}
