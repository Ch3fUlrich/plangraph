"""Strict review-reply parsing: verdict, test-gaming, payload helpers.

Port of the verdict/test-gaming parse used by the one-shot review call,
without any caller, transport, or key handling.

This parse is STRICTER than ``tools/seat_verdict.py``: a verdict line inside
a fenced block, a diff hunk body, or an indented code block is ignored, and
an unclosed fence fails closed (only lines before the first fence marker
count). The keeper re-parses ``review.txt`` with its own looser rule, so a
``pass`` here always parses as pass there, while prose or template echoes
stay missing here.
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

_VERDICT_LINE_RE = re.compile(
    r"(?i)^\s*(?:#{1,6}\s*|[-*+]\s+)*(?P<dec>[*_]{1,2})?VERDICT"
    r"(?:\s*(?P<dec2>[*_]{1,2})\s*:|\s*:)\s*(?P<value>\S.*)$")

_STANDING_ANSWER_RE = re.compile(
    r"^TEST-GAMING: (?:"
    r'(?P<no>no - searched [^"]+: 0 hits)'
    r'|(?P<yes>yes - "(?P<quote>.+)")'
    r"|(?P<unsure>unsure - .+)"
    r")$")

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
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


def _verdict_value(stripped: str) -> str | None:
    """Map one stripped line to pass/fix/halt, or None.

    Quoted (``>``), template (``<`` or ``|``), and prose values
    (``pass, but ...``) are not verdicts; emphasis must balance.
    """
    if stripped.startswith(">") or "<" in stripped or "|" in stripped:
        return None
    m = _VERDICT_LINE_RE.match(stripped)
    if not m:
        return None
    dec, dec2, value = m.group("dec"), m.group("dec2"), m.group("value")
    if dec != dec2 and not (dec and dec2 is None and value.startswith(dec)):
        return None
    for _ in range(2):
        value = value.strip().strip("*_` ").strip()
        if value[-1:] in (".", "!", "?"):
            value = value[:-1]
    value = value.strip().lower()
    if value in ("pass", "approve"):
        return "pass"
    if value in ("fix", "fail", "fail-with-findings",
                 "change", "changes", "reject"):
        return "fix"
    if value == "halt":
        return "halt"
    return None


def parse_verdict(text: str) -> str | None:
    """Last qualifying ``VERDICT:`` line mapped to pass/fix/halt, or None."""
    raws = [_ANSI_RE.sub("", line) for line in (text or "").splitlines()]
    found = None
    found_before_fence = None
    fence = None
    first_marker = None
    hunk = None
    for i, raw in enumerate(raws):
        if hunk is not None:
            old, new = hunk
            if old <= 0 and new <= 0:
                hunk = None
            elif raw.startswith(" "):
                hunk = (old - 1, new - 1)
                continue
            elif raw.startswith("-"):
                hunk = (old - 1, new)
                continue
            elif raw.startswith("+"):
                hunk = (old, new - 1)
                continue
            elif raw.startswith("\\"):
                continue
            elif raw == "":
                hunk = (old - 1, new - 1)
                continue
            else:
                hunk = None
        if fence is not None:
            m = _FENCE_CLOSE_RE.match(raw)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        if raw.startswith(("diff --git", "index ", "---", "+++")):
            continue
        m = _HUNK_RE.match(raw)
        if m:
            hunk = (int(m.group(1) or 1), int(m.group(2) or 1))
            continue
        m = _FENCE_OPEN_RE.match(raw)
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            if first_marker is None:
                first_marker = i
            continue
        if _indent_columns(raw) >= 4:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        value = _verdict_value(stripped)
        if value:
            found = value
            if first_marker is None:
                found_before_fence = value
    return found_before_fence if fence is not None else found


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
        return doc


def judge(text: str, finish_reason: str | None,
          material: str = "") -> Verdict:
    """Judge a reply text with its finish reason and reviewed material."""
    empty = (text or "").strip() == ""
    if empty:
        return Verdict(verdict=None, finish_reason=finish_reason,
                       test_gaming="missing", test_gaming_quote=None,
                       empty=True)
    verdict = parse_verdict(text or "")
    gaming, quote = parse_test_gaming(text or "", material or "")
    return Verdict(verdict=verdict, finish_reason=finish_reason,
                   test_gaming=gaming, test_gaming_quote=quote, empty=False)
