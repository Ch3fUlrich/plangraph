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
import stat
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


# ------------------------------------------------- seat3 (W3): one keyword table
import random as _random
import re as _re2

_PROP_SEED = 20261011
_PROP_N = 500


def _prop_mixed(word, rng):
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in word)


def _prop_pool(rng, keywords):
    """Fragments covering every keyword, case variants, braces, pipes, ';', '=', '*' and newlines."""
    pool = []
    for kw in keywords:
        if kw.endswith(":"):
            pool += [kw, kw.lower(), kw.upper(), _prop_mixed(kw, rng)]
            low = kw.lower()
            if low.startswith("deadline"):
                pool += [f"{kw} 2026-10-12", f"{_prop_mixed(kw, rng)} 2026-10-12"]
            elif low.startswith("parent"):
                pool += [f"{kw} PH-OPERATOR", f"{kw} routing:G1",
                         f"[{kw} foo]", f"{_prop_mixed(kw, rng)} routing:G1"]
            elif low.startswith("options"):
                pool += [f"{kw} a = b", f"{_prop_mixed(kw, rng)} x"]
        elif "{" in kw or "}" in kw:
            pool += ["{id}", "{x}", "{custom-id}", "{Keep}", "{a} {b}", "{", "}",
                     "label {id}", "Pick {x}", " {trail}", "[bracket]"]
        elif kw == "*":
            pool += ["*", "**", "*lead", "a*b", "* urgent", " *"]
        else:  # a future keyword: poison it literally so the test stays sensitive
            pool += [kw, kw.lower(), kw.upper()]
    pool += ["|", ";", "=", "\n", "alpha", "beta", "pick", "go", "fast",
             "win", "soon", "ok", "123"]
    return pool


def _prop_string(rng, pool, lo=1, hi=3):
    text = " ".join(rng.choice(pool) for _ in range(rng.randint(lo, hi)))
    return text + " ok" + rng.choice(["", ".", "..", "!", ":", " "])


def _prop_pad(text, want):
    """Pad *text* with plain spaced words to exactly *want* chars (never truncates).

    Chunks keep a space every third char, so no 40+-char alnum run forms and
    ``mp.redact`` leaves the padded length alone; the pad holds no keywords or
    separators, so it only exercises the length limits (60 for labels, 160 for
    consequences).
    """
    while len(text) < want:
        room = want - len(text)
        if room == 1:
            text += "q"
        elif room == 2:
            text += " w"
        else:
            text += " wq"
    return text


def _assert_property_roundtrip(n=_PROP_N, seed=_PROP_SEED, keywords=None):
    """Seeded questions_md -> parse_questions round trip over poisoned keeper rows.

    Every title/description/label/consequence is built from the structural
    keywords (any case), braces, pipes, ';', '=', '*' and newlines.  After the
    documented neutralisation (``arch._cell``/``arch._label``) the title, labels,
    consequences, option ids, default, deadline and kind round-trip exactly, no
    row yields a parent or a malformed (``None``) choices, and the file holds
    exactly one ``Options:`` block per row, one ``Deadline:`` per dated row and
    no ``Parent:`` token.
    """
    keywords = tuple(keywords) if keywords is not None else tuple(mp.STRUCTURAL_KEYWORDS)
    rng = _random.Random(seed)
    pool = _prop_pool(rng, keywords)
    rows, descriptions, meta = [], {}, []
    for idx in range(n):
        raw_title = _prop_string(rng, pool, 1, 3)
        raw_desc = _prop_string(rng, pool, 1, 4)
        opts = []
        for j in range(2):
            opts.append((f"opt-{idx}-{j}",
                         _prop_string(rng, pool, 1, 2),
                         _prop_string(rng, pool, 1, 3)))
        # Seat6 (d): also cover the parser's length limits. Every 4th row pads its
        # first label to 55-60 chars, every 5th row its second consequence to
        # 155-160. The transforms never lengthen (neutralisation is 1-for-1, the
        # pad forms no redactable runs), so the block must still parse.
        if idx % 4 == 0:
            oid, rl, rc = opts[0]
            opts[0] = (oid, _prop_pad(rl, 55 + (idx % 6)), rc)
        if idx % 5 == 0:
            oid, rl, rc = opts[1]
            opts[1] = (oid, rl, _prop_pad(rc, 155 + (idx % 6)))
        default = rng.choice([opts[0][0], opts[1][0], None])
        deadline = rng.choice(["2026-10-12", None])
        kind = rng.choice(["question", "decision"])
        key = f"routing:Q-PROP{idx:04d}"
        title = f"Q: {raw_title}"
        choices = {"options": [{"id": oid, "label": rl, "consequence": rc}
                               for oid, rl, rc in opts],
                   "default": default, "deadline": deadline, "blocks": None}
        rows.append(row(key, title, kind=kind, choices=choices))
        descriptions[key] = raw_desc
        meta.append((key, title, kind, raw_desc, opts, default, deadline))
    text = arch.questions_md(rows, descriptions)
    parsed = {q.id: q for q in mp.parse_questions(text)}
    assert len(parsed) == n
    assert mp.unmatched_rows(text) == []
    assert len(_re2.findall(r"(?i)(?:^|\s)Options:", text)) == n
    n_dated = sum(1 for m in meta if m[6] is not None)
    assert len(_re2.findall(r"(?i)deadline\s*:", text)) == n_dated
    assert _re2.search(r"(?i)(?:^|\s)Parent\s*:", text) is None
    for key, title, kind, raw_desc, opts, default, deadline in meta:
        qid = key.split(":", 1)[-1]
        got = parsed[qid]
        assert got.parent is None, f"{qid} extra parent {got.parent!r}"
        assert got.kind == kind, f"{qid} kind {got.kind!r} != {kind!r}"
        assert got.choices is not None, f"{qid} choices lost (extra structure?)"
        assert [o["id"] for o in got.choices["options"]] == [o[0] for o in opts]
        assert got.choices.get("default") == default, f"{qid} default"
        assert got.choices.get("deadline") == deadline, f"{qid} deadline"
        assert [o["label"] for o in got.choices["options"]] == [
            arch._label(rl) for _, rl, _ in opts], f"{qid} labels"
        assert [o.get("consequence", "") for o in got.choices["options"]] == [
            arch._cell(rc, consequence=True) for _, _, rc in opts], f"{qid} consequences"
        exp_title = "Q: " + mp._title_and_rest(arch._cell(title[3:]).strip())[0]
        assert got.title == exp_title, f"{qid} title {got.title!r} != {exp_title!r}"
        # the parser strips " .;:" between the description and the Options block,
        # so a description trailing in those chars round-trips stripped.
        exp_step = arch._cell(raw_desc).rstrip(" .;:")
        exp_desc = f"{exp_step} — {arch._cell(key)}" if exp_step else arch._cell(key)
        assert got.description == exp_desc, f"{qid} description"


def test_property_all_keywords_round_trip():
    """W3: 500 seeded poisoned rows survive questions_md -> parse_questions."""
    _assert_property_roundtrip()


def test_tuple_mutation_each_keyword_is_required(monkeypatch):
    """W3: dropping any one keyword from the tuple breaks the round trip."""
    original = tuple(mp.STRUCTURAL_KEYWORDS)
    assert len(original) >= 5  # Options:, Deadline:, Parent:, {id}, *
    for drop in original:
        monkeypatch.setattr(mp, "STRUCTURAL_KEYWORDS",
                            tuple(k for k in original if k != drop))
        with pytest.raises(AssertionError):
            _assert_property_roundtrip(keywords=original)


# ------------------------------------------------- seat2 (W2): cells
def test_literal_braces_in_keeper_label_round_trip():
    """V3: a keeper label ending in a literal ``{x}`` keeps its text (as parens) and its id."""
    choices = {"options": [
        {"id": "pick-x", "label": "Pick {x}", "consequence": "kept"},
        {"id": "other", "label": "Other {word} choice", "consequence": "gone"}],
        "default": "pick-x", "deadline": None, "blocks": None}
    text = arch.questions_md([row("routing:Q-B", choices=choices)], {})
    assert "Pick (x) {pick-x}" in text            # ALWAYS writes {id} when braces were present
    assert "Other (word) choice {other}" in text
    got = {q.id: q for q in mp.parse_questions(text)}["Q-B"].choices
    assert got is not None
    assert [(o["id"], o["label"], o.get("consequence")) for o in got["options"]] == [
        ("pick-x", "Pick (x)", "kept"), ("other", "Other (word) choice", "gone")]
    assert got["default"] == "pick-x"


def test_any_case_options_neutralised_in_every_cell():
    """V2: ``options:``/``OPTIONS:`` in title, description, label, consequence: one block only."""
    import re as _re
    for poison in ("options:", "OPTIONS:"):
        poisoned = {"options": [
            {"id": "a", "label": f"go {poison} fast", "consequence": f"win {poison} soon"}],
            "default": "a", "deadline": None, "blocks": None}
        text = arch.questions_md(
            [row("routing:Q-P", f"Q: decide {poison} now?", choices=poisoned)],
            {"routing:Q-P": f"pick it {poison} soon"})
        assert _re.findall(r"(?i)(?:^|\s)Options:", text) == [" Options:"], text  # only the real block
        assert arch._NEUTRAL_COLON in text  # keeper keywords neutralised length-preservingly
        got = {q.id: q for q in mp.parse_questions(text)}["Q-P"]
        assert got.choices is not None and len(got.choices["options"]) == 1
        opt = got.choices["options"][0]
        assert opt["id"] == "a" and arch._NEUTRAL_COLON in opt["label"] \
            and arch._NEUTRAL_COLON in opt["consequence"]
        assert arch._NEUTRAL_COLON in got.title and arch._NEUTRAL_COLON in got.description
        assert _re.search(r"(?i)options:", got.description) is None


def test_neutralisation_mutations_are_killed():
    """V5c: reverting either neutralisation (exact-case Options, or braces) fails."""
    import re as _re
    # V2 mutation: an exact-case-only ``.replace("Options:", ...)`` would leave these behind.
    for bad in ("a options: b", "a OPTIONS: b", "a oPtIoNs: b"):
        cell = arch._cell(bad)
        assert arch._NEUTRAL_COLON in cell, cell
        assert _re.search(r"(?i)options:", cell) is None, cell
    old_exact_case = "a OPTIONS: b".replace("Options:", "Options -")
    assert "OPTIONS:" in old_exact_case  # the old code left upper-case behind: the new test kills it
    assert "OPTIONS:" not in arch._cell("a OPTIONS: b")
    assert "OPTIONS" + arch._NEUTRAL_COLON in arch._cell("a OPTIONS: b")  # case kept, colon swapped 1-for-1
    # V3 mutation: dropping the brace escape (or the ALWAYS-id) breaks the round trip.
    assert arch._label("Pick {x}") == "Pick (x)"
    assert "{" not in arch._label("Other {word} choice") and "}" not in arch._label("Other {word} choice")
    block = arch._options({"options": [{"id": "pick-x", "label": "Pick {x}"}],
                           "default": None, "deadline": None, "blocks": None})
    assert "Pick (x) {pick-x}" in block, block  # id == slug(parens) yet still explicit


# ------------------------------------------------- seat4 (W4): trailing period
def test_trailing_period_consequence_round_trips():
    """Seat4: a consequence ending in '.' keeps it, with and without a deadline."""
    for deadline in (None, "2026-10-12"):
        choices = {"options": [{"id": "a", "label": "enable",
                                "consequence": "mail goes out."}],
                   "default": "a", "deadline": deadline, "blocks": None}
        text = arch.questions_md([row("routing:Q-D", choices=choices)], {})
        (entry,) = mp.parse_questions(text)
        assert entry.choices is not None, text
        assert entry.choices["options"][0].get("consequence") == "mail goes out.", text
        assert entry.choices.get("deadline") == deadline, text
        assert entry.choices.get("default") == "a", text
        line = next(ln for ln in text.splitlines() if "Options:" in ln)
        if deadline is None:
            assert line.rstrip().endswith(".. |") or ".. |" in line, line  # text '.' + generator '.'
        else:
            assert f"Deadline: {deadline}." in line, line


def test_trailing_period_last_label_round_trips():
    """Seat4: a last label ending in '.' with id == slug keeps it, +/- deadline."""
    for deadline in (None, "2026-10-12"):
        choices = {"options": [{"id": "a", "label": "first", "consequence": "x"},
                               {"id": "done", "label": "Done."}],
                   "default": None, "deadline": deadline, "blocks": None}
        text = arch.questions_md([row("routing:Q-L", choices=choices)], {})
        (entry,) = mp.parse_questions(text)
        assert entry.choices is not None, text
        assert [(o["id"], o["label"]) for o in entry.choices["options"]] == [
            ("a", "first"), ("done", "Done.")], text
        assert entry.choices.get("deadline") == deadline, text
        # single-option shape too (the label is still last)
        solo = {"options": [{"id": "done", "label": "Done."}],
                "default": None, "deadline": deadline, "blocks": None}
        solo_text = arch.questions_md([row("routing:Q-L", choices=solo)], {})
        (solo_entry,) = mp.parse_questions(solo_text)
        assert solo_entry.choices["options"] == [{"id": "done", "label": "Done."}], solo_text


def test_options_block_always_terminated():
    """Seat4: the generator's own '.' terminates the block, never the keeper text."""
    bare = arch._options({"options": [{"id": "a", "label": "enable",
                                        "consequence": "mail goes out."}],
                          "default": "a", "deadline": None, "blocks": None})
    assert bare.endswith("."), bare
    assert bare.endswith("mail goes out.."), bare
    dated = arch._options({"options": [{"id": "a", "label": "enable",
                                         "consequence": "mail goes out."}],
                           "default": "a", "deadline": "2026-10-12", "blocks": None})
    assert dated.endswith("Deadline: 2026-10-12."), dated
    assert "mail goes out.. Deadline:" in dated, dated
    plain = arch._options({"options": [{"id": "a", "label": "enable",
                                         "consequence": "mail goes out"}],
                           "default": "a", "deadline": None, "blocks": None})
    assert plain.endswith("mail goes out."), plain
    plain_dated = arch._options({"options": [{"id": "a", "label": "enable",
                                              "consequence": "mail goes out"}],
                                "default": "a", "deadline": "2026-10-12", "blocks": None})
    assert plain_dated.endswith("Deadline: 2026-10-12."), plain_dated
    assert "mail goes out. Deadline:" in plain_dated, plain_dated


# ------------------------------------------------- seat5 (W5): collapse before redact
_SEP_CASES = ["\n", "\r\n", "\t", "   ", "|"]
_HEX16 = "0123456789abcdef"
# Credential-shaped fixtures are built at runtime (never as literals) so the
# public-split scrub sees no credential shape in this source. Runtime values:
# _SECRET is "abcd"+"1234", _PAIR is _USER + "/" + _SECRET.
_USER = "user"
_SECRET = "abcd" + "1234"
_PAIR = _USER + "/" + _SECRET


def _split_secret_rows():
    """One row per separator x field: a whitespace-gapped secret in each cell kind.

    Each whitespace separator gets a >120-char run between the guard keyword and the pair
    (``credential_guard`` only links a pair within 120 chars, so redacting the
    raw text misses it) plus the reported shape (a 48-hex secret straddling one
    separator) in the description. ``|`` is a table pipe, not whitespace: its case is
    the seat6 V2 shape, a credential pair split by the pipe (``user|abcd1234``), which
    the pipeline's ``|`` -> ``/`` join must bring together *before* redaction.
    """
    rows, descriptions = [], {}
    for i, sep in enumerate(_SEP_CASES):
        if sep == "|":
            pair = "credential " + _USER + sep + _SECRET + " ok"
        else:
            gap = sep * (150 // len(sep) + 1)
            pair = "credential see" + gap + _PAIR + " ok"
        key = f"routing:Q-SPLIT{i}"
        rows.append(row(key, "Q: pick?"))
        descriptions[key] = pair
        lab_key = f"routing:Q-SLAB{i}"
        rows.append(row(lab_key, "Q: pick?", choices={
            "options": [{"id": "a", "label": f"go {pair}",
                         "consequence": "nothing changes"}],
            "default": None, "deadline": None, "blocks": None}))
        descriptions[lab_key] = "plain"
        con_key = f"routing:Q-SCON{i}"
        rows.append(row(con_key, "Q: pick?", choices={
            "options": [{"id": "a", "label": "go",
                         "consequence": f"{pair} done"}],
            "default": None, "deadline": None, "blocks": None}))
        descriptions[con_key] = "plain"
        hex_key = f"routing:Q-SHEX{i}"
        rows.append(row(hex_key, "Q: pick?"))
        descriptions[hex_key] = f"token {_HEX16 * 2}{sep}{_HEX16} end"
    return rows, descriptions


def _assert_split_secrets_redacted():
    import re as _re3

    from plangraph.keeper import credential_guard as _cg
    rows, descriptions = _split_secret_rows()
    text = arch.questions_md(rows, descriptions)
    assert "[redacted]" in text
    assert "user/abcd1234" not in text
    assert _cg.find_shapes(text) == []
    assert _re3.search(r"[0-9a-fA-F]{32,}", text) is None


def test_split_secrets_redacted_after_collapse():
    """Seat5: a secret gapped by newline/CRLF/tab/spaces is redacted in the file."""
    _assert_split_secrets_redacted()


def test_old_order_redact_before_collapse_misses_gapped_secrets(monkeypatch):
    """Seat5 mutation: redacting before the whitespace collapse leaks the gapped pair."""
    import md_sync_parse as _mp

    from plangraph.keeper import credential_guard as _cg

    def _old_cell(text, **_kw):
        redacted = _cg.mask(_mp.redact(str(text or ""))[0])[0]
        return arch._neutralise_keywords(" ".join(redacted.replace("|", "/").split()))

    monkeypatch.setattr(arch, "_cell", _old_cell)
    with pytest.raises(AssertionError):
        _assert_split_secrets_redacted()


# ------------------------------------------------- seat6 (W6): one pipeline, redaction last
def test_pipe_split_secret_redacted_in_every_cell():
    """Seat6 V2 (a): a credential split by '|' is joined by '/' before redaction.

    ``user|abcd1234`` matches no credential shape until the pipe becomes ``/``;
    the pipeline joins first and redacts last, so no cell kind leaks it.
    """
    from plangraph.keeper import credential_guard as _cg
    secret = "credential " + _USER + "|" + _SECRET
    rows = [row("routing:Q-PDESC", "Q: pick?"),
            row("routing:Q-PLAB", "Q: pick?", choices={
                "options": [{"id": "a", "label": f"go {secret}",
                             "consequence": "nothing changes"}],
                "default": None, "deadline": None, "blocks": None}),
            row("routing:Q-PCON", "Q: pick?", choices={
                "options": [{"id": "a", "label": "go",
                             "consequence": f"{secret} done"}],
                "default": None, "deadline": None, "blocks": None})]
    text = arch.questions_md(rows, {"routing:Q-PDESC": f"see {secret} ok"})
    assert "user|abcd1234" not in text and "user/abcd1234" not in text
    assert "[redacted]" in text
    assert _cg.find_shapes(text) == []


def test_sixty_char_label_with_keyword_round_trips():
    """Seat6 V3 (b): keyword neutralisation is length-preserving.

    A valid 60-char label containing a keyword round-trips: the block is accepted
    (not dropped as malformed) and the label keeps its length; same for a
    160-char consequence.
    """
    import re as _re4
    raw_label = "Options: " + "ab " * 16 + "ab!"
    assert len(raw_label) == 60
    neutral_label = arch._label(raw_label)
    assert len(neutral_label) == len(raw_label)
    assert _re4.search(r"(?i)options:", neutral_label) is None
    raw_consequence = "Deadline: " + "cd " * 49 + "cd?"
    assert len(raw_consequence) == 160
    neutral_consequence = arch._cell(raw_consequence, consequence=True)
    assert len(neutral_consequence) == len(raw_consequence)
    assert _re4.search(r"(?i)deadline:", neutral_consequence) is None
    choices = {"options": [{"id": "a", "label": raw_label,
                            "consequence": raw_consequence}],
               "default": None, "deadline": None, "blocks": None}
    text = arch.questions_md([row("routing:Q-LEN", choices=choices)], {})
    (entry,) = mp.parse_questions(text)
    assert entry.choices is not None  # block accepted, not malformed
    (opt,) = entry.choices["options"]
    assert opt["label"] == neutral_label and len(opt["label"]) == 60
    assert opt["consequence"] == neutral_consequence and len(opt["consequence"]) == 160


def test_old_order_redact_before_pipe_join_leaks_split_secret(monkeypatch):
    """Seat6 V2 mutation (e): redacting before the '|' -> '/' join leaks the split pair."""
    import md_sync_parse as _mp

    from plangraph.keeper import credential_guard as _cg

    def _old_cell(text, **_kw):
        collapsed = " ".join(str(text or "").split())
        redacted = _cg.mask(_mp.redact(collapsed)[0])[0]
        return arch._neutralise_keywords(redacted.replace("|", "/"))

    monkeypatch.setattr(arch, "_cell", _old_cell)
    text = arch.questions_md([row("routing:Q-P", "Q: pick?")],
                             {"routing:Q-P": "credential " + _USER + "|" + _SECRET + " ok"})
    assert "user/abcd1234" in text or _cg.find_shapes(text) != []


# ------------------------------------------------- seat7 (W7): redaction last, robust write
def test_finalise_cell_redaction_last_on_all_four_kinds():
    """Seat7 HIGH: redaction ends every branch; '=' / ';' splits never leak."""
    from plangraph.keeper import credential_guard as _cg
    assert hasattr(arch, "_finalise_cell")
    for kind in ("title", "description", "label", "consequence"):
        for secret in ("credential " + _USER + "=" + _SECRET,
                       "credential " + _USER + ";" + _SECRET,
                       "credential " + _USER + "|" + _SECRET,
                       "credential " + _PAIR):
            got = arch._finalise_cell(secret, kind)
            assert "abcd1234" not in got, (kind, secret, got)
            assert "user/abcd1234" not in got and "user=abcd1234" not in got \
                and "user;abcd1234" not in got, (kind, secret, got)
            assert _cg.find_shapes(got) == [], (kind, secret, got)
        # separator-safe per kind: labels carry no ';' / '=' after finalise
        assert ";" not in arch._finalise_cell("x; y = z", "label")
        assert "=" not in arch._finalise_cell("x; y = z", "label")
        assert ";" not in arch._finalise_cell("x; y", "consequence")
    # all four cell kinds flow through the helper (title, description, label, consequence)
    seen = []
    real = arch._finalise_cell

    def spy(text, kind):
        seen.append(kind)
        return real(text, kind)

    import unittest.mock as _mock
    with _mock.patch.object(arch, "_finalise_cell", spy):
        arch._cell("t", kind="title")
        arch._cell("d", kind="description")
        arch._cell("l", label=True)
        arch._cell("c", consequence=True)
    assert seen == ["title", "description", "label", "consequence"]


def test_split_secrets_equals_semicolon_in_labels_redacted():
    """Seat7 HIGH: '=' / ';'-split pairs in labels are redacted, file stays safe."""
    from plangraph.keeper import credential_guard as _cg
    for sep in ("=", ";"):
        secret = "credential " + _USER + sep + _SECRET
        choices = {"options": [{"id": "a", "label": f"go {secret}",
                                "consequence": "nothing changes"}],
                   "default": None, "deadline": None, "blocks": None}
        text = arch.questions_md([row("routing:Q-SEP", choices=choices)], {})
        assert "abcd1234" not in text, (sep, text)
        assert _cg.find_shapes(text) == [], (sep, text)
        assert ("user=abcd1234" not in text and "user;abcd1234" not in text
                and "user/abcd1234" not in text), (sep, text)
        assert "[redacted]" in text
    # title / description / consequence with '=' also redacted (redaction last everywhere)
    text = arch.questions_md(
        [row("routing:Q-T", "Q: credential user=abcd1234?"),
         row("routing:Q-C", choices={"options": [
             {"id": "a", "label": "go", "consequence": "credential user;abcd1234 done"}],
             "default": None, "deadline": None, "blocks": None})],
        {"routing:Q-T": "see credential user;abcd1234 ok",
         "routing:Q-C": "plain"})
    assert "abcd1234" not in text and _cg.find_shapes(text) == []


def test_write_atomic_surrogate_replaced_keeps_mode_and_no_temp(tmp_path):
    """Seat7 MEDIUM: lone surrogate never crashes; old mode (0644) is kept."""
    target = tmp_path / "QUESTIONS.md"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    arch._write_atomic(str(target), "new \ud800 text")
    assert "old" not in target.read_text(encoding="utf-8")
    assert "\ud800" not in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".questions.")] == []


def test_write_atomic_forced_failures_keep_old_file(tmp_path, monkeypatch):
    """Seat7 MEDIUM: ANY write failure keeps the old file, no temp left behind."""
    import os as _os
    # (a) os.replace raises
    target = tmp_path / "A.md"
    target.write_text("keep-a", encoding="utf-8")
    def boom_replace(*_a, **_k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(_os, "replace", boom_replace)
    with pytest.raises(RuntimeError):
        arch._write_atomic(str(target), "new")
    assert target.read_text(encoding="utf-8") == "keep-a"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".questions.")] == []
    monkeypatch.undo()
    # (b) handle.write raises (mock the file object os.fdopen returns)
    target2 = tmp_path / "B.md"
    target2.write_text("keep-b", encoding="utf-8")
    real_fdopen = _os.fdopen

    class _BadHandle:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def write(self, _text):
            raise RuntimeError("write gone")

    def bad_fdopen(*_a, **_k):
        _fd = _a[0]
        try:
            _os.close(_fd)
        except OSError:
            pass
        return _BadHandle()

    monkeypatch.setattr(_os, "fdopen", bad_fdopen)
    try:
        with pytest.raises(RuntimeError):
            arch._write_atomic(str(target2), "new")
    finally:
        monkeypatch.setattr(_os, "fdopen", real_fdopen)
    assert target2.read_text(encoding="utf-8") == "keep-b"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".questions.")] == []


def test_empty_label_after_neutralisation_falls_back_to_id():
    """Seat7 MEDIUM: '*' / '* ' labels are written as the option id."""
    for raw in ("*", "* "):
        block = arch._options({"options": [{"id": "a", "label": raw}],
                               "default": None, "deadline": None, "blocks": None})
        assert block == "Options: a.", block
        got = mp.parse_options(block + " ")
        # parses to a real option whose label is the id (never empty)
        assert got[1] is not None and got[1]["options"][0]["label"] == "a", block


def test_malformed_snapshot_data_returns_2_with_status(tmp_path, monkeypatch):
    """Seat7 MEDIUM: choices={'options':['a']} writes the status file, returns 2."""
    target = tmp_path / "QUESTIONS.md"
    target.write_text("keep me", encoding="utf-8")
    tok = tmp_path / "t"
    tok.write_text("x" * 20, encoding="utf-8")
    os.chmod(tok, 0o600)
    status = tmp_path / "status.json"
    bad = {"key": "routing:Q-BAD", "project": "routing", "title": "Q: bad?",
           "kind": "question", "state": "open", "wait": "answer",
           "operator": True, "priority": 0, "depends_on": [],
           "description": "d", "choices": {"options": ["a"]}}

    def fake_get(self, _verb):
        return {"items": [bad]}

    monkeypatch.setattr(md_sync.apply_batch.Session, "get", fake_get)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers", "--questions-out", str(target),
                       "--url", "http://keeper.invalid", "--token-file", str(tok),
                       "--apply", "--status-file", str(status)], out=out, err=err)
    assert rc == 2
    assert target.read_text(encoding="utf-8") == "keep me"
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert doc["error"] is not None and doc["written"] is False
