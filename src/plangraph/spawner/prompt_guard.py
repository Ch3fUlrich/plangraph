"""Caller-brief admission and tool-less review material checks.

Review seats skip control-byte admission: raw diffs may legitimately contain
those bytes. Tool-less file reviews must supply their evidence inline.
"""
import unicodedata
import re


_REPO_PATH = re.compile(r'\b[\w./-]+\.(?:py|rs|md|txt|json|jsonl|toml|ya?ml|js|ts|tsx|jsx|sh|sql|cfg|ini)\b', re.I)
_FILE_REQUEST = re.compile(r'\b(?:read|open|check|inspect|review)\b[^\n.!?]{0,80}\b(?:plan|code|source|file)s?\b', re.I)
_INLINE = re.compile(
    r'(?:^(?P<label>Plan file [^\n]+:|--- [^\n]+ ---)\n)?'
    r'^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n(?P<body>.*?)^[ \t]*(?P=fence)[ \t]*$'
    r'|^=== INLINE (?P<inline>[^\n]+) ===\n(?P<inline_body>.*?)^=== END [^\n]+ ==='
    r'|^## (?P<header>File: [^\n]+|PR [^\n]+)\n(?P<header_body>.*?)(?=^## |\Z)', re.M | re.S)
_RAW_DIFF = re.compile(
    r'(?:^diff --git [^\n]+\n(?:(?!diff --git |--- )[^\n]*\n)*)?'
    r'^--- (?P<old>[^\s]+)[^\n]*\n^\+\+\+ (?P<new>[^\s]+)[^\n]*\n'
    r'(?:^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n'
    r'(?:^(?!\-\-\- |\+\+\+ )[ +\-][^\n]*(?:\n|\Z))+)+', re.M)


def _paths(text):
    return {m[0].removeprefix('./').rstrip('.') for m in _REPO_PATH.finditer(text)}


def admit_toolless(prompt, card, role, leg, refuse):
    """Check the assembled payload; labels/headers must cover the requested paths."""
    if role not in ('review', 'judge') or getattr(leg, 'tool_mode', 'tools') not in ('prompt-only', 'inline-only'):
        return
    if not isinstance(prompt, str):
        raise refuse('toolless_prompt_not_string')
    if card.get('diff_ref') is not None:
        return  # seat_spawn uses the existing inline_diff transport.
    brief = _RAW_DIFF.sub('', _INLINE.sub('', prompt))
    requested = _paths(brief)
    supplied = set()
    for match in _RAW_DIFF.finditer(prompt):
        for name in ('old', 'new'):
            supplied.update(_paths(re.sub(r'^[ab]/', '', match[name])))
    for match in _INLINE.finditer(prompt):
        labels = _paths(' '.join(match[name] or '' for name in ('label', 'info', 'inline', 'header')))
        requested.update(labels)
        if any((match[name] or '').strip() for name in ('body', 'inline_body', 'header_body')):
            supplied.update(labels)
    if not (requested or _FILE_REQUEST.search(brief)):
        return
    if not supplied or not requested.issubset(supplied):
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
