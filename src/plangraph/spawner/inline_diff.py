"""Inline the reviewed diff for review legs that give no verdict in tool mode.

luna and mimo-pro (D-3134/D-3173) only answer when the change is in the prompt.
The card names the change as ``diff_ref`` = ``{"repo", "base", "head"}``
(validated by ``planner.seats._check_diff_ref``); the spawner runs ``git diff``
itself so no caller pastes diffs by hand.
"""
from __future__ import annotations

import re
import subprocess

from plangraph.planner.seats import _check_diff_ref
from plangraph.routing_errors import SpawnRefused
from plangraph.routing_support import background_process_options
from .env_redact import _before_review_output, _diff_block, PrivateKeyScan

HEADER = "\n\n=== INLINE DIFF (base {base}, head {head}; assess the change from this material without calling tools) ===\n"
FOOTER = "\n=== END INLINE DIFF ===\n"


class InlineDiffError(Exception):
    """The diff could not be produced or does not fit the leg."""

    def __init__(self, message, *, chars=None, max_chars=None):
        super().__init__(message)
        self.chars, self.max_chars = chars, max_chars


def changed_paths(diff_ref) -> tuple[str, ...]:
    """Read the reviewed paths from the same merge-base range as the inline diff."""
    _check_diff_ref(diff_ref)
    try:
        proc = subprocess.run(
            _diff_command(diff_ref, "--name-only", "--no-renames", "-z"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False, **background_process_options())
    except (OSError, subprocess.TimeoutExpired):
        return ()
    return tuple(filter(None, (proc.stdout or "").split("\0"))) if proc.returncode == 0 else ()


_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
WITHHELD = "[REDACTED: hunk overlaps a private key block]\n"


def _git_text(repo, *args, timeout):
    proc = subprocess.run(["git", "-C", repo, *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
                          **background_process_options())
    return proc.stdout if proc.returncode == 0 and proc.stdout is not None else None


def _key_lines(content):
    """1-based numbers of the lines inside or touching a private-key block of ``content``."""
    scan = PrivateKeyScan()
    return {n for n, line in enumerate(content.split("\n"), 1) if scan.feed(line)}


def _count(text):
    return 1 if text is None else int(text)


_INDEX = re.compile(r'^index ([0-9a-f]+)\.\.([0-9a-f]+)')


def _withhold_key_hunks(diff, repo, timeout):
    """The diff with the body of every hunk that overlaps a private-key block of either side replaced.

    A hunk cut from the middle of a key shows base64 body lines without any marker, so the
    blocks are found in the whole blob of each side, named by the ids of the diff's own
    ``index`` line (file names may be quoted). A side that cannot be read withholds."""
    out, cache, blobs, in_hunk, keep = [], {}, {}, False, True

    def key_lines(blob):
        if blob not in cache:
            text = _git_text(repo, "cat-file", "blob", blob, timeout=timeout)
            cache[blob] = None if text is None else _key_lines(text)
        return cache[blob]

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            blobs, in_hunk, keep = {}, False, True
        elif not in_hunk and _INDEX.match(line):
            old, new = _INDEX.match(line).groups()
            # An all-zero id is the absent side of an added or deleted file.
            blobs = {"-": None if not old.strip("0") else old, "+": None if not new.strip("0") else new}
        hunk = _HUNK.match(line)
        if hunk:
            in_hunk, keep, keyed = True, True, False
            for mark, first, count in (("-", hunk[1], hunk[2]), ("+", hunk[3], hunk[4])):
                if mark not in blobs:   # no index line: this side cannot be named
                    keep, keyed = False, True
                    continue
                if blobs[mark] is None:
                    continue
                lines = key_lines(blobs[mark])
                keyed = keyed or lines is None or bool(lines)
                if lines is None or any(n in lines for n in range(int(first), int(first) + _count(count))):
                    keep = False
            # The heading after the second @@ is a copy of an earlier line of the file: a keyed file loses it.
            head = line[:hunk.end()] + line[len(line.rstrip("\r\n")):] if keyed or not keep else line
            out.append(head if keep else head + WITHHELD)
        elif keep:
            out.append(line)
    return "".join(out)


def _diff_command(diff_ref, *flags):
    paths = tuple(diff_ref.get("paths") or ())
    return ["git", "--literal-pathspecs", "-C", diff_ref["repo"], "diff", "--no-ext-diff", "--no-color",
            "--no-textconv", *flags, f"{diff_ref['base']}...{diff_ref['head']}", "--", *paths]


def ensure_nonempty_diff(diff_ref, *, timeout: float = 60) -> None:
    """Raise ``empty_diff`` when base...head changes nothing, so the card fails once before any leg.

    A diff that cannot be read here (missing repo, unknown revision, timeout) is left to the
    legs, which report their own cause."""
    try:
        _check_diff_ref(diff_ref)
        proc = subprocess.run(_diff_command(diff_ref, "--quiet"), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, **background_process_options())
    except (SpawnRefused, OSError, subprocess.TimeoutExpired):
        return
    if proc.returncode == 0:
        raise InlineDiffError(f"empty_diff: no change between base {diff_ref['base']} and head {diff_ref['head']}")


def refuse_empty_diff(card, role, *, dry_run=False) -> None:
    """The one card-level empty diff check of a review spawn: ``SpawnRefused('empty_diff: ...')``."""
    if role != "review" or dry_run or (card or {}).get("diff_ref") is None:
        return
    try:
        ensure_nonempty_diff(card["diff_ref"])
    except InlineDiffError as exc:
        raise SpawnRefused(str(exc)) from None


def inline_diff_prompt(prompt: str, diff_ref, *, max_chars: int, timeout: float = 60,
                       allow_document: bool = False) -> str:
    """The prompt with the diff inlined. ``diff_ref is None`` returns the prompt
    unchanged only when ``allow_document`` says the caller already decided a
    document review (no diff) is allowed; otherwise the leg must not proceed."""
    if diff_ref is None:
        if allow_document:
            return prompt
        raise InlineDiffError("no diff_ref for inline-only leg")
    try:
        _check_diff_ref(diff_ref)
    except SpawnRefused as exc:
        raise InlineDiffError(str(exc)) from None
    base, head = diff_ref["base"], diff_ref["head"]
    try:
        proc = subprocess.run(
            _diff_command(diff_ref),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
            **background_process_options())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InlineDiffError(f"git diff failed: {type(exc).__name__}") from None
    if proc.stdout is None or proc.stderr is None:
        raise InlineDiffError("git diff reader failure: stdout or stderr is None")
    if proc.returncode != 0:
        raise InlineDiffError("git diff failed: " + (proc.stderr.strip().splitlines() or ["exit %d" % proc.returncode])[0][:200])
    if not proc.stdout.strip():
        raise InlineDiffError("empty diff")
    if len(proc.stdout) > max_chars:
        raise InlineDiffError(f"prompt_too_large: diff too large for leg: {len(proc.stdout)} > {max_chars} chars",
                              chars=len(proc.stdout), max_chars=max_chars)
    try:
        shown = _withhold_key_hunks(proc.stdout, diff_ref["repo"], timeout) if "@@ -" in proc.stdout else proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InlineDiffError(f"private key check failed: {type(exc).__name__}") from None
    block = HEADER.format(base=base, head=head) + _diff_block(shown) + FOOTER
    return _before_review_output(prompt, block)
