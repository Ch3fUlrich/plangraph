"""Inline the reviewed diff for review legs that give no verdict in tool mode.

luna and mimo-pro (D-3134/D-3173) only answer when the change is in the prompt.
The card names the change as ``diff_ref`` = ``{"repo", "base", "head"}``
(validated by ``planner.seats._check_diff_ref``); the spawner runs ``git diff``
itself so no caller pastes diffs by hand.
"""
from __future__ import annotations

import re
import subprocess
from bisect import bisect_right

from plangraph.planner.seats import _check_diff_ref
from plangraph.routing_errors import SpawnRefused
from plangraph.routing_support import background_process_options
from .env_redact import _source_block
from .redact_tokens import _wrapped_spans

HEADER = "\n\n=== INLINE DIFF (base {base}, head {head}; this leg reads the change only from here) ===\n"
FOOTER = "\n=== END INLINE DIFF ===\n"


def _scrub_hunk(lines):
    edits = [[] for _ in lines]
    for excluded in ('-', '+'):
        body, offsets, indices = [], [0], []
        for index, line in enumerate(lines):
            if line[:1] in (' ', '+', '-') and line[0] != excluded:
                body.append(line[1:])
                offsets.append(offsets[-1] + len(line) - 1)
                indices.append(index)
        for start, end in _wrapped_spans(''.join(body)):
            first = bisect_right(offsets, start) - 1
            for row in range(first, bisect_right(offsets, end - 1)):
                left = max(start, offsets[row]) - offsets[row] + 1
                index = indices[row]
                right = min(end - offsets[row] + 1, len(lines[index].rstrip('\r\n')))
                edits[index].append((left, right))
    for index, spans in enumerate(edits):
        if spans:
            pieces, copied = [], 0
            for left, right in sorted(spans):
                if left >= copied:
                    pieces.extend((lines[index][copied:left], '[REDACTED]'))
                copied = max(copied, right)
            lines[index] = ''.join((*pieces, lines[index][copied:]))
    return ''.join(lines)


def _scrub_hunks(diff):
    # Scan old and new content separately. Diff markers are framing bytes.
    parts = re.split(r'(?m)(^@@ .*?\n|^diff --git .*?\n)', diff)
    for index in range(1, len(parts), 2):
        if parts[index].startswith('@@ '):
            parts[index + 1] = _scrub_hunk(parts[index + 1].splitlines(keepends=True))
    return ''.join(parts)


class InlineDiffError(Exception):
    """The diff could not be produced or does not fit the leg."""

    def __init__(self, message, *, chars=None, max_chars=None):
        super().__init__(message)
        self.chars, self.max_chars = chars, max_chars


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
    return prompt + HEADER.format(base=base, head=head) + _source_block(_scrub_hunks(proc.stdout)) + FOOTER
