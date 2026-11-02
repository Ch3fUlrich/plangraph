"""Code slices inlined into a writer brief (D-3605).

mimo legs read file by file, one tool round per model turn, so a brief that sends them through a big
file delays their first edit.  ``card["context"]`` lists the code the writer needs as entries
``{"path", "start_line", "end_line"}`` or ``{"path", "symbol"}`` (``symbol``: a Python function, class
or method such as ``Class.method``, resolved with ``ast``).  ``inline_context`` appends the slices to the
writer prompt, each under a ``path:start-end`` header and scrubbed by ``env_redact``; ``notes`` gives the
run record its ``context_slices`` and, for a slow-reading leg, one warning when the brief names a big
file that no entry covers.  Pure: reads files under the run cwd only, and a path or symlink that
leaves it, a malformed entry or slices over the cap refuse the dispatch.
"""
from __future__ import annotations

import ast
import os
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from plangraph.routing_errors import ParameterRefused

from .env_redact import redact_env_values

MAX_LINES = 1200
MAX_CHARS = 60_000
MAX_ENTRIES = 50
MAX_FILE_BYTES = 8 * 1024 * 1024
BIG_FILE_LINES = 600
#: Legs whose agent opens one file per model turn (operator order 2026-11-02); matched on leg id or model name.
SLOW_READER_LEGS = {'mimo-v2.6-pro': 'one file per tool round', 'mimo-v2.6-flash': 'one file per tool round'}
HEADER = ('\n\n=== CONTEXT (slices of files in your working directory, each under its path:first-last lines; '
          'work from them and open a file only for code not shown) ===\n')
FOOTER = '\n=== END CONTEXT ===\n'

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: A file name in free text: optional directories, a dotted name with a letter-led extension.
_NAME = re.compile(r'(?<![\w./-])(/?(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/-])')


class ContextError(ParameterRefused):
    """A ``context`` entry is malformed, unreadable or outside the run cwd, or the slices pass the cap."""


@dataclass(frozen=True)
class Slice:
    shown: str      # path relative to the run cwd
    resolved: Path
    start: int
    end: int
    text: str

    @property
    def header(self) -> str:
        return f'{self.shown}:{self.start}-{self.end}'


def is_slow_reader(leg) -> bool:
    return (getattr(leg, 'id', None) in SLOW_READER_LEGS
            or str(getattr(leg, 'model', '')).rsplit('/', 1)[-1] in SLOW_READER_LEGS)


def _check(entry):
    path = entry.get('path') if isinstance(entry, Mapping) else None
    if not isinstance(path, str) or not path.strip() or '\0' in path:
        raise ContextError('must be an object with a path')
    keys = set(entry) - {'path'}
    if keys == {'symbol'}:
        if not isinstance(entry['symbol'], str) or not entry['symbol'].strip():
            raise ContextError('symbol must be a name such as "func" or "Class.method"')
    elif keys == {'start_line', 'end_line'}:
        first, last = entry['start_line'], entry['end_line']
        if type(first) is not int or type(last) is not int or not 1 <= first <= last:
            raise ContextError('start_line and end_line must be integers with 1 <= start_line <= end_line')
    else:
        raise ContextError('needs {path, symbol} or {path, start_line, end_line}')


def _locate(root: Path, raw: str):
    """(path relative to ``root``, resolved file): lexically and after symlinks inside ``root``."""
    lexical = Path(os.path.normpath(root / raw))
    try:
        shown = lexical.relative_to(root).as_posix()
    except ValueError:
        raise ContextError(f'{raw!r} is outside the run cwd') from None
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ContextError(f'{shown} does not exist') from None
    if not resolved.is_relative_to(root):
        raise ContextError(f'{shown} is a symlink out of the run cwd')
    if not resolved.is_file():
        raise ContextError(f'{shown} is not a regular file')
    return shown, resolved


def _lines(shown: str, resolved: Path) -> list[str]:
    """The file's lines, split on \\n, \\r\\n and \\r like the Python tokenizer."""
    try:
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise ContextError(f'{shown} is over {MAX_FILE_BYTES} bytes; name a smaller file')
        data = resolved.read_bytes()
    except OSError as exc:
        raise ContextError(f'{shown} is unreadable: {exc.strerror or type(exc).__name__}') from None
    if b'\0' in data[:8192]:
        raise ContextError(f'{shown} is a binary file')
    text = data.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    return text.removesuffix('\n').split('\n') if text else []


def _defs(body, name):
    """Definitions called ``name`` in ``body``, also under if/try/with/for/while/match blocks."""
    for node in body:
        if isinstance(node, _DEFS):
            if node.name == name:
                yield node
        else:
            for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
                yield from _defs(getattr(node, field, ()), name)


def _symbol_spans(shown: str, lines: list[str], symbol: str):
    """(first, last) line of every definition ``symbol`` (dotted) names, decorators included."""
    if not shown.endswith('.py'):
        raise ContextError(f'{shown}: symbol needs a .py file; give start_line and end_line')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse('\n'.join(lines))
    except (SyntaxError, ValueError) as exc:
        raise ContextError(f'{shown} does not parse ({type(exc).__name__}); give start_line and end_line') from None
    owners = [tree]
    for part in symbol.split('.'):
        owners = [d for owner in owners for d in _defs(owner.body, part)]
    if not owners:
        top = [n.name for n in tree.body if isinstance(n, _DEFS)][:10]
        raise ContextError(f'{shown} has no function, class or method {symbol!r}; top level: {", ".join(top) or "none"}')
    return sorted((min([n.lineno, *(d.lineno for d in n.decorator_list)]), n.end_lineno) for n in owners)


def resolve(card, cwd, env=None) -> list[Slice]:
    """The redacted slices of ``card["context"]`` in entry order; ``[]`` without entries."""
    entries = card.get('context') if isinstance(card, Mapping) else None
    if not entries:
        return []
    if not isinstance(entries, (list, tuple)) or len(entries) > MAX_ENTRIES:
        raise ContextError(f'context must be a list of at most {MAX_ENTRIES} entries')
    if env is None:
        from plangraph.routing_support.host_keys import redaction_env
        env = redaction_env(os.environ)
    root, slices, lines, chars = Path(cwd).resolve(), [], 0, 0
    for index, entry in enumerate(entries):
        try:
            _check(entry)
            shown, resolved = _locate(root, entry['path'])
            source = _lines(shown, resolved)
            if 'symbol' in entry:
                spans = _symbol_spans(shown, source, entry['symbol'])
            elif entry['start_line'] > len(source):
                raise ContextError(f'{shown} has {len(source)} lines; start_line {entry["start_line"]} is past the end')
            else:
                spans = [(entry['start_line'], min(entry['end_line'], len(source)))]
        except ContextError as exc:
            raise ContextError(f'context[{index}]: {exc}') from None
        for start, end in spans:
            text = redact_env_values('\n'.join(source[start - 1:end]), env)
            lines, chars = lines + end - start + 1, chars + len(text)
            if lines > MAX_LINES or chars > MAX_CHARS:
                raise ContextError(f'context is over the cap at entry {index}: {lines} lines, {chars} chars '
                                   f'(cap {MAX_LINES} lines or {MAX_CHARS} chars); trim the entries or split the step')
            slices.append(Slice(shown, resolved, start, end, text))
    return slices


def inline_context(card, prompt: str, cwd, role: str = 'implement', env=None):
    """``(prompt with the slices appended, slices)``; only a writer prompt gets them."""
    slices = resolve(card, cwd, env) if role == 'implement' else []
    if not slices:
        return prompt, slices
    body = '\n\n'.join(f'{s.header}\n{s.text}' for s in slices)
    return prompt + HEADER + body + FOOTER, slices


def big_files(brief: str, slices, legs, cwd):
    """``(name, line count)`` of files the brief names that exist in ``cwd``, pass BIG_FILE_LINES and no slice covers.

    Only for a plan with a slow-reading leg; a file the brief names with a path outside ``cwd`` is skipped.
    """
    if not any(is_slow_reader(leg) for leg in legs):
        return []
    root, covered, found = Path(cwd).resolve(), {s.resolved for s in slices}, []
    for name in list(dict.fromkeys(_NAME.findall(brief[:200_000])))[:200]:
        try:
            shown, path = _locate(root, name)
            count = len(_lines(shown, path))
        except ContextError:
            continue
        if count > BIG_FILE_LINES and path not in covered:
            found.append((name, count))
    return found


def notes(slices, brief: str, legs, cwd) -> dict:
    """Run record fields: the inlined headers, and one warning for big files named without a slice."""
    out = {}
    if slices:
        out['context_slices'] = [s.header for s in slices]
    big = big_files(brief, slices, legs, cwd)
    if big:
        out['warnings'] = ['brief names big files without context slices: '
                           + ', '.join(f'{name} {count} lines' for name, count in big)
                           + '; add context entries or split the slice']
    return out
