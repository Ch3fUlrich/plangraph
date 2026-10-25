"""§0.2c/d seat output -> findings, strict seat outcome, fix brief by code.

``parse_findings`` reads a seat's reply and returns every severity-tagged
finding verbatim (with its continuation lines) and a stable id: the id the seat
wrote (``F3``, ``DS-2``, ``#4``) or ``<seat>-F<n>``.  ``seat_outcome`` turns a
reply into ``changes`` / ``pass`` / ``None``: any HIGH/MED finding wins over the
VERDICT line, and an empty, failed, billing or sandbox-fence reply is no
verdict at all (re-run the seat, never count it as a pass).  ``build_fix_brief``
writes the fixer brief from the findings; ``missing_ids`` is the spawner check
that a brief still carries every finding id.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["BLOCKING", "seat_text", "parse_seat_verdict", "parse_findings", "blocking", "failure_reason", "seat_outcome",
           "FIRST_REVIEW_SEATS", "first_review", "findings_from_files", "build_fix_brief", "missing_ids"]

BLOCKING = frozenset({"HIGH", "MED", "CRITICAL", "BLOCKER", "MAJOR"})
_SEV = r"(?P<sev>CRITICAL|BLOCKER|HIGH|MAJOR|MEDIUM|MED|MINOR|LOW|NIT)"
_ID = r"(?P<id>#\d+|[A-Z][A-Z0-9]{0,11}-?\d{1,3})"
_LEAD = r"^\s*(?:>\s*)?(?:[-*+]|\d{1,3}[.)]|\|)?\s*"
_WRAP = r"[\s*_`\[\](){}]*"
# "- HIGH foo", "1. **[MED]** foo", "| F2 | HIGH | foo", "H1 (HIGH): foo", "F3 [HIGH] foo"
_FINDING = re.compile(
    _LEAD + _WRAP + r"(?:" + _ID + _WRAP + r"[:.|\-–—]?" + _WRAP + r")?" + _SEV + r"\b(?P<rest>.*)$",
    re.I)
# "Severity: HIGH" anywhere on the line.
_SEVERITY_FIELD = re.compile(r"\bseverity\s*[:=]\s*[*_`\[]*" + _SEV + r"\b", re.I)
_NEGATED = re.compile(r"^[\s*_`\])}:|=-]*(?:none|n/?a|0|no|nil|-|—|–)?\s*(?:findings?)?[\s*_`|]*$"
                      r"|^[\s*_`\])}]*[|/]\s*(?:HIGH|MED|MEDIUM|LOW)\b"
                      r"|^[\s*_`\])}:|=-]*(?:none|n/?a|0|nil)\b", re.I)
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict\b[\s*_`]*:?[\s*_`]*(?P<word>[a-z_-]*)", re.I)
_PASS_WORDS = frozenset({"pass", "approve", "approved", "accept", "accepted", "lgtm"})
_CHANGE_WORDS = frozenset({"fix", "fail", "failed", "changes", "change", "reject", "rejected",
                           "request_changes", "request-changes", "halt", "block", "blocked"})
# A row/line reporting an earlier finding as closed is not a new finding.
_CLOSED = re.compile(r"(?<!NOT )(?<!PARTIALLY )(?<!NOT YET )\b(?:FIXED|RESOLVED|WITHDRAWN|REFUTED)\b")
_HEADING = re.compile(r"^\s*#{1,6}\s")
#: A PASS reply shorter than this (VERDICT line removed) that carries a
#: failure signature is a provider error, not a review.
FAILED_TEXT_MAX = 400
_FAILED = re.compile(
    r"insufficient (?:balance|credits?|quota)|billing|payment required|\b402\b|quota exceeded"
    r"|rate.?limit(?:ed)?\b|\b429\b|too many requests|sandbox fence|outside (?:the )?(?:allowed|sandbox)"
    r"|permission denied|cross-family fence|context length exceeded|max_tokens"
    r"|internal server error|\b50[23]\b|service unavailable|connection (?:reset|refused)", re.I)


def _norm_sev(sev):
    sev = sev.upper()
    return {"MEDIUM": "MED"}.get(sev, sev)


def seat_text(raw):
    """``(text, is_error)``: the reply inside a stream-json/JSON wrapper, else *raw*."""
    raw = raw or ""
    texts, error = [], False
    for line in raw.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("result"), str):
            texts.append(doc["result"])
            error = error or bool(doc.get("is_error"))
    if texts:
        return texts[-1], error
    return raw, False


def parse_seat_verdict(text):
    """Last VERDICT line: ``pass``/``changes``/``None`` (colon optional, ``fail`` = changes)."""
    for line in reversed((text or "").splitlines()):
        m = _VERDICT_LINE.match(line)
        if not m:
            continue
        rest = line[m.end():]
        if re.match(r"\s*[|/]", rest):
            return None  # echoed "PASS | FIX" template, not a decision
        word = m.group("word").lower()
        if word in _PASS_WORDS:
            return "pass"
        if word in _CHANGE_WORDS:
            return "changes"
        return None
    return None


def _match(line):
    if _CLOSED.search(line):
        return None
    m = _FINDING.match(line)
    if m and not _NEGATED.match(m.group("rest") or ""):
        return m.group("id"), _norm_sev(m.group("sev"))
    f = _SEVERITY_FIELD.search(line)
    if f:
        rest = line[f.end():]
        if not _NEGATED.match(rest):
            return None, _norm_sev(f.group("sev"))
    return None


def parse_findings(text, seat="seat"):
    """Every severity-tagged finding in *text*: ``[{id, severity, text}]``."""
    lines = (text or "").splitlines()
    out = []
    current = None
    for line in lines:
        hit = None if _VERDICT_LINE.match(line) else _match(line)
        if hit:
            ident, sev = hit
            current = {"id": ident, "severity": sev, "text": line.strip()}
            out.append(current)
            continue
        if current is None:
            continue
        if not line.strip() or _HEADING.match(line) or _VERDICT_LINE.match(line):
            current = None
            continue
        current["text"] += "\n" + line.rstrip()
    seen = set()
    for n, finding in enumerate(out, 1):
        ident = finding["id"] or f"{seat}-F{n}"
        if ident in seen:
            ident = f"{seat}-F{n}"
        seen.add(ident)
        finding["id"] = ident
    return out


def blocking(findings):
    return [f for f in findings if f["severity"] in BLOCKING]


def failure_reason(text, status=None, is_error=False, exit_class=None, liveness=None, output_cut=False):
    """Why a seat reply carries no verdict (re-run it), or ``None``."""
    if liveness:
        return f"run liveness {liveness}"
    if exit_class == "cancelled":
        return "run cancelled"
    if output_cut:
        return "output cut"
    if status is not None and status not in ("completed", "succeeded", "ok", "stop", "end_turn"):
        return f"seat run {status}"
    if is_error:
        return "seat harness reported is_error"
    if not (text or "").strip():
        return "empty output"
    return None


def _body_len(text):
    return len("".join(l for l in text.splitlines() if not _VERDICT_LINE.match(l)).strip())


def seat_outcome(raw, verdict=None, status=None, seat="seat", exit_class=None, liveness=None, output_cut=False):
    """``(outcome, reason, findings)``; outcome is ``changes``, ``pass`` or ``None``.

    *raw* is the seat output (plain text or a stream-json/JSON wrapper).
    *verdict* overrides the VERDICT-line parse when the caller already has one
    (``pass``/``fix``/``halt``/``changes``).  ``None`` means no verdict: re-run
    the seat, never count it as a pass.
    """
    text, is_error = seat_text(raw)
    reason = failure_reason(text, status, is_error, exit_class=exit_class, liveness=liveness, output_cut=output_cut)
    if reason:
        return None, reason, []
    findings = parse_findings(text, seat)
    hard = blocking(findings)
    if hard:
        ids = ", ".join(f["id"] for f in hard)
        return "changes", f"{len(hard)} HIGH/MED finding(s) in body: {ids}", findings
    if verdict is None:
        verdict = parse_seat_verdict(text)
    m = _FAILED.search(text)
    if verdict == "pass" and m and _body_len(text) < FAILED_TEXT_MAX:
        return None, f"failed seat output ({m.group(0)!r}) beside VERDICT: PASS", findings
    if verdict in ("fix", "halt", "changes"):
        return "changes", f"verdict {verdict}", findings
    if verdict == "pass":
        return "pass", "verdict pass, no HIGH/MED in body", findings
    why = f"failed seat output ({m.group(0)!r})" if m else "no VERDICT line"
    return None, f"{why} and no HIGH/MED finding", findings


#: §0.2c: a first review needs this many seats, each returning text.
FIRST_REVIEW_SEATS = 2


def first_review(outputs, need=FIRST_REVIEW_SEATS):
    reasons, outcomes = [], []
    for seat, item in outputs.items():
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict):
            r = item[1]
            outcome, reason, _ = seat_outcome(item[0], status=r.get("status"), seat=seat,
                                              exit_class=r.get("exit_class"), liveness=r.get("liveness"),
                                              output_cut=bool(r.get("output_cut", False)))
        else:
            outcome, reason, _ = seat_outcome(item[0] if isinstance(item, tuple) else item, seat=seat)
        outcomes.append(outcome)
        reasons.append(f"{seat}: {outcome or 'no verdict'} ({reason})")
    if len(outputs) < need:
        return None, reasons + [f"{len(outputs)} seat(s), first review needs {need} with text"]
    return (None if None in outcomes else ("changes" if "changes" in outcomes else "pass")), reasons


def findings_from_files(paths, card=None, seat_classes=None):
    classes = seat_classes or ((card or {}).get("seat_classes") if isinstance(card, dict) else {}) or {}
    out = []
    for path in paths:
        p = Path(path)
        fp, res, stem = (p / "stdout.log", p / "result.json", p.name) if p.is_dir() else (p, p.parent / "result.json", p.stem)
        meta = json.loads(res.read_text("utf-8")) if res.is_file() else classes.get(stem)
        if meta and (meta.get("liveness") or meta.get("exit_class") == "cancelled" or meta.get("output_cut") or meta.get("status") not in (None, "completed")):
            continue
        if fp.is_file():
            text, _ = seat_text(fp.read_text("utf-8", "replace"))
            out.extend(parse_findings(text, stem))
    return out


_CLASS_RULES = (
    "Fix the whole defect class, not the instance: for every finding, list every place in the "
    "code with the same defect (grep), fix all of them, and name them in your report.",
    "Check every change against the invariant table; a fix that breaks an invariant is a new defect.",
    "Write or extend a failing test per finding first; then run the FULL affected suite "
    "(tools/affected_tests.py, all of it) before you commit, not only the new tests.",
    "Do not merge main into the lane. Do not touch files no finding names unless the class sweep needs it.",
    "The run ends only when each finding is fixed or refuted by a named test; report per id.",
)


def build_fix_brief(findings, invariants="", note=""):
    """Fixer brief: findings verbatim (all of them), invariants, class-fix rules, then *note*."""
    parts = ["# Fix brief (built by code, PG-EFFICIENCY-GATES; nothing may be dropped)", "",
             f"## Findings ({len(findings)}, all must be handled)"]
    for f in findings:
        parts.append(f"- [ ] {f['id']} ({f.get('severity', '?')}):")
        parts.extend("    " + line for line in str(f.get("text", "")).splitlines())
    parts += ["", "## Invariants", (invariants or "").strip() or
              "(no design invariant table attached: derive one from the code before editing)", "",
              "## Rules"]
    parts += [f"{n}. {rule}" for n, rule in enumerate(_CLASS_RULES, 1)]
    if note and note.strip():
        parts += ["", "## L1 note (appended)", note.strip()]
    return "\n".join(parts) + "\n"


def missing_ids(brief, findings):
    """Finding ids that *brief* does not mention."""
    text = brief or ""
    return [f["id"] for f in findings
            if not re.search(r"(?<![\w-])" + re.escape(str(f["id"])) + r"(?![\w-])", text)]
