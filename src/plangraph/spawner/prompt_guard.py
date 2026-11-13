"""Caller-brief admission and prompt-only review material checks.

Review seats skip control-byte admission: raw diffs may legitimately contain
those bytes. Prompt-only file reviews must supply their evidence inline.
"""
import unicodedata
import re


_REPO_PATH = re.compile(r'\b[\w./-]+\.(?:py|rs|md|txt|json|jsonl|toml|ya?ml|js|ts|tsx|jsx|sh|sql|cfg|ini)\b', re.I)
_FILE_REQUEST = re.compile(r'\b(?:read|open|check|inspect|review)\b[^\n.!?]{0,80}\b(?:plan|code|source|file)s?\b', re.I)
_INLINE = re.compile(
    r'^\s*(`{3,}|~{3,})[^\n]*\n(.*?)^\s*\1\s*$'
    r'|^=== (?:INLINE [^\n]+|CONTEXT PACK) ===\n(.*?)^=== END [^\n]+ ==='
    r'|^## (?:File: [^\n]+|PR [^\n]+)\n(.*?)(?=^## |\Z)', re.M | re.S)
_RAW_DIFF = re.compile(
    r'^diff --git [^\n]+\n(?:(?!diff --git |--- )[^\n]*\n)*'
    r'^--- (?:a/[^\n]+|/dev/null)\n^\+\+\+ (?:b/[^\n]+|/dev/null)\n'
    r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n^[ +\-][^\n]+', re.M)


def admit_toolless(prompt, card, role, leg, refuse):
    """Prompt-only file reviews need supplied material; never read files here."""
    if role not in ('review', 'judge') or getattr(leg, 'tool_mode', 'tools') != 'prompt-only':
        return
    if not isinstance(prompt, str):
        raise refuse('toolless_prompt_not_string')
    if card.get('diff_ref') is not None:
        return  # seat_spawn uses the existing inline_diff transport.
    if not (_REPO_PATH.search(prompt) or _FILE_REQUEST.search(prompt)):
        return
    if not (_RAW_DIFF.search(prompt) or any(
            (m[2] or m[3] or m[4] or '').strip() for m in _INLINE.finditer(prompt))):
        raise refuse('toolless_needs_inline')


def admit_prompt(prompt, refuse):
    """Return the input, or raise the entry's refusal without echoing its text."""
    if isinstance(prompt, str):
        line, previous = 1, ''
        for char in prompt:
            if unicodedata.category(char) == 'Cc' and char not in '\t\n\r':
                raise refuse(f'prompt contains control U+{ord(char):04X} on line {line}')
            if char == '\r' or (char == '\n' and previous != '\r'):
                line += 1
            previous = char
    return prompt
