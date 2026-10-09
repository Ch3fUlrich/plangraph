"""Parsers and redaction for md_sync (QUESTIONS.md, ANSWERS.md, decisions-log.md)."""
import re
import unicodedata
from collections import namedtuple

Question = namedtuple("Question", "id kind title description heading wait choices",
                      defaults=(None,))
Answer = namedtuple("Answer", "id decisions date text")
Skipped = namedtuple("Skipped", "id reason")

ID = r"(?:OS-[0-9]+[A-Za-z]?|Q-[A-Z0-9][A-Z0-9-]*)"
_BULLET_RE = re.compile(r"^- +(" + ID + r")(?![A-Za-z0-9-])[ :]*(.*)$")
_ANY_BULLET_RE = re.compile(r"^- ")
_HEADING_RE = re.compile(r"^## +(.*?)\s*$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_SENTENCE_RE = re.compile(r"^(.+?[.?!])(?:\s+|$)")

_DECISION_ROW_RE = re.compile(r"^\| D-([0-9]+) \|")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
TITLE_MAX = 120
TITLE_CAP = 200



def is_control(c):
    """Same rule as plangraph.keeper.item_fields.is_control (kept in sync by a test): the
    parser must never emit choices the keeper refuses, or the whole md-sync batch is refused."""
    return ord(c) < 32 or 127 <= ord(c) < 160 or unicodedata.category(c) in ("Cf", "Zl", "Zp")

def _entries(text, bullet_re):
    """Yield ``(heading, match, lines)``: a top-level ID bullet plus its body."""
    heading, cur = None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        head = _HEADING_RE.match(line)
        if head:
            if cur:
                yield cur
            heading, cur = head.group(1), None
            continue
        match = bullet_re.match(line)
        if match:
            if cur:
                yield cur
            cur = (heading, match, [])
        elif _ANY_BULLET_RE.match(line):
            if cur:
                yield cur
            cur = None
        elif cur is not None and line[:1] in (" ", "\t"):
            cur[2].append(line.strip())
        elif cur is not None and line == "":
            continue
        elif cur is not None:
            yield cur
            cur = None
    if cur:
        yield cur


def _title_and_rest(text):
    bold = _BOLD_RE.search(text)
    if bold:
        title = bold.group(1).strip()
        rest = (text[:bold.start()] + text[bold.end():]).lstrip(" \t:-.")
        return title, rest.strip()
    sentence = _SENTENCE_RE.match(text)
    title = sentence.group(1) if sentence else text
    rest = text[len(title):].strip()
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX].rstrip()
        rest = text[len(title):].strip()
    return title, rest


def _norm_id(ident):
    if re.fullmatch(r"(?i)OS-[0-9]+[A-Za-z]?", ident or ""):
        return ident.upper()
    return ident


def _classify_wait(ident, kind, title, raw):
    """``answer`` for a question/decision, else ``task`` (D-677)."""
    if ident.startswith("Q-"):
        return "answer"
    if "?" in (title or ""):
        return "answer"
    stripped = (raw or "").lstrip()
    if stripped.startswith("Decision") or stripped.startswith("decide"):
        return "answer"
    if kind == "question":
        return "answer"
    return "task"


#: the explicit Kind cell of a priority-table row -> wait
_KIND_CELLS = {"TASK": "task", "ANSWER": "answer"}

_TABLE_ID_RE = re.compile(r"(?i)(OS-[0-9]+[A-Za-z]?|Q-[A-Z0-9][A-Z0-9-]*)")


def _table_rows(text):
    """``(heading, id, why, step, where, kind)`` per priority-table data row.

    Shape: ``| <anything> | OS-123 | <why> | <exact step> | <where> |``.
    Rows before the table header count too; a header/separator row has no
    id in its 2nd cell.  ``OS-70 (3b)`` yields ``OS-70``; a 2nd cell with
    two or more ids (``OS-56, OS-51, ...``) is skipped.
    """
    out, heading, fenced, kind_col = [], None, False, False
    for raw in text.splitlines():
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        head = _HEADING_RE.match(line)
        if head:
            heading, _ = head.group(1), None
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            kind_col = False     # a table ended: the next one declares its own header
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        parts = stripped.split("|")[1:]
        if parts and parts[-1].strip() == "":
            parts = parts[:-1]
        cells = [p.strip() for p in parts]
        if len(cells) < 5:
            continue
        if cells[1].strip().lower() == "id":
            # Optional Kind column (QUESTIONS.md since 2026-10-09), declared by the header:
            # ``| Prio | ID | Kind | why | step | where |``.
            kind_col = len(cells) >= 3 and cells[2].strip().lower() == "kind"
            continue
        ids = _TABLE_ID_RE.findall(cells[1])
        if len(ids) != 1:
            continue
        kind = None
        if kind_col or (len(cells) >= 6 and cells[2].strip().upper() in _KIND_CELLS):
            value = cells[2].strip().upper()
            kind = value if value in _KIND_CELLS else None   # unknown -> the rule decides
            cells = cells[:2] + cells[3:] + [""]
            if len(cells) < 5:
                continue
        out.append((heading, _norm_id(ids[0]), cells[2], cells[3], cells[4], kind))
    return out


#: QA-IMPORT: ``Options: a = consequence; *b = consequence. Deadline: 2026-10-12`` at the end
#: of the "Exact step" cell (``*`` marks the default; ``;`` separates, a table cell cannot hold ``|``).
_OPTIONS_RE = re.compile(r"(?:^|\s)Options:\s*(?P<opts>.+?)"
                         r"(?:\.?\s*Deadline:\s*(?P<deadline>[0-9]{4}-[0-9]{2}-[0-9]{2}))?\.?\s*$", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(label):
    return _SLUG_RE.sub("-", label.lower()).strip("-")[:40] or "option"


def parse_options(step, why=""):
    """``(step without the Options block, choices dict or None)``.

    A malformed block (empty labels, duplicate ids, > 8 options) is left in the
    step text and yields no choices, so a typo never drops the operator's words.
    """
    if len(re.findall(r"(?i)(?:^|\s)Options:", step or "")) > 1:
        return step, None                     # two blocks: ambiguous, keep the operator's text
    match = _OPTIONS_RE.search(step or "")
    if not match:
        return step, None
    if re.search(r"deadline\s*:", match.group("opts"), re.IGNORECASE):   # a Deadline that did not parse: keep the text
        return step, None
    if match.group("deadline"):
        from datetime import date
        try:
            date.fromisoformat(match.group("deadline"))
        except ValueError:
            return step, None
    options, default, seen = [], None, set()
    for part in match.group("opts").split(";"):
        part = part.strip()
        if not part:
            return step, None
        marked = part.startswith("*")
        part = part.lstrip("*").strip()
        label, _, consequence = part.partition("=")
        if any(is_control(c) for c in label + consequence):
            return step, None                 # checked before trimming: a tab is a control too
        label, consequence = label.strip(), consequence.strip()
        oid = _slug(label)
        if not re.search(r"[A-Za-z0-9]", label) or oid in seen or len(label) > 60 or len(consequence) > 160:
            return step, None
        seen.add(oid)
        entry = {"id": oid, "label": label}
        if consequence:
            entry["consequence"] = consequence
        options.append(entry)
        if marked:
            if default is not None:
                return step, None
            default = oid
    if not options or len(options) > 8:
        return step, None
    blocks = (why or "").strip()[:200] or None
    if blocks and any(ord(c) < 32 or 127 <= ord(c) < 160 for c in blocks):
        blocks = None                       # derived context only: drop it, keep the choices
    rest = step[:match.start()].rstrip(" .;:")
    return rest, {"options": options, "default": default,
                  "deadline": match.group("deadline"), "blocks": blocks}


_ONE_ID_CELL = re.compile(r"(?i)(?:OS-[0-9]+[A-Za-z]?|Q-[A-Z0-9][A-Z0-9-]*)(?:\s*\([^()]{1,40}\))?")


def unmatched_rows(text):
    """QA-IMPORT: data rows of the QUESTIONS tables whose ID cell is not exactly one id."""
    out, fenced = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        stripped = line.strip()
        if fenced or not stripped.startswith("|") or line.startswith(("    ", "\t")):
            continue
        cells = [p.strip() for p in stripped.split("|")[1:]]
        if len(cells) < 5 or (cells[1].lower() == "id" and cells[0].lower() in ("prio", "priority", "")) \
                or all(set(c) <= set("-: ") for c in cells if c):   # header / separator row
            continue
        if not cells[1]:
            out.append("(empty id cell)")
            continue
        ids = _TABLE_ID_RE.findall(cells[1])
        # exactly one id, optionally followed by one parenthesised variant tag ("OS-70 (3b)")
        if len(ids) != 1 or not _ONE_ID_CELL.fullmatch(cells[1].strip()):
            out.append(cells[1])          # full text: the caller redacts, then truncates
    return out


def parse_questions(text):
    bullets = []
    for heading, match, body in _entries(text, _BULLET_RE):
        raw = match.group(2).strip()
        title, rest = _title_and_rest(raw)
        kind = "op" if heading and "Operator" in heading else "question"
        prefix = "OP: " if kind == "op" else "Q: "
        full_title = (prefix + title)[:TITLE_CAP]
        description = "\n".join(p for p in [rest, *body] if p)
        ident = _norm_id(match.group(1))
        bullets.append(Question(ident, kind, full_title, description, heading,
                                _classify_wait(ident, kind, full_title, raw)))
    won = {entry.id for entry in bullets}
    tables = []
    seen_table = set()
    for heading, ident, why, step, where, kind_cell in _table_rows(text):
        if ident in won or ident in seen_table:
            continue
        seen_table.add(ident)
        title, _ = _title_and_rest(why.strip())
        kind = "op" if ident.startswith("OS-") else "question"
        prefix = "OP: " if kind == "op" else "Q: "
        full_title = (prefix + title)[:TITLE_CAP]
        step, where = step.strip(), where.strip()
        step, choices = parse_options(step, why)
        if step and where:
            description = f"{step} — {where}"
        else:
            description = step or where
        wait = (_KIND_CELLS[kind_cell] if kind_cell
                else _classify_wait(ident, kind, full_title, why))
        tables.append(Question(ident, kind, full_title, description, heading, wait, choices))
    return tables + bullets


#: the status words that make an id an *answer* (ADDENDUM 2)
_STATUS_RE = re.compile(r"(?:done|resolved|obsolete|superseded|answered|cancelled|closed)",
                        re.IGNORECASE)
#: an id straight after by/with/via/see is a reference, not an answer
_REF_BEFORE_RE = re.compile(r"(?i)\b(?:by|with|via|see)\s+$")
_ID_OCCURRENCE_RE = re.compile(r"(?<![A-Za-z0-9-])(" + ID + r")(?![A-Za-z0-9-])")
_D_IN_GROUP_RE = re.compile(r"D-[0-9]+")
_GROUP_RE = re.compile(r"\(([^()]*)\)")
_CLAUSE_ENDS = (", ", ". ", "(")


def _d_groups(text):
    """``(start, [normalised D-ids])`` per parenthesised group naming a D-id."""
    out = []
    for match in _GROUP_RE.finditer(text):
        ids = [_norm_d(v) for v in _D_IN_GROUP_RE.findall(match.group(1))]
        if ids:
            out.append((match.start(), ids))
    return out


def _sentences(text):
    """``(start, end)`` spans: one per sentence (split after ``. ``/``! ``/``? ``)."""
    spans, start = [], 0
    for match in re.finditer(r"[.!?]\s+", text):
        spans.append((start, match.end()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _clause_start(text, pos):
    """Is *pos* the start of a clause (line start, after ``. `` / `, ` / `` + `` / `/`)?"""
    if pos == 0:
        return True
    before = text[max(0, pos - 3):pos]
    return before[-1] == "/" or before[-2:] in (", ", ". ", "- ") \
        or before[-3:] == " + "


def _has_status(text, end):
    """Is a status word ahead of *end*, up to the next clause or sentence end?"""
    stop = len(text)
    for token in _CLAUSE_ENDS:
        found = text.find(token, end)
        if found != -1:
            stop = min(stop, found)
    return bool(_STATUS_RE.search(text[end:stop]))


def _ids_for(groups, spans, pos, last_ids):
    """*pos*'s sentence's D-ids, falling back to the line's last D-id group."""
    span = next((s for s in spans if s[0] <= pos < s[1]), spans[-1])
    own = []
    for start, ids in groups:
        if span[0] <= start < span[1]:
            own += [i for i in ids if i not in own]
    return own or list(last_ids)


def _sentence_text(text, spans, pos):
    for start, end in spans:
        if start <= pos < end:
            sent = text[start:end]
            break
    else:
        sent = text
    # drop the D-id groups (and keep prose groups such as "(some text)")
    sent = _GROUP_RE.sub(lambda m: "" if _D_IN_GROUP_RE.search(m.group(1))
                         else m.group(0), sent)
    return " ".join(sent.split())


def parse_answers(text):
    """``(answers, skipped)``: one ``Answer`` per answered id under ``## <date>``.

    One line may answer several ids: each id that *starts* a clause (line start,
    or after ``. `` / `, ` / `` + `` / `/`) and is followed — directly or after
    joined ids — by one of done/resolved/obsolete/superseded/answered/cancelled/
    closed is an answer.  An id after by/with/via/see or inside parentheses is a
    reference, not an answer.  Each answered id takes the D-ids of its own
    sentence, else the line's last D-id group; ``/`` separates D-ids like `,`.
    A classic single-id bullet is exempt from the status rule and takes the
    line's last group, as before (ADDENDUM 2).
    """
    answers, skipped = [], []
    for heading, match, body in _entries(text, _BULLET_RE):
        if not heading or not _DATE_RE.match(heading):
            continue
        lead, rest = _norm_id(match.group(1)), match.group(2).strip()
        head = f"{lead} {rest}" if rest else lead
        full = " ".join([head, *body]).strip()
        groups = _d_groups(full)
        if not groups:
            skipped.append(Skipped(lead, "no D-id"))
            continue
        spans, last_ids = _sentences(full), groups[-1][1]
        parens = [m.span() for m in _GROUP_RE.finditer(full)]
        candidates = []
        for found in _ID_OCCURRENCE_RE.finditer(full):
            pos = found.start()
            if any(a <= pos < b for a, b in parens):
                continue  # inside parentheses: a reference (e.g. "superseded by X (y)")
            if not _clause_start(full, pos) or _REF_BEFORE_RE.search(full[:pos]):
                continue  # mid-prose or after by/with/via/see: a reference
            candidates.append((_norm_id(found.group(1)), pos))
        if len(candidates) == 1:
            # the classic single-id bullet: it is the answer whatever it says
            answered = list(candidates)
        else:
            answered = [(ident, pos) for ident, pos in candidates
                        if _has_status(full, pos + len(ident))]
        if not answered:
            skipped.append(Skipped(lead, "no status word"))
            continue
        emitted = set()
        for ident, pos in sorted(answered, key=lambda item: item[1]):
            if ident in emitted:
                continue
            emitted.add(ident)
            answers.append(Answer(ident, _ids_for(groups, spans, pos, last_ids),
                                  heading, _sentence_text(full, spans, pos)))
    return answers, skipped


def _norm_d(value):
    return f"D-{int(value.split('-')[1])}"


def parse_decisions(text):
    """The set of ``D-<n>`` ids that have a ``| D-n |`` row outside code blocks."""
    ids, fenced = set(), False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced or line.startswith("    ") or line.startswith("\t"):
            continue
        match = _DECISION_ROW_RE.match(line)
        if match:
            ids.add(f"D-{int(match.group(1))}")
    return ids


_WORD_SECRET_RE = re.compile(
    r"(?i)\b(token|key|prefix|secret|password|passwd|bearer)\b"
    r"(\s*[:=]\s*|\s+)([A-Za-z0-9_+/=.-]{8,})")
_RUNS = (re.compile(r"\b[0-9a-fA-F]{32,}\b"),
         re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"))
MASK = "[redacted]"


def redact(text):
    """Mask token-like strings; returns ``(text, number_of_masks)``."""
    count = 0

    def word(match):
        nonlocal count
        value, sep = match.group(3), match.group(2)
        if sep.strip() == "" and not any(c.isdigit() for c in value):
            return match.group(0)  # prose such as "key management"
        count += 1
        return match.group(1) + sep + MASK

    text = _WORD_SECRET_RE.sub(word, text)

    def run(match):
        nonlocal count
        if match.group(0) == MASK:
            return MASK
        count += 1
        return MASK

    for pattern in _RUNS:
        text = pattern.sub(run, text)
    return text, count
