"""QA-IMPORT (D-865): operator questions carry structured choices; every QUESTIONS row is named.

Mutations killed: a malformed Options block dropping the operator's words, a default that is not
an option, choices on a lane, unescaped or secret-like option text accepted, an unmatched row
silently ignored, md-sync re-editing an unchanged item, the fold disagreeing with the cache.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync  # noqa: E402
import md_sync_parse as mp  # noqa: E402
from keeper_util import World, defn  # noqa: E402
from plangraph import views  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402
from plangraph.keeper.errors import Refused  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402
from plangraph.keeper.item_fields import check_choices  # noqa: E402

STEP = ("say `enable` after reviewing Options: enable = mail goes out as your Gmail; "
        "own sender = needs a new mailbox; *stay disabled = nothing changes. Deadline: 2026-10-12")


# ------------------------------------------------------------------ parser
def test_parse_options_full():
    rest, ch = mp.parse_options(STEP, "great-wiki digest mail")
    assert rest == "say `enable` after reviewing"
    assert [o["id"] for o in ch["options"]] == ["enable", "own-sender", "stay-disabled"]
    assert ch["default"] == "stay-disabled" and ch["deadline"] == "2026-10-12"
    assert ch["options"][1]["consequence"] == "needs a new mailbox"
    assert ch["blocks"] == "great-wiki digest mail"


@pytest.mark.parametrize("bad", [
    "Options: a = x; ; b = y", "Options: *a; *b", "Options: a; a = dup",
    "Options: " + "; ".join(f"o{i}" for i in range(9)), "Options: " + "x" * 61,
    "Options: a = bad\x01; b", "Options: a = x. Deadline: tomorrow",
    "Options: a = x. Deadline: 2026-13-40", "Options: a = del\x7f",
    "Options: a = x. deadline : tomorrow", "review Options: old; Options: a = x; *b",
    "no block here"])
def test_malformed_block_keeps_the_text_and_yields_no_choices(bad):
    rest, ch = mp.parse_options(bad)
    assert ch is None and rest == bad


def test_unmatched_rows_are_named():
    text = ("| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | OS-1 | TASK | a | b | c |\n"
            "| 1 | prox-L1-main approval (verify) | ANSWER | a | b | c |\n"
            "| 2 | OS-5, OS-6 | TASK | a | b | c |\n")
    assert mp.unmatched_rows(text) == ["prox-L1-main approval (verify)", "OS-5, OS-6"]


def test_table_row_carries_choices():
    text = ("| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
            f"| 3 | Q-MAIL | ANSWER | great-wiki digest mail | {STEP} | any session |\n")
    (q,) = mp.parse_questions(text)
    assert q.choices["default"] == "stay-disabled"
    assert "Options:" not in q.description


# ------------------------------------------------------------------ keeper
def choices(**kw):
    value = {"options": [{"id": "a", "label": "A", "consequence": "does a"},
                         {"id": "b", "label": "B"}], "default": "b", "deadline": "2026-10-12",
             "blocks": "x"}
    value.update(kw)
    return value


@pytest.mark.parametrize("bad", [
    {"default": "zzz"}, {"default": []}, {"default": 1}, {"options": 0}, {"options": ""},
    {"options": [{"id": "a", "label": "a\x7f"}]}, {"options": [{"id": "a", "label": "a\x85"}]}, {"options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
    {"options": [{"id": "A B", "label": "A"}]}, {"options": [{"id": "a", "label": "x" * 61}]},
    {"options": [{"id": "a", "label": "line\nbreak"}]}, {"deadline": "2026-13-40"},
    {"deadline": "tomorrow"}, {"extra": 1},
    {"options": [{"id": "a", "label": "A", "html": "<b>"}]},
    {"options": [{"id": f"o{i}", "label": "x"} for i in range(9)], "default": None},
    {"blocks": "log" + "in admin/" + "hunter2" + "secret"},
    {"options": [{"id": "log" + "in-admin-x", "label": "pass" + "word abc/defghij1"}]}])
def test_check_choices_refuses(bad):
    with pytest.raises(Refused):
        check_choices(choices(**bad), "question")


def test_choices_only_on_operator_kinds():
    with pytest.raises(Refused, match="only on"):
        check_choices(choices(), "lane")
    assert json.loads(check_choices(choices(), "operator_task"))["default"] == "b"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def test_keeper_stores_shows_edits_and_folds_choices(w):
    r = w.p("router")
    w.store.create_items(r, [
        defn("Q-1", project="routing", kind="question", choices=choices()),
        defn("GOAL", project="routing", kind="goal", deps=["routing:Q-1"])])
    shown = w.store.show(r, "routing:Q-1")
    assert shown["choices"]["default"] == "b"
    snap = {i["key"]: i for i in w.store.snapshot(r)["items"]}
    assert snap["routing:Q-1"]["choices"]["options"][0]["consequence"] == "does a"
    w.store.edit_definition(r, "routing:Q-1", {"choices": choices(default="a")})
    assert w.store.show(r, "routing:Q-1")["choices"]["default"] == "a"
    w.store.edit_definition(r, "routing:Q-1", {"choices": None})
    assert w.store.show(r, "routing:Q-1")["choices"] is None
    assert w.store.verify_integrity() == []
    with pytest.raises(Refused):
        w.store.create_items(r, [defn("L-1", project="routing", choices=choices(),
                                      deps=[])])


def test_needs_you_carries_choices(w):
    r = w.p("router")
    w.store.create_items(r, [
        defn("Q-1", project="routing", kind="question", choices=choices()),
        defn("GOAL", project="routing", kind="goal", deps=["routing:Q-1"])])
    rows = {row["key"]: row for row in w.store.needs_you(r)}
    assert rows["routing:Q-1"]["choices"]["default"] == "b"
    snap_rows = views.needs_you(w.store.snapshot(r)["items"])
    assert {row["key"]: row for row in snap_rows}["routing:Q-1"]["choices"]["deadline"] == "2026-10-12"


# ---------------------------------------------------------------- md-sync e2e
FIX = ROOT / "tests" / "fixtures" / "md_sync"


@pytest.fixture
def site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    shutil.copy(FIX / "sample_answers.md", site / "ANSWERS.md")
    shutil.copy(FIX / "decisions-log.md", site / "decisions-log.md")
    (site / "QUESTIONS.md").write_text(
        "# Q\n\n| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
        f"| 3 | Q-MAIL | ANSWER | great-wiki digest mail | {STEP} | any session |\n"
        "| 1 | some row (verify) | TASK | a | b | c |\n")
    return site


@pytest.fixture
def srv(w, tmp_path, site):
    r = w.p("router")
    w.store.create_items(r, [defn("R-1", project="routing"),
                             defn("GOAL", project="routing", kind="goal", deps=["routing:R-1"])])
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(site / "decisions-log.md"))
    w.store.batch_roots = [str(site)]
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(w.path), "bind": "127.0.0.1:0",
                               "snapshot": str(tmp_path / "plan.snapshot.json")}))
    os.chmod(cfg, 0o600)
    server = api.make_server(w.store, config.load_config(cfg), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def sync(site, srv, w, tmp_path, *extra):
    token = tmp_path / "router.token"
    token.write_text(w.tokens["router"], encoding="utf-8")
    status = tmp_path / "status.json"
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(site / "QUESTIONS.md"), "--answers", str(site / "ANSWERS.md"),
                       "--decisions", str(site / "decisions-log.md"), "--url", srv.base_url,
                       "--token-file", str(token), "--status-file", str(status), *extra],
                      out=out, err=err)
    return rc, out.getvalue(), json.loads(status.read_text())


def test_md_sync_imports_choices_idempotently_and_names_unmatched_rows(w, srv, site, tmp_path):
    rc, out, status = sync(site, srv, w, tmp_path, "--apply")
    r = w.p("router")
    shown = w.store.show(r, "routing:Q-MAIL")
    assert shown["choices"]["default"] == "stay-disabled"
    assert status["unmatched"] == ["some row (verify)"]
    rc2, out2, _ = sync(site, srv, w, tmp_path, "--dry-run")
    assert "Q-MAIL" not in out2                                  # unchanged: no edit planned
    text = (site / "QUESTIONS.md").read_text().replace("*stay disabled", "stay disabled") \
        .replace("own sender = needs", "*own sender = needs")
    (site / "QUESTIONS.md").write_text(text)
    sync(site, srv, w, tmp_path, "--apply")
    assert w.store.show(r, "routing:Q-MAIL")["choices"]["default"] == "own-sender"
    assert w.store.verify_integrity() == []


def test_variant_suffix_ids_are_matched_not_unmatched():
    """``OS-70 (3b)`` is a variant tag of OS-70 (documented md-sync rule), so it is not 'unmatched'."""
    text = ("| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | OS-1 (verify) | TASK | a | b | c |\n")
    assert mp.unmatched_rows(text) == []


def test_control_chars_in_the_why_cell_drop_blocks_not_choices():
    rest, ch = mp.parse_options("Options: a = x; *b", "why\x01")
    assert ch is not None and ch["blocks"] is None and ch["default"] == "b"


def test_variant_tag_with_extra_text_is_unmatched():
    text = ("| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 | OS-1 (verify) extra | TASK | a | b | c |\n")
    assert mp.unmatched_rows(text) == ["OS-1 (verify) extra"]


def test_masked_choices_renames_a_credential_shaped_id():
    import md_sync_plan as mplan
    plan = mplan.Plan()
    secret = "log" + "in admin/hunter2secret"
    out = mplan._masked_choices({"options": [{"id": "x/abcdefgh", "label": secret}],
                                 "default": "x/abcdefgh", "deadline": None, "blocks": None}, plan)
    assert out["options"][0]["id"] == "masked-1" and out["default"] == "masked-1"
    assert "hunter2" not in json.dumps(out)


def test_deadline_keyword_is_case_insensitive():
    rest, ch = mp.parse_options("Options: a = x; *b. deadline: 2026-10-12")
    assert ch["deadline"] == "2026-10-12" and ch["default"] == "b"


def test_empty_id_cell_is_unmatched():
    text = ("| Prio | ID | Kind | why | step | where |\n|---|---|---|---|---|---|\n"
            "| 1 |  | TASK | a | b | c |\n")
    assert mp.unmatched_rows(text) == ["(empty id cell)"]


def test_masked_id_never_collides_with_a_real_id():
    import md_sync_plan as mplan
    plan = mplan.Plan()
    secret = "log" + "in admin/hunter2secret"
    out = mplan._masked_choices({"options": [{"id": "x", "label": secret}, {"id": "masked-1", "label": "masked 1"}],
                                 "default": "x", "deadline": None, "blocks": None}, plan)
    ids = [o["id"] for o in out["options"]]
    assert len(ids) == len(set(ids)) and out["default"] == ids[0] != "masked-1"


def test_unmatched_cells_are_redacted_before_truncation(w, srv, site, tmp_path):
    long_secret = "ab" * 60                                   # a 120-char hex-ish run
    text = (site / "QUESTIONS.md").read_text().replace("some row (verify)", f"row {long_secret}")
    (site / "QUESTIONS.md").write_text(text)
    rc, out, status = sync(site, srv, w, tmp_path, "--dry-run")
    (cell,) = status["unmatched"]
    assert "abab" * 4 not in cell and len(cell) <= 80
