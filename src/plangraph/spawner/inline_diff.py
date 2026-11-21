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
from .env_redact import _diff_block, PrivateKeyScan

HEADER = "\n\n=== INLINE DIFF (base {base}, head {head}; this leg reads the change only from here) ===\n"
FOOTER = "\n=== END INLINE DIFF ===\n"


class InlineDiffError(Exception):
    """The diff could not be produced or does not fit the leg."""

    def __init__(self, message, *, chars=None, max_chars=None):
        super().__init__(message)
        self.chars, self.max_chars = chars, max_chars


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


def _withhold_key_hunks(diff, repo, old_rev, new_rev, timeout):
    """The diff with the body of every hunk that overlaps a private-key block of either side replaced.

    A hunk cut from the middle of a key shows base64 body lines without any marker, so the
    blocks are found in the whole file of each side. A side that cannot be read withholds."""
    out, cache, files, in_hunk, keep = [], {}, {}, False, True

    def key_lines(rev, name):
        if (rev, name) not in cache:
            text = _git_text(repo, "cat-file", "blob", f"{rev}:{name}", timeout=timeout)
            cache[rev, name] = None if text is None else _key_lines(text)
        return cache[rev, name]

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            files, in_hunk, keep = {}, False, True
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            name = line[4:].rstrip("\r\n").split("\t")[0]
            # None: no such side (/dev/null); False: a name we cannot resolve safely (quoted).
            files[line[0]] = None if name == "/dev/null" else (
                name[2:] if name[:2] in ("a/", "b/") and not name.startswith('"') else False)
        hunk = _HUNK.match(line)
        if hunk:
            in_hunk, keep, keyed = True, True, False
            for mark, rev, first, count in (("-", old_rev, hunk[1], hunk[2]), ("+", new_rev, hunk[3], hunk[4])):
                name = files.get(mark)
                if name is None:
                    continue
                lines = key_lines(rev, name) if name else None
                keyed = keyed or lines is None or bool(lines)
                if lines is None or any(n in lines for n in range(int(first), int(first) + _count(count))):
                    keep = False
            # The heading after the second @@ is a copy of an earlier line of the file: a keyed file loses it.
            head = line[:hunk.end()] + line[len(line.rstrip("\r\n")):] if keyed else line
            out.append(head if keep else head + WITHHELD)
        elif keep:
            out.append(line)
    return "".join(out)


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
    paths = tuple(diff_ref.get("paths") or ())
    try:
        proc = subprocess.run(
            ["git", "--literal-pathspecs", "-C", diff_ref["repo"], "diff", "--no-ext-diff", "--no-color",
             "--no-textconv", f"{base}...{head}", "--", *paths],
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
    shown = proc.stdout
    try:
        if "@@ -" in shown:
            merge_base = (_git_text(diff_ref["repo"], "merge-base", base, head, timeout=timeout) or "").strip()
            shown = _withhold_key_hunks(shown, diff_ref["repo"], merge_base or base, head, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InlineDiffError(f"private key check failed: {type(exc).__name__}") from None
    return prompt + HEADER.format(base=base, head=head) + _diff_block(shown) + FOOTER
