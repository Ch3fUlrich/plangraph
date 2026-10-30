"""Inline the reviewed diff for review legs that give no verdict in tool mode.

luna and mimo-pro (D-3134/D-3173) only answer when the change is in the prompt.
The card names the change as ``diff_ref`` = ``{"repo", "base", "head"}``
(validated by ``planner.seats._check_diff_ref``); the spawner runs ``git diff``
itself so no caller pastes diffs by hand.
"""
from __future__ import annotations

import subprocess

from plangraph.planner.seats import _check_diff_ref
from plangraph.routing_errors import SpawnRefused

HEADER = "\n\n=== INLINE DIFF (base {base}, head {head}; this leg reads the change only from here) ===\n"
FOOTER = "\n=== END INLINE DIFF ===\n"


class InlineDiffError(Exception):
    """The diff could not be produced or does not fit the leg."""


def inline_diff_prompt(prompt: str, diff_ref, *, max_chars: int, timeout: float = 60) -> str:
    if diff_ref is None:
        raise InlineDiffError("card has no diff_ref")
    try:
        _check_diff_ref(diff_ref)
    except SpawnRefused as exc:
        raise InlineDiffError(str(exc)) from None
    base, head = diff_ref["base"], diff_ref["head"]
    try:
        proc = subprocess.run(
            ["git", "-C", diff_ref["repo"], "diff", "--no-ext-diff", "--no-color",
             "--no-textconv", f"{base}...{head}", "--"],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InlineDiffError(f"git diff failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise InlineDiffError("git diff failed: " + (proc.stderr.strip().splitlines() or ["exit %d" % proc.returncode])[0][:200])
    if not proc.stdout.strip():
        raise InlineDiffError("empty diff")
    if len(proc.stdout) > max_chars:
        raise InlineDiffError(f"diff too large for leg: {len(proc.stdout)} > {max_chars} chars")
    return prompt + HEADER.format(base=base, head=head) + proc.stdout + FOOTER
