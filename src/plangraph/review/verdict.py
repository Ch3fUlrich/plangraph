"""Review-reply parsing: verdict, test-gaming, payload helpers.

The verdict parse is identical to ``tools/seat_verdict.py``: the last
``VERDICT:`` line decides and fenced, quoted, indented, and hunk lines
all count. Only the test-gaming check and the finish-reason gating
here are stricter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "VERDICTS",
    "parse_verdict",
    "parse_test_gaming",
    "reply_text",
    "finish_reason",
    "Verdict",
    "judge",
]

VERDICTS = ("pass", "fix", "halt")

PASS_WORDS = frozenset({"pass", "approve"})
FIX_WORDS = frozenset({"fix", "halt", "change", "changes", "reject"})
_LOW_ONLY_WORDS = frozenset({"fix", "change", "changes"})
# [*_`]* before/after the word: markdown-bold labels like **MED**: or
# _Medium_ (b) must count, or a bold blocking label reads as low-only pass.
# A blocking word before a dash ("HIGH - y") counts too: the relaxation fails closed.
_BLOCKING_LABEL = re.compile(
    r"\b[*_`]*(?:critical|high|med|medium|major|blocker)[*_`]*\s*(?:[:(]|[-–—]\s)", re.I)
_LOW_LABEL = re.compile(
    r"\b[*_`]*(?:low|lows|nit|nits|minor)[*_`]*(?:\s+only[*_`]*)?\s*[:(]", re.I)
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict[\s*_`]*:[\s*_`]*(.*)$", re.I)
_FIRST_WORD = re.compile(r"([a-z]+)(?=$|[\s.,;:!)(*_`—–-])(?!\s*[|/])", re.I)

_STANDING_ANSWER_RE = re.compile(
    r"^TEST-GAMING: (?:"
    r'(?P<no>no - searched .+: 0 hits)'
    r'|(?P<yes>yes - "(?P<quote>.+)")'
    r"|(?P<unsure>unsure - .+)"
    r")$")

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _indent_columns(line: str) -> int:
    """Leading indent in CommonMark columns (tab advances to next 4)."""
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - (col % 4)
        else:
            break
    return col


def _scan_verdict(text: str) -> tuple[str | None, bool]:
    lines = (text or "").splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        match = _VERDICT_LINE.match(lines[idx])
        if not match:
            continue
        stripped = match.group(1).strip()
        word_m = _FIRST_WORD.match(stripped)
        word = word_m.group(1).lower() if word_m else ""
        if word in PASS_WORDS:
            return "pass", False
        if word == "halt":
            return "halt", False
        if word in _LOW_ONLY_WORDS:
            remainder = stripped[word_m.end():]
            if _LOW_LABEL.search(remainder) and not _BLOCKING_LABEL.search(remainder):
                return "pass", True
            return "fix", False
        if word in FIX_WORDS:
            return "fix", False
        return None, False
    return None, False


def parse_verdict(text: str) -> str | None:
    """Last ``VERDICT:`` line mapped to pass/fix/halt, or None.

    Identical to ``tools/seat_verdict.py``: scan from the end, the
    first matching line decides; the first word maps to the verdict.
    """
    return _scan_verdict(text)[0]


def _standing_outcome(match: re.Match, material: str) -> tuple[str, str | None]:
    """Outcome and quote for one line matching ``_STANDING_ANSWER_RE``."""
    if match.group("yes") is not None:
        quote = match.group("quote") or ""
        if quote and quote in (material or ""):
            return "yes", quote
        return "yes_unverified_quote", quote
    if match.group("no") is not None:
        return "no", None
    return "unsure", None


def parse_test_gaming(text: str, material: str = "") -> tuple[str, str | None]:
    """First qualifying ``TEST-GAMING:`` line, else (``missing``, None)."""
    raws = [_ANSI_RE.sub("", line) for line in (text or "").splitlines()]
    found = None
    found_before_fence = None
    fence = None
    seen_marker = False
    for raw in raws:
        if fence is not None:
            m = _FENCE_CLOSE_RE.match(raw)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        m = _FENCE_OPEN_RE.match(raw)
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            seen_marker = True
            continue
        if _indent_columns(raw) >= 4:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        m = _STANDING_ANSWER_RE.match(stripped)
        if not m:
            continue
        if found is None:
            found = _standing_outcome(m, material or "")
            if not seen_marker:
                found_before_fence = found
    if fence is not None:
        found = found_before_fence
    if found is None:
        return "missing", None
    return found


def reply_text(payload: dict) -> str:
    """Assistant text from a completion payload (str or parts content)."""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def finish_reason(payload: dict) -> str | None:
    """``choices[0].finish_reason`` verbatim, or None when absent."""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) else None


@dataclass(frozen=True)
class Verdict:
    """Judged reply: verdict, finish reason, gaming outcome, emptiness."""

    verdict: str | None
    finish_reason: str | None
    test_gaming: str
    test_gaming_quote: str | None
    empty: bool
    low_only: bool = False

    @property
    def pass_allowed(self) -> bool:
        """True only for pass + stop + gaming no."""
        return (self.verdict == "pass" and self.finish_reason == "stop"
                and self.test_gaming == "no")

    def to_evidence(self) -> dict[str, Any]:
        """Evidence fields: reason, verdict, gaming (+ quote for yes)."""
        doc: dict[str, Any] = {
            "finish_reason": self.finish_reason,
            "verdict": self.verdict,
            "test_gaming": self.test_gaming,
        }
        if self.test_gaming in ("yes", "yes_unverified_quote"):
            doc["test_gaming_quote"] = self.test_gaming_quote
        if self.low_only:
            doc["low_only"] = True
        return doc


def judge(text: str, finish_reason: str | None,
          material: str = "") -> Verdict:
    """Judge a reply text with its finish reason and reviewed material."""
    empty = (text or "").strip() == ""
    if empty:
        return Verdict(verdict=None, finish_reason=finish_reason,
                       test_gaming="missing", test_gaming_quote=None,
                       empty=True)
    verdict, low_only = _scan_verdict(text or "")
    if verdict == "pass":
        # D-2739 §0.2c: a HIGH/MED finding in the body beats the VERDICT line.
        from plangraph.gates.findings import blocking, parse_findings
        if blocking(parse_findings(text or "")):
            verdict = "fix"
    gaming, quote = parse_test_gaming(text or "", material or "")
    return Verdict(verdict=verdict, finish_reason=finish_reason,
                   test_gaming=gaming, test_gaming_quote=quote, empty=False,
                   low_only=low_only)
