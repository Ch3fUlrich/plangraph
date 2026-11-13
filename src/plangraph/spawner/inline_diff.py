"""Inline the reviewed diff for review legs that give no verdict in tool mode.

luna and mimo-pro (D-3134/D-3173) only answer when the change is in the prompt.
The card names the change as ``diff_ref`` = ``{"repo", "base", "head"}``
(validated by ``planner.seats._check_diff_ref``); the spawner runs ``git diff``
itself so no caller pastes diffs by hand.
"""
from __future__ import annotations

import subprocess
import re
from pathlib import Path

from plangraph.planner.seats import _check_diff_ref
from plangraph.routing_errors import SpawnRefused
from plangraph.routing_support import background_process_options
from .env_redact import _source_block

HEADER = "\n\n=== INLINE DIFF (base {base}, head {head}; this leg reads the change only from here) ===\n"
FOOTER = "\n=== END INLINE DIFF ===\n"


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
    try:
        proc = subprocess.run(
            ["git", "-C", diff_ref["repo"], "diff", "--no-ext-diff", "--no-color",
             "--no-textconv", f"{base}...{head}", "--"],
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
    return prompt + HEADER.format(base=base, head=head) + _source_block(proc.stdout) + FOOTER


def has_diff_text(prompt):
    """Require a patch header plus patch data, never just an inline-diff label."""
    return isinstance(prompt, str) and bool(re.search(
        r'^diff --git .+\n(?:(?!diff --git ).*\n)*?(?:@@ |[+-]{3} |[+-](?![+-])|GIT binary patch|Binary files |(?:old|new|deleted file) mode )',
        prompt + '\n', re.M))


def prompt_only_void(record, directory):
    """Check the persisted, dispatched prompt before accepting review evidence."""
    leg = record.get('chosen_leg') or (record.get('plan') or {}).get('first_leg') or {}
    if record.get('role') != 'review' or leg.get('tool_mode', record.get('tool_mode')) != 'prompt-only':
        return False
    if record.get('status') in ('running', 'queued') and record.get('ended') is None:
        return False  # The dispatcher may still be constructing the prompt.
    try:
        present = has_diff_text((Path(directory) / 'prompt.txt').read_text('utf-8'))
    except (OSError, UnicodeError):
        present = False
    if not present:
        record.update(verdict_status='void', verdict_void_reason='prompt_only_no_diff')
        return True
    record.pop('verdict_status', None)
    record.pop('verdict_void_reason', None)
    return False
