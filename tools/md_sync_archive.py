"""PG-RF-ARCHIVE-ANSWERS: QUESTIONS.md generated from the keeper (answers live in the keeper, Q9).

``questions_md`` renders the keeper's open operator items (``views.needs_you``: question /
decision / operator_task, ``choices`` included) as the priority table ``md_sync_parse`` reads
back, so the file round-trips losslessly: a decision row is written with Kind ``DECISION``
(which parses back to kind ``decision``, wait ``answer``) and an option whose id differs
from its label slug is written ``label {id}`` (which parses back to that id).  ``archive_main``
is the ``md_sync.py --archive-answers`` switch (default OFF): it replaces the markdown import,
writing the file from the live graph instead.

    md_sync.py --archive-answers --questions-out PATH --url U --token-file F
               [--project routing] [--dry-run | --apply] [--status-file FILE]
"""
import argparse
import os
import re
import sys
import tempfile

import apply_batch
import md_sync_parse as mp
import md_sync_plan as mplan  # noqa: F401  (puts src/ on sys.path)

from plangraph import views  # noqa: E402
from plangraph.keeper import credential_guard  # noqa: E402

USAGE = ("usage: md_sync.py --archive-answers --questions-out PATH --url U --token-file F "
         "[--project NAME] [--dry-run | --apply] [--status-file FILE]\n")

HEADER = ("# QUESTIONS\n\n"
          "Generated from the keeper's `needs_you` (`md_sync.py --archive-answers`); do not edit, it is "
          "overwritten.\nAnswers live in the keeper: answer with `pg answer KEY`, read them with "
          "`pg answers`.\n\n## Open operator items\n\n")
TABLE_HEAD = "| Prio | ID | Kind | Why | Exact step | Where |\n|---|---|---|---|---|---|\n"
_PREFIX = re.compile(r"^(?:Q|OP):\s*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _neutralise_keywords(text):
    """Neutralise every keyword in ``mp.STRUCTURAL_KEYWORDS`` (case-insensitive).

    ``Options:``/``Deadline:``/``Parent:`` lose their colon (``Deadline:`` ->
    ``Deadline -``) so the parser never sees a block/token/deadline; ``{id}``
    means any brace (``{``/``}`` -> parens) so no `` {id}`` suffix parses;
    ``*`` strips the leading default marker. Reads the tuple live so dropping
    an entry disables that neutralisation."""
    for kw in mp.STRUCTURAL_KEYWORDS:
        if kw.endswith(":"):
            text = re.compile(re.escape(kw), re.IGNORECASE).sub(kw[:-1] + " -", text)
        elif "{" in kw or "}" in kw:
            text = text.replace("{", "(").replace("}", ")")
        elif kw == "*":
            text = text.lstrip("* ")
    return text


def _cell(text):
    """One table-safe line: credentials masked, whitespace collapsed, no ``|``."""
    text = credential_guard.mask(mp.redact(str(text or ""))[0])[0]
    return _neutralise_keywords(" ".join(text.replace("|", "/").split()))


def _label(text):
    # Only the generator's own " {id}" suffix may carry braces (handled in
    # _cell via the "{id}" keyword); ";" and "=" are option separators.
    return _cell(text).replace(";", ",").replace("=", "-")


def _options(choices):
    """``Options: a = c; *b {custom-id} = c. Deadline: D`` (the block ``parse_options`` reads back), or ``""``.

    An option whose id is not the slug of its label is written ``label {id}`` so the
    id survives a round trip; ``parse_options`` reads the explicit id back, slugging
    only labels without one.  The Kind column carries ``DECISION`` for decisions, so a
    decision's kind survives too; the keeper stays the source of truth."""
    if not isinstance(choices, dict) or not choices.get("options"):
        return ""
    parts = []
    for option in choices["options"]:
        raw_label = str(option.get("label") or "")
        label = _label(option.get("label"))
        oid = option.get("id")
        text = ("*" if oid == choices.get("default") else "") + label
        if isinstance(oid, str) and (("{" in raw_label or "}" in raw_label)
                                    or oid != mp._slug(label)):
            text += " {" + oid + "}"
        if option.get("consequence"):
            text += " = " + _cell(option["consequence"]).replace(";", ",")
        parts.append(text)
    block = "Options: " + "; ".join(parts)
    if _DATE.fullmatch(str(choices.get("deadline") or "")):
        block += f". Deadline: {choices['deadline']}"
    return block


def _kind_cell(row):
    if row.get("kind") == "decision":
        return "DECISION"
    wait = row.get("wait")
    if wait in ("answer", "task"):
        return "ANSWER" if wait == "answer" else "TASK"
    return "TASK" if row.get("kind") == "operator_task" else "ANSWER"


def questions_md(rows, descriptions):
    """The QUESTIONS.md text for *rows* (``views.needs_you`` order kept); *descriptions*: key -> text."""
    out = [HEADER]
    if not rows:
        out.append("(no open operator items)\n")
        return "".join(out)
    out.append(TABLE_HEAD)
    for rank, row in enumerate(rows, 1):
        key = row["key"]
        step = _cell(descriptions.get(key))
        block = _options(row.get("choices"))
        step = f"{step} {block}".strip()
        why = _cell(_PREFIX.sub("", row.get("title") or ""))
        out.append(f"| {rank} | {_cell(key.split(':', 1)[-1])} | {_kind_cell(row)} | {why} | {step} "
                   f"| {_cell(key)} |\n")
    return "".join(out)


def _write_atomic(path, text):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".questions.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def archive_main(argv, out, err, write_status):
    """``md_sync.py --archive-answers ...``; *write_status* is md_sync's atomic status writer."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--archive-answers", action="store_true")
    for name in ("questions-out", "token-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--url", default=apply_batch.DEFAULT_URL)
    parser.add_argument("--project", default="routing")
    parser.add_argument("--status-file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    status = {"ts": None, "mode": "archive", "archived": 0, "written": False, "error": None}
    try:
        opts = parser.parse_args(argv)
    except SystemExit:
        err.write(USAGE)
        write_status(_flag(argv, "--status-file"), status, "error: bad arguments (see usage)", err)
        return 2

    def stop(rc, error):
        write_status(opts.status_file, status, error, err)
        return rc

    try:
        token = apply_batch.read_token(opts.token_file)
    except (OSError, apply_batch.BatchError) as exc:
        err.write(f"error: {exc}\n")
        return stop(2, f"error: {exc}")
    try:
        items = apply_batch.Session(opts.url, token).get("snapshot").get("items") or []
    except apply_batch.ApiError as exc:
        err.write(f"error: cannot read the live graph: {exc.code} {exc.msg}\n")
        return stop(2, f"error: cannot read the live graph: {exc.code} {exc.msg}")
    rows = views.needs_you(items, project=opts.project)
    text = questions_md(rows, {i["key"]: i.get("description") for i in items})
    status["archived"] = len(rows)
    if not opts.apply:
        out.write(f"dry run: would write {len(rows)} open items to {opts.questions_out} (no file written)\n")
        return stop(0, None)
    try:
        _write_atomic(opts.questions_out, text)
    except OSError as exc:
        err.write(f"error: cannot write {opts.questions_out}: {exc.__class__.__name__}\n")
        return stop(2, f"error: cannot write {opts.questions_out}: {exc.__class__.__name__}")
    status["written"] = True
    out.write(f"md-sync archive: wrote {len(rows)} open items to {opts.questions_out}\n")
    return stop(0, None)


def _flag(argv, name):
    for n, arg in enumerate(argv):
        if arg == name and n + 1 < len(argv):
            return argv[n + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None
