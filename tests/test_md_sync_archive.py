"""PG-RF-ARCHIVE-ANSWERS: QUESTIONS.md generated from the keeper's ``needs_you``.

Answers live in the keeper (Q9).  ``md_sync_archive.questions_md`` renders the open
operator items (choices included) as the priority table ``md_sync_parse`` already
reads; ``md_sync --archive-answers`` (default OFF) writes it instead of importing.

Mutations killed: choices dropped or not round-tripping through ``parse_questions``;
unstable bytes (order, timestamps); ``|`` / newline / credential text breaking the table;
the default (no flag) run changing; --dry-run writing the file; a non-atomic or partial
write; answered/cancelled items listed; other projects listed.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync  # noqa: E402
import md_sync_archive as arch  # noqa: E402
import md_sync_parse as mp  # noqa: E402
import md_sync_plan as mplan  # noqa: E402

from keeper_util import World, defn  # noqa: E402
from plangraph import views  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402

CHOICES = {"options": [{"id": "enable", "label": "enable", "consequence": "mail goes out as you"},
                       {"id": "stay-disabled", "label": "stay disabled", "consequence": "nothing changes"}],
           "default": "stay-disabled", "deadline": "2026-10-12", "blocks": "mail"}


def row(key, title="Q: Send mail?", kind="question", wait="answer", **kw):
    base = {"key": key, "project": key.split(":")[0], "title": title, "kind": kind,
            "state": "open", "wait": wait, "priority": 0, "unblocks": 0, "choices": None}
    base.update(kw)
    return base


# ------------------------------------------------------------- generator
def test_roundtrips_through_the_md_sync_parser():
    rows = [row("routing:OS-12", "OP: Renew the certificate", "operator_task", "task", unblocks=3),
            row("routing:Q-MAIL", "Q: Send mail?", choices=CHOICES, unblocks=1),
            row("routing:Q-PLAIN", "Q: Pick a name?")]
    text = arch.questions_md(rows, {"routing:OS-12": "Run the renewal script on the box",
                                    "routing:Q-MAIL": "Needs your Gmail"})
    parsed = {q.id: q for q in mp.parse_questions(text)}
    assert set(parsed) == {"OS-12", "Q-MAIL", "Q-PLAIN"}
    assert parsed["OS-12"].wait == "task" and parsed["Q-MAIL"].wait == "answer"
    assert parsed["Q-MAIL"].title == "Q: Send mail?"
    assert parsed["OS-12"].title == "OP: Renew the certificate"
    assert "Run the renewal script on the box" in parsed["OS-12"].description
    got = parsed["Q-MAIL"].choices
    assert [(o["label"], o.get("consequence")) for o in got["options"]] == [
        ("enable", "mail goes out as you"), ("stay disabled", "nothing changes")]
    assert got["default"] == "stay-disabled" and got["deadline"] == "2026-10-12"
    assert parsed["Q-PLAIN"].choices is None
    assert mp.unmatched_rows(text) == []


def test_output_is_stable_ordered_and_clock_free():
    rows = [row("routing:Q-B"), row("routing:Q-A", unblocks=2)]
    first = arch.questions_md(rows, {})
    assert first == arch.questions_md(list(rows), {})
    ids = [ln.split("|")[2].strip() for ln in first.splitlines()
           if ln.startswith("| ") and "Q-" in ln]
    assert ids == ["Q-B", "Q-A"]                        # the caller's (needs_you) order is kept
    assert "20" not in first.split("\n")[0]             # no timestamp in the header
    assert first.endswith("\n") and "\r" not in first


def test_empty_list_still_a_valid_file():
    text = arch.questions_md([], {})
    assert "no open operator items" in text and mp.parse_questions(text) == []


def test_cells_are_safe_one_line_and_credential_free():
    rows = [row("routing:Q-X", "Q: a | b\nc"),
            row("routing:Q-Y", choices={"options": [
                {"id": "a", "label": "x; y = z", "consequence": "p; q"}], "default": None,
                "deadline": None, "blocks": None})]
    hexrun = "0123456789abcdef" * 3                     # 48 hex: what md_sync_parse.redact masks
    text = arch.questions_md(rows, {"routing:Q-X": f"token {hexrun}\nsecond line | pipe"})
    assert hexrun not in text and "[redacted]" in text
    for line in text.splitlines():
        if "Q-X" in line:
            assert line.count("|") == 7                  # 6 cells, no stray pipes
    parsed = {q.id: q for q in mp.parse_questions(text)}
    assert parsed["Q-X"].title.startswith("Q: a ") and "\n" not in parsed["Q-X"].title
    opts = parsed["Q-Y"].choices["options"]
    assert len(opts) == 1 and ";" not in opts[0]["label"] and "=" not in opts[0]["label"]


def test_choice_text_is_sanitised_deadline_and_star_labels():
    hexrun = "0123456789abcdef" * 3
    rows = [row("routing:Q-Z", choices={"options": [
        {"id": "a", "label": "*urgent", "consequence": "x"},
        {"id": "b", "label": "later", "consequence": "y"}], "default": "b",
        "deadline": f"2026-01-01 | leak\n{hexrun}", "blocks": None})]
    text = arch.questions_md(rows, {})
    assert hexrun not in text and "leak" not in text and text.count("\n") == len(text.splitlines())
    got = {q.id: q for q in mp.parse_questions(text)}["Q-Z"].choices
    assert got["default"] == "b" and got["deadline"] is None
    assert [(o["id"], o["label"]) for o in got["options"]] == [("a", "urgent"), ("b", "later")]
    ok = arch.questions_md([row("routing:Q-Z", choices={**CHOICES, "deadline": "2026-02-03"})], {})
    assert "Deadline: 2026-02-03" in ok


def test_decision_kind_and_custom_option_ids_round_trip_losslessly():
    """W1: DECISION kind + explicit ``{id}`` survive questions_md -> parse_questions."""
    choices = {"options": [{"id": "keep", "label": "Keep the Shelfmark lane",
                             "consequence": "the lane stays"},
                            {"id": "drop-shelf", "label": "Drop shelf",
                             "consequence": "gone"}],
               "default": "keep", "deadline": "2026-10-12", "blocks": None}
    rows = [row("routing:Q-D", "Q: pick", kind="decision", wait="answer",
                choices=choices),
            row("routing:Q-MAIL", "Q: Send mail?", choices=CHOICES)]
    text = arch.questions_md(rows, {})
    assert "| DECISION |" in text and "| ANSWER |" in text
    assert "Keep the Shelfmark lane {keep}" in text
    assert "{drop-shelf}" not in text          # a slug id needs no braces
    got = {q.id: q for q in mp.parse_questions(text)}
    assert got["Q-D"].kind == "decision" and got["Q-D"].wait == "answer"
    assert got["Q-MAIL"].kind == "question" and got["Q-MAIL"].wait == "answer"
    dec = got["Q-D"].choices
    assert [(o["id"], o["label"], o.get("consequence")) for o in dec["options"]] == [
        ("keep", "Keep the Shelfmark lane", "the lane stays"),
        ("drop-shelf", "Drop shelf", "gone")]
    assert dec["default"] == "keep" and dec["deadline"] == "2026-10-12"
    mail = got["Q-MAIL"].choices
    assert [(o["id"], o["label"]) for o in mail["options"]] == [
        ("enable", "enable"), ("stay-disabled", "stay disabled")]
    assert mail["default"] == "stay-disabled" and mail["deadline"] == "2026-10-12"


def test_generated_file_read_back_plans_no_choices_edits():
    """W1: a generated file read back plans no choices edits on unchanged items."""
    choices = {"options": [{"id": "keep", "label": "Keep the Shelfmark lane",
                             "consequence": "the lane stays"}],
               "default": "keep", "deadline": None, "blocks": "pick"}
    rows = [row("routing:Q-D", "Q: pick", kind="decision", wait="answer",
                choices=choices)]
    text = arch.questions_md(rows, {})
    (entry,) = mp.parse_questions(text)
    assert entry.choices["options"][0]["id"] == "keep"  # was slugged to keep-the-shelfmark-lane
    probe = mplan.Plan()
    live = {"project": "routing", "id": "Q-D", "key": "routing:Q-D", "state": "open",
            "kind": "decision", "title": entry.title, "description": entry.description,
            "wait": entry.wait, "source": "md-sync", "depends_on": [], "evidence": [],
            "choices": mplan._masked_choices(dict(choices), probe)}
    goal = {"project": "routing", "id": "GOAL", "key": "routing:GOAL", "state": "open",
            "kind": "goal", "depends_on": ["routing:PH-OPERATOR"], "evidence": []}
    gate = {"project": "routing", "id": "PH-OPERATOR", "key": "routing:PH-OPERATOR",
            "state": "open", "kind": "gate", "depends_on": ["routing:Q-D"],
            "evidence": []}
    planned = mplan.build_plan([entry], [], [], set(), [goal, gate, live])
    assert [o for o in planned.ops if o.get("op") == "edit"
            and "choices" in o.get("fields", {})] == []


def test_non_matching_ids_are_listed_but_flagged():
    text = arch.questions_md([row("routing:weird id")], {})
    assert "weird id" in text


# --------------------------------------------------- md_sync --archive-answers
@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    r = w.p("router")
    w.store.create_items(r, [
        defn("Q-OPEN", project="routing", kind="question", operator=True, wait="answer",
             title="Q: Send mail?", description="Needs the Gmail account", priority=2),
        defn("OS-1", project="routing", kind="operator_task", operator=True, wait="task",
             title="OP: Renew cert"),
        defn("Q-DONE", project="routing", kind="question", operator=True, wait="answer",
             title="Q: old"),
        defn("Q-OTHER", project="server", kind="question", operator=True, wait="answer"),
        defn("LANE", project="routing"),
        defn("GOAL", project="routing", kind="goal",
             deps=["routing:Q-OPEN", "routing:OS-1", "routing:Q-DONE", "routing:LANE",
                   "server:Q-OTHER"])])
    w.store.edit_definition(r, "routing:Q-OPEN", {"choices": CHOICES})
    w.store.cancel(r, "routing:Q-DONE", "withdrawn")
    yield w
    w.close()


@pytest.fixture
def srv(world, tmp_path):
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(world.path), "bind": "127.0.0.1:0",
                               "snapshot": str(tmp_path / "plan.snapshot.json")}))
    os.chmod(cfg, 0o600)
    server = api.make_server(world.store, config.load_config(cfg), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def token(world, tmp_path):
    path = tmp_path / "router.token"
    path.write_text(world.tokens["router"], encoding="utf-8")
    return str(path)


def archive(srv, token, out_path, *extra):
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers", "--questions-out", str(out_path),
                       "--url", srv.base_url, "--token-file", token, *extra],
                      out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_apply_writes_the_open_operator_items_of_the_project(srv, token, tmp_path):
    target = tmp_path / "QUESTIONS.md"
    rc, out, err = archive(srv, token, target, "--apply")
    assert rc == 0, err
    parsed = {q.id: q for q in mp.parse_questions(target.read_text(encoding="utf-8"))}
    assert set(parsed) == {"Q-OPEN", "OS-1"}             # not Q-DONE, Q-OTHER (project), LANE
    assert parsed["Q-OPEN"].choices["default"] == "stay-disabled"
    assert "Needs the Gmail account" in parsed["Q-OPEN"].description
    assert "wrote 2 open items" in out
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []   # no temp left


def test_dry_run_is_the_default_and_writes_nothing(srv, token, tmp_path):
    target = tmp_path / "QUESTIONS.md"
    rc, out, err = archive(srv, token, target)
    assert rc == 0 and not target.exists() and "dry run" in out and "2 open items" in out
    target.write_text("old", encoding="utf-8")
    archive(srv, token, target, "--dry-run")
    assert target.read_text(encoding="utf-8") == "old"


def test_project_flag_and_identical_rewrite(srv, token, tmp_path):
    target = tmp_path / "Q.md"
    assert archive(srv, token, target, "--apply", "--project", "server")[0] == 0
    assert {q.id for q in mp.parse_questions(target.read_text(encoding="utf-8"))} == {"Q-OTHER"}
    before = target.read_bytes()
    assert archive(srv, token, target, "--apply", "--project", "server")[0] == 0
    assert target.read_bytes() == before


def test_status_file_records_the_archive_run(srv, token, tmp_path):
    status = tmp_path / "status.json"
    target = tmp_path / "QUESTIONS.md"
    assert archive(srv, token, target, "--apply", "--status-file", str(status))[0] == 0
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert doc["mode"] == "archive" and doc["archived"] == 2 and doc["written"] is True
    assert doc["error"] is None and doc["ts"]


def test_unreachable_keeper_leaves_the_file_alone_and_reports(tmp_path, monkeypatch):
    target = tmp_path / "QUESTIONS.md"
    target.write_text("keep me", encoding="utf-8")
    tok = tmp_path / "t"
    tok.write_text("x" * 20, encoding="utf-8")
    os.chmod(tok, 0o600)
    status = tmp_path / "status.json"
    def down(self, verb):
        raise md_sync.apply_batch.ApiError("unreachable", "URLError")
    monkeypatch.setattr(md_sync.apply_batch.Session, "get", down)   # no socket, no port race
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers", "--questions-out", str(target), "--apply",
                       "--url", "http://keeper.invalid", "--token-file", str(tok),
                       "--status-file", str(status)], out=out, err=err)
    assert rc == 2 and target.read_text(encoding="utf-8") == "keep me"
    assert json.loads(status.read_text(encoding="utf-8"))["error"].startswith("error: cannot read")


def test_questions_out_is_required_with_the_flag(srv, token):
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers", "--url", srv.base_url, "--token-file", token],
                      out=out, err=err)
    assert rc == 2 and "questions-out" in err.getvalue()


def test_default_run_is_unchanged_without_the_flag(srv, token, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--url", srv.base_url, "--token-file", token], out=out, err=err)
    assert rc == 2 and md_sync.USAGE in err.getvalue()   # the three inputs are still required


def test_views_needs_you_is_the_source_of_truth(world):
    items = world.store.snapshot(world.p("router"))["items"]
    keys = [r["key"] for r in views.needs_you(items, project="routing")]
    assert keys and "routing:Q-DONE" not in keys
