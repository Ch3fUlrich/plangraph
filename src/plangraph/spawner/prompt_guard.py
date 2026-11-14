"""Caller-brief admission and tool-less review material checks.

Review diffs may contain control bytes; tool-less file reviews need inline evidence.
"""
import re
import unicodedata


_REPO_PATH = re.compile(r'\b[\w./-]+\.(?:py|rs|md|txt|json|jsonl|toml|ya?ml|js|ts|tsx|jsx|sh|sql|cfg|ini)\b', re.I)
_FILE_REQUEST = re.compile(r'\b(?:read|open|check|inspect|review)\b[^\n.!?]{0,80}\b(?:plan|code|source|file)s?\b', re.I)
_READ = re.compile(r'\b(?:read|open|check|inspect|review)\b(?![ \t]*\()', re.I)
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
    """Remove only validated evidence before finding unsatisfied file requests."""
    if role not in ('review', 'judge') or getattr(leg, 'tool_mode', 'tools') not in ('prompt-only', 'inline-only'):
        return
    if not isinstance(prompt, str):
        raise refuse('toolless_prompt_not_string')
    if card.get('diff_ref') is not None:
        return  # The existing diff inliner validates this transport before launch.
    supplied = set()

    def diff(match):
        for name in ('old', 'new'):
            supplied.update(_paths(re.sub(r'^[ab]/', '', match[name])))
        return ''

    def inline(match):
        labels = _paths(' '.join(match[name] or '' for name in ('label', 'info', 'inline', 'header')))
        body = ''.join(match[name] or '' for name in ('body', 'inline_body', 'header_body'))
        if labels and body.strip() and not (_FILE_REQUEST.search(body) or (_READ.search(body) and _paths(body))):
            supplied.update(labels)
            return ''
        return match[0]  # A label/fence around a read request is not evidence.

    brief = re.sub(r'^## Cut files\n(?:- [^\n]+ \((?:byte budget|unavailable or deleted)\)\n|None\n)*',
                   '', prompt, flags=re.M) if role == 'judge' else prompt
    brief = _INLINE.sub(inline, _RAW_DIFF.sub(diff, brief))
    requested = _paths(brief)
    if (requested or _FILE_REQUEST.search(brief)) and (not supplied or not requested.issubset(supplied)):
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


# These children expose skill tools, but no repository/shell tools (D-4681).
INLINE_DIFF_HARNESSES = frozenset({'opencode'})

def _needs_inline_diff(leg, card):
    return (getattr(leg, 'tool_mode', 'tools') == 'inline-only'
            or (getattr(leg, 'tool_mode', 'tools') == 'prompt-only' and card.get('diff_ref') is not None)
            or (getattr(leg, 'harness', None) in INLINE_DIFF_HARNESSES
                and card.get('diff_ref') is not None))


def _refuse_oversized_diff(exc, leg):
    from .context_pack import PromptBoundError
    if exc.chars is not None:
        raise PromptBoundError(
            f'prompt_too_large: inline_diff_oversized: chars {exc.chars} estimated_tokens {(exc.chars + 2) // 3} '
            f'max_request_tokens {leg.max_request_tokens}') from None
