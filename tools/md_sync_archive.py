"""PG-RF-ARCHIVE-ANSWERS: QUESTIONS.md generated from the keeper (answers live in the keeper, Q9).

``questions_md`` renders the keeper's open operator items (``views.needs_you``: question /
decision / operator_task, ``choices`` included) as the priority table ``md_sync_parse`` reads
back, so the file round-trips.  ``archive_main`` is the ``md_sync.py --archive-answers`` switch
(default OFF): it replaces the markdown import, writing the file from the live graph instead.

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


def _cell(text):
    """One table-safe line: credentials masked, whitespace collapsed, no ``|``."""
    text = credential_guard.mask(mp.redact(str(text or ""))[0])[0]
    return " ".join(text.replace("|", "/").split())


def _label(text):
    return _cell(text).replace(";", ",").replace("=", "-")


def _options(choices):
    """``Options: a = c; *b = c. Deadline: D`` (the block ``parse_options`` reads back), or ``""``."""
    if not isinstance(choices, dict) or not choices.get("options"):
        return ""
    parts = []
    for option in choices["options"]:
        text = ("*" if option.get("id") == choices.get("default") else "") + _label(option.get("label"))
        if option.get("consequence"):
            text += " = " + _cell(option["consequence"]).replace(";", ",")
        parts.append(text)
    block = "Options: " + "; ".join(parts)
    if choices.get("deadline"):
        block += f". Deadline: {choices['deadline']}"
    return block


def _kind_cell(row):
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
        step = _cell(descriptions.get(key)).replace("Options:", "Options -")
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
