"""Parsers and redaction for md_sync (QUESTIONS.md, ANSWERS.md, decisions-log.md)."""
import re
from collections import namedtuple

Question = namedtuple("Question", "id kind title description heading")
Answer = namedtuple("Answer", "id decisions date text")
Skipped = namedtuple("Skipped", "id reason")

ID = r"(?:OS-[0-9]+|Q-[A-Z0-9][A-Z0-9-]*)"
_BULLET_RE = re.compile(r"^- +(" + ID + r")(?![A-Za-z0-9-])[ :]*(.*)$")
_ANY_BULLET_RE = re.compile(r"^- ")
_HEADING_RE = re.compile(r"^## +(.*?)\s*$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_SENTENCE_RE = re.compile(r"^(.+?[.?!])(?:\s+|$)")
_TRAILING_D_RE = re.compile(
    r"\(\s*(D-[0-9]+(?:\s*,\s*D-[0-9]+)*)\s*\)\s*[.]?\s*$")
_DECISION_ROW_RE = re.compile(r"^\| D-([0-9]+) \|")
_FENCE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
TITLE_MAX = 120
TITLE_CAP = 200


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


def parse_questions(text):
    out = []
    for heading, match, body in _entries(text, _BULLET_RE):
        title, rest = _title_and_rest(match.group(2).strip())
        kind = "op" if heading and "Operator" in heading else "question"
        prefix = "OP: " if kind == "op" else "Q: "
        description = "\n".join(p for p in [rest, *body] if p)
        out.append(Question(match.group(1), kind, (prefix + title)[:TITLE_CAP],
                            description, heading))
    return out


def parse_answers(text):
    """``(answers, skipped)``: entries under ``## <date>`` with / without D-ids."""
    answers, skipped = [], []
    for heading, match, body in _entries(text, _BULLET_RE):
        if not heading or not _DATE_RE.match(heading):
            continue
        full = " ".join([match.group(2).strip(), *body]).strip()
        tail = _TRAILING_D_RE.search(full)
        if not tail:
            skipped.append(Skipped(match.group(1), "no D-id"))
            continue
        ids = [d.strip() for d in tail.group(1).split(",")]
        answers.append(Answer(match.group(1), [_norm_d(d) for d in ids],
                              heading, full[:tail.start()].strip()))
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
