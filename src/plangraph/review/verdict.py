"""Review-reply parsing: verdict, test-gaming, payload helpers.

The verdict parse is identical to ``tools/seat_verdict.py``: the last
``VERDICT:`` line outside a ``` fence or ``>`` quote decides; indented
and hunk lines count.  Only the test-gaming check and the finish-reason
gating here are stricter.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from html import unescape
from typing import Any

__all__ = [
    "VERDICTS",
    "parse_verdict",
    "parse_test_gaming",
    "reply_text",
    "finish_reason",
    "Verdict",
    "judge",
    "Findings",
    "classify_findings",
    "diff_paths",
    "diff_binding",
    "bound_diff_paths",
]

VERDICTS = ("pass", "fix", "halt")

PASS_WORDS = frozenset({"pass", "approve", "accept", "go"})
FIX_WORDS = frozenset({"fail", "fix", "halt", "change", "changes", "reject", "no", "no-go", "blocked"})
_LOW_ONLY_WORDS = frozenset({"fix", "change", "changes"})
# [*_`]* before/after the word: markdown-bold labels like **MED**: or
# _Medium_ (b) must count, or a bold blocking label reads as low-only pass.
# A blocking word before a dash ("HIGH - y") counts too: the relaxation fails closed.
_BLOCKING_LABEL = re.compile(
    r"\b[*_`]*(?:critical|high|med|medium|major|blocker)[*_`]*\s*(?:[:(]|[-–—]\s)", re.I)
_LOW_LABEL = re.compile(
    r"\b[*_`]*(?:low|lows|nit|nits|minor)[*_`]*(?:\s+only[*_`]*)?\s*[:(]", re.I)
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict[*_`]*(?:[\s*_`]*:[\s*_`]*|[ \t]+(?=[*_`]*(?:[a-z]+|request[ _-]+changes|no[ -]+go)[*_`]*[.,;:!]*[ \t]*$))(.*)$", re.I)
# no "?": "VERDICT: pass?" is a question, not a decision (fail closed)
_WORD_END = r"(?=$|[\s.,;:!)(*_`—–-])(?!\s*[|/])"
_FIRST_WORD = re.compile(r"([a-z]+)" + _WORD_END, re.I)
_REQUEST_CHANGES = re.compile(r"request[\s_-]+changes" + _WORD_END, re.I)

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


def _unquoted_lines(text: str):
    lines = (text or "").splitlines()
    # r3: VERDICT lines inside ``` fences or starting with ">" (quoted template
    # echoes) never decide; the last remaining line keeps the last-line rule.
    fence = None
    for raw in lines:
        if fence is not None:
            close = _FENCE_CLOSE_RE.match(raw)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= fence[1]:
                fence = None
            continue
        opener = _FENCE_OPEN_RE.match(raw)
        if opener:
            fence = (opener.group(1)[0], len(opener.group(1)))
            continue
        if raw.lstrip().startswith(">"):
            continue
        yield raw


# Match the markdown decorations accepted by Rust's review_split::key_tail.
_FILES_READ_LINE = re.compile(r"^[\s>#*_`-]*(?ai:files read)[*_`]*[ \t]*:[ \t*_`]*(.*)$")
_FILE_PATH = re.compile(r"(?:[\w.-]+/)+[\w.-]+|[\w-]+\.[\w.-]+|[\w.-]+:[1-9][0-9]*")


def has_files_read(text: str) -> bool:
    """Check the last unquoted FILES READ declaration for a path."""
    files = ''
    for raw in _unquoted_lines(text):
        if match := _FILES_READ_LINE.match(raw):
            files = match.group(1).strip()
    return (not re.match(r"^(?:none(?:$|[\s(])|(?:inline diff|diff only)(?:$|[\s(])|diff$)", files, re.I)
            and bool(_FILE_PATH.search(files)))


_TOOL_MARKUP = re.compile(
    r"<\s*(?:[\w.-]+:)?(?:invoke_tool|invoke|parameter|function_calls?|tool_use|tool_calls?)\b"
    r"|<\s*function\s*=", re.I)


def has_tool_call_markup(text: str) -> bool:
    """Detect serialized calls printed in the final answer."""
    return bool(_TOOL_MARKUP.search(unescape(text)))


def _scan_verdict(text: str) -> tuple[str | None, bool]:
    match = None
    for raw in _unquoted_lines(text):
        if found := _VERDICT_LINE.match(raw):
            match = found
    if match is not None:
        stripped = match.group(1).strip().lstrip('*_`')
        if _REQUEST_CHANGES.match(stripped):
            return "fix", False
        if re.match(r"no(?:-|\s+)go\b", stripped, re.I):
            word_m = re.match(r"(no(?:-|\s+)go)" + _WORD_END, stripped, re.I)
        else:
            word_m = _FIRST_WORD.match(stripped)
        word = word_m.group(1).lower() if word_m else ""
        if word_m and re.match(r"no(?:-|\s+)go\b", stripped, re.I):
            word = "no-go"
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
    gaming, quote = parse_test_gaming(text or "", material or "")
    return Verdict(verdict=verdict, finish_reason=finish_reason,
                   test_gaming=gaming, test_gaming_quote=quote, empty=False,
                   low_only=low_only)


# Files that make up the supplied review prompt. A finding about one of them, when the
# reviewed diff does not change it and the emitted prompt shows the defect, is a process
# diagnostic for the orchestrator, not a defect of the head.
PROCESS_PATHS = ("review_brief.md", "skills/review/SKILL.md")
_SEVERITY = {"HIGH": "high", "P1": "high", "MED": "med", "P2": "med",
             "LOW": "low", "P3": "low", "PROCESS": "process"}
_FINDING_LINE = re.compile(
    r"^[\s#*\-\[(]*(?:\d+[.)][\s#*\-\[(]*)?(?:FINDING:[\s#*\-\[(]*)?"
    r"(HIGH|MED|LOW|P1|P2|P3|PROCESS)\b[\s:*\])]*(.*)$")
_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_DIFF_COMMAND = re.compile(r"^git diff ([0-9a-f]{7,40}) ([0-9a-f]{7,40})$", re.M)


@dataclass(frozen=True)
class Findings:
    """Finding lines of one review, split into the head's blockers and process diagnostics."""

    high: int = 0
    med: int = 0
    low: int = 0
    process: tuple[str, ...] = ()

    @property
    def blocking(self) -> int:
        """HIGH+MED count of the head; PROCESS lines are not in it."""
        return self.high + self.med


def diff_paths(prompt: str, cwd: str | None = None) -> set[str] | None:
    """Paths the dispatched diff changes, or None when the prompt does not identify it.

    Inline seats carry `diff --git` headers; tool seats carry the `git diff A B` command,
    which is run in `cwd` (the seat's checkout) read-only.
    """
    paths: set[str] = set()
    for old, new in _DIFF_HEADER.findall(prompt):
        paths.update((old, new))
    for base, head in _DIFF_COMMAND.findall(prompt):
        if cwd is None:
            return None
        try:
            out = subprocess.run(["git", "-C", cwd, "diff", "--name-only", base, head],
                                 capture_output=True, text=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        paths.update(out.stdout.split())
    return paths or None


# A PROCESS diagnostic is one fixed statement form: a defect word, then the context field it
# reports. Advice after "fix"/"suggest"/"should"... is cut first, so a finding about other
# wording that merely suggests adding callers is not a statement about the prompt.
_ADVICE = re.compile(r"\b(?:fix|fixes|suggest\w*|should|recommend\w*|replace|change|edit|add|adding|"
                     r"require|consider)\b.*$", re.I | re.S)
_DEFECT = r"(?:omits?|omitted|lacks?|lacking|missing|empty|without|unrendered|unavailable|" \
          r"does not (?:carry|include|supply|render|list|contain))\b[^.;]*?"
_FIELD_WORDS = {
    "public_symbols": r"\bsymbols?\b",
    "callers": r"\bcallers?\b",
    "test_output_paths": r"\btest[- ]output\b|\boutput paths?\b|\btest logs?\b",
}


def _primary_path(rest: str) -> str | None:
    """The file of the finding itself: its first token, not a path cited in the advice."""
    tokens = rest.strip().lstrip("`'\"(").split(None, 1)
    path = tokens[0].strip("`'\",;)").partition(":")[0] if tokens else ""
    for name in PROCESS_PATHS:
        if path == name or path.endswith("/" + name):
            return path
    return None


def _names_defect(rest: str, defects: list[str]) -> bool:
    """The finding states that the prompt omits a context field the prompt really lacks."""
    claim = _ADVICE.sub("", rest.partition(" - ")[2] or rest)
    return any(re.search(_DEFECT + "(?:" + _FIELD_WORDS[field] + ")", claim, re.I)
               for field in defects)


def diff_binding(base: str, head: str, cwd: str) -> tuple[str, set[str]] | None:
    """(sha256, paths) of the endpoint-tree diff base..head as the CLI seat binding computes
    them (`cli_review_binding.rs::binding`: raw records sorted by path, hashed), or None."""
    import hashlib

    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "--no-replace-objects", "diff", "--no-color", "--no-renames",
             "--no-relative", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none",
             "--raw", "--abbrev=40", "-z", base, head, "--"],
            capture_output=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = out.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return None
    changes = sorted((fields[i + 1], fields[i]) for i in range(0, len(fields), 2))
    digest = hashlib.sha256()
    for path, change in changes:
        digest.update(change + b"\0" + path + b"\0")
    return digest.hexdigest(), {path.decode("utf-8", "replace") for path, _ in changes}


def bound_diff_paths(binding: object, cwd: str | None) -> set[str] | None:
    """Paths of the dispatched diff when the seat's recorded `review_binding` digest matches
    the diff recomputed in `cwd`; None (scope unknown, fail closed) otherwise."""
    if not isinstance(binding, dict) or not cwd:
        return None
    found = diff_binding(str(binding.get("base_sha")), str(binding.get("head_sha")), cwd)
    return found[1] if found and found[0] == binding.get("diff_sha256") else None


def classify_findings(text: str, *, prompt: str = "",
                      changed_paths: set[str] | None = None) -> Findings:
    """Count finding lines; a HIGH/MED/PROCESS line about the supplied brief or skill is
    PROCESS only when the emitted `prompt` has an empty item-context field and the
    dispatched diff (`changed_paths`) does not change that file. Otherwise it keeps its
    severity (fail closed); a typed PROCESS line that fails the check counts as MED.
    """
    from .prompt import item_context_defects

    counts = {"high": 0, "med": 0, "low": 0}
    process: list[str] = []
    defects = item_context_defects(prompt) if prompt else []
    verified = bool(defects) and changed_paths is not None
    for raw in _unquoted_lines(text):
        found = _FINDING_LINE.match(raw)
        if not found:
            continue
        kind = _SEVERITY[found.group(1)]
        path = _primary_path(found.group(2))
        about_prompt = (path is not None and verified and _names_defect(found.group(2), defects)
                        and not any(c == name or c.endswith("/" + name) or name.endswith("/" + c)
                                    for c in changed_paths for name in (path,)))
        if kind == "process":
            kind = "process" if about_prompt else "med"
        elif kind != "low" and about_prompt:
            kind = "process"
        if kind == "process":
            process.append(raw.strip())
        else:
            counts[kind] += 1
    return Findings(counts["high"], counts["med"], counts["low"], tuple(process))
