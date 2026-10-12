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
import stat
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

#: Length-preserving neutralisation for the ':' of ``Options:``/``Deadline:``/``Parent:``:
#: U+A789 MODIFIER LETTER COLON. The parser only reacts to a literal ':' (``_OPTIONS_RE``,
#: ``_PARENT_RE``, ``deadline\s*:``), so the lookalike neutralises without ever parsing as
#: structure. ';' cannot serve: it separates options in ``parse_options``. U+A789 is a
#: modifier letter (Lm, not a control per ``is_control``), never a table or option
#: separator, and swaps 1-for-1, so a valid 60-char label or 160-char consequence keeps
#: its length instead of growing by one and being rejected on read-back.
_NEUTRAL_COLON = "\uA789"


def _neutralise_keywords(text):
    """Neutralise every keyword in ``mp.STRUCTURAL_KEYWORDS`` (case-insensitive).

    Length-preserving: the ':' of ``Options:``/``Deadline:``/``Parent:`` becomes
    U+A789 (one char for one char, matched case kept); ``{``/``}`` become parens
    (``{id}`` means any brace, so no `` {id}`` suffix parses); ``*`` strips the
    leading default marker (shortens only, never grows). Reads the tuple live so
    dropping an entry disables that neutralisation."""
    for kw in mp.STRUCTURAL_KEYWORDS:
        if kw.endswith(":"):
            text = re.compile(re.escape(kw), re.IGNORECASE).sub(
                lambda match: match.group(0)[:-1] + _NEUTRAL_COLON, text)
        elif "{" in kw or "}" in kw:
            text = text.replace("{", "(").replace("}", ")")
        elif kw == "*":
            text = text.lstrip("* ")
    return text


def _redact_all(text):
    """``mp.redact`` + ``credential_guard.mask`` together; ``(text, count)``."""
    redacted, count = mp.redact(text)
    masked, extra = credential_guard.mask(redacted)
    return masked, count + extra


def _finalise_cell(text, kind):
    """Separator-safe redaction; redaction is always the LAST transform.

    *kind*: ``"title"`` / ``"description"`` / ``"label"`` / ``"consequence"``.
    ``=``/``;``-split credential pairs (``"credential user=abcd1234"``,
    ``"credential user;abcd1234"``) match no shape until the separator
    becomes ``/``, so the label ``=`` -> ``-`` / ``;`` -> ``,`` swaps would
    hide them when they run before redaction.  Expose them on a probe
    (``=``/``;`` -> ``/``) for detection, redact, make the redacted output
    separator-safe per *kind*, then redact AGAIN so the returned text ends
    with redaction.  Non-secret text keeps the historical safe chars
    (``,``/``-`` for labels); split-secret text returns the redacted probe
    (``/`` + ``[redacted]``, separator-free).
    """
    s = str(text or "")
    probe = s.replace("|", "/").replace("=", "/").replace(";", "/")
    red_probe, n_probe = _redact_all(probe)
    if kind == "label":
        safe = s.replace("|", "/").replace(";", ",").replace("=", "-")
    elif kind == "consequence":
        safe = s.replace("|", "/").replace(";", ",")
    else:  # title / description / keys: '|' joined, '=' / ';' kept
        safe = s.replace("|", "/")
    red_safe, n_safe = _redact_all(safe)
    base = red_probe if n_probe > n_safe else red_safe
    if kind == "label":
        base = base.replace(";", ",").replace("=", "-")
    elif kind == "consequence":
        base = base.replace(";", ",")
    return _redact_all(base)[0]


def _cell(text, *, label=False, consequence=False, kind=None):
    """One table-safe line through the single ordered cell pipeline.

    Order (all text transforms first, redaction last): (a) whitespace/newline
    collapse, (b) ``|`` -> ``/``, (c) keyword neutralisation, and LAST (d)
    ``_finalise_cell`` (separator-safe swaps per kind + ``mp.redact`` +
    ``credential_guard.mask``, re-checked so redaction ends the pipeline).
    Redaction runs last so it sees the final text exactly as it is written
    to the file: a credential split by a table pipe -- or by ``=`` / ``;``
    in a label -- is exposed as ``/`` on the probe before redaction and
    masked, never joined after redaction and leaked.
    """
    collapsed = " ".join(str(text or "").split())  # (a)
    shaped = collapsed.replace("|", "/")  # (b) pipe join before anything else
    neutral = _neutralise_keywords(shaped)  # (c) braces + keywords
    if kind is None:
        if label:
            kind = "label"
        elif consequence:
            kind = "consequence"
        else:
            kind = "description"
    return _finalise_cell(neutral, kind)  # (d) LAST, on the final text


def _label(text):
    # Only the generator's own " {id}" suffix may carry braces (handled in
    # _cell via the "{id}" keyword); ";" and "=" are option separators, so label
    # cells take the pipeline's label branch (swaps run before redaction inside
    # _cell: all text transforms first, redaction last).
    return _cell(text, label=True)


def _options(choices):
    """``Options: a = c; *b {custom-id} = c. Deadline: D.`` (the block ``parse_options`` reads back), or ``""``.

    The block always ends with its own ``.`` (and ``Deadline: D.`` ends with one
    too), so the parser's optional trailing ``.`` eats the generator's period,
    never the keeper text's: a consequence or label ending in ``.`` round-trips.
    An option whose id is not the slug of its label is written ``label {id}`` so the
    id survives a round trip; ``parse_options`` reads the explicit id back, slugging
    only labels without one.  The Kind column carries ``DECISION`` for decisions, so a
    decision's kind survives too; the keeper stays the source of truth."""
    if not isinstance(choices, dict) or not choices.get("options"):
        return ""
    parts = []
    for option in choices["options"]:
        if not isinstance(option, dict):
            raise ValueError(f"malformed option: {option!r}")
        raw_label = str(option.get("label") or "")
        label = _label(option.get("label"))
        oid = option.get("id")
        if not label.strip():
            label = oid if isinstance(oid, str) and oid else "option"
            label = _cell(label, kind="label")
        text = ("*" if oid == choices.get("default") else "") + label
        if isinstance(oid, str) and (("{" in raw_label or "}" in raw_label)
                                    or oid != mp._slug(label)):
            text += " {" + oid + "}"
        if option.get("consequence"):
            text += " = " + _cell(option["consequence"], consequence=True)
        parts.append(text)
    block = "Options: " + "; ".join(parts) + "."
    if _DATE.fullmatch(str(choices.get("deadline") or "")):
        block += f" Deadline: {choices['deadline']}."
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
        step = _cell(descriptions.get(key), kind="description")
        block = _options(row.get("choices"))
        step = f"{step} {block}".strip()
        why = _cell(_PREFIX.sub("", row.get("title") or ""), kind="title")
        out.append(f"| {rank} | {_cell(key.split(':', 1)[-1], kind='title')} | {_kind_cell(row)} | {why} | {step} "
                   f"| {_cell(key, kind='description')} |\n")
    return "".join(out)


def _write_atomic(path, text):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    try:
        old_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        old_mode = None
    fd, tmp = tempfile.mkstemp(prefix=".questions.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
        if old_mode is not None:
            os.chmod(tmp, old_mode)
        os.replace(tmp, path)
    except Exception:
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
    try:
        rows = views.needs_you(items, project=opts.project)
        text = questions_md(rows, {i["key"]: i.get("description") for i in items})
    except Exception as exc:
        err.write(f"error: malformed snapshot data: {exc.__class__.__name__}\n")
        return stop(2, f"error: malformed snapshot data: {exc.__class__.__name__}")
    status["archived"] = len(rows)
    if not opts.apply:
        out.write(f"dry run: would write {len(rows)} open items to {opts.questions_out} (no file written)\n")
        return stop(0, None)
    try:
        _write_atomic(opts.questions_out, text)
    except Exception as exc:
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
