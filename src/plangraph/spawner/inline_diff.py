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
from .env_redact import _source_block, private_key_line
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


_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
_INDEX = re.compile(r'(?m)^index ([0-9a-f]{40,64})\.\.([0-9a-f]{40,64})(?: |$)')


def _private_key_ranges(repo, blob, timeout):
    if not blob.strip('0'):
        return ()
    git = ['git', '-C', repo, 'cat-file']
    try:
        opts = dict(stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                    check=True, **background_process_options())
        size = int(subprocess.run(git + ['-s', blob], **opts).stdout)
        if not 0 <= size <= 8 * 1024 * 1024:
            return None
        data = subprocess.run(git + ['blob', blob], **opts).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    ranges, start, inside = [], None, False
    for number, line in enumerate(data.decode('utf-8', 'replace').split('\n'), 1):
        overlap, inside = private_key_line(line, inside)
        if overlap and start is None:
            start = number
        if start is not None and not inside:
            ranges.append((start, number))
            start = None
    if start is not None:
        ranges.append((start, number))
    return ranges


def _scrub_hunks(diff, repo, timeout):
    # Read immutable old and new blobs because a hunk can start inside a key.
    parts = re.split(r'(?m)(^@@ .*?\n|^diff --git .*?\n)', diff)
    ranges = ()
    for index in range(1, len(parts), 2):
        header = parts[index]
        if header.startswith('diff --git'):
            blobs = _INDEX.search(parts[index + 1])
            ranges = [_private_key_ranges(repo, blob, timeout) for blob in blobs.groups()] if blobs else [None]
            continue
        hunk = _HUNK.match(header)
        if hunk and any(keys is None or any(
            int(start) <= last and first < int(start) + int(count or 1)
            for first, last in keys
        ) for keys, start, count in zip(ranges, hunk.group(1, 3), hunk.group(2, 4))):
            parts[index:index + 2] = ['[autoos:source hunk withheld]\n', '']
        else:
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
             "--no-textconv", "--full-index", f"{base}...{head}", "--", *paths],
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
    return prompt + HEADER.format(base=base, head=head) + _source_block(_scrub_hunks(proc.stdout, diff_ref["repo"], timeout)) + FOOTER
