"""Context pack for inline-only legs and the big-file brief warning (D-3622, D-3649).

An inline-only leg (``tool_mode`` "inline-only", e.g. mimo-v2.6-pro) cannot read files, so
``card["context"]`` lists the slices it needs: ``{"path", "start", "end"}`` (1-based, inclusive) or
``"path:start-end"``, at most ``MAX_ENTRIES`` of them.  ``pack_prompt`` appends them under a CONTEXT PACK
section, each fenced and labelled ``path:start-end``, capped by ``PLANGRAPH_CONTEXT_PACK_MAX_CHARS``
(default 60000); slices over the cap are dropped and named in one ``omitted:`` line.  Tool legs read files
themselves and get no pack.  ``warnings`` names every repo file the brief mentions that has over 400 lines
and no slice.  Paths are relative to the run cwd; a path that leaves it (lexically or by symlink) refuses
the spawn.

Safety, in order of strength:

* Fail closed (``ContextError``, the spawn is refused): the card has ``context`` entries and git cannot list
  the tracked files of the run cwd (no work tree, git missing or failing), a symlink loop, more than
  ``MAX_ENTRIES`` entries.  A build without entries packs nothing, so a failed listing there only skips the
  advisory big-file scan and says so (one ``big_file_scan_skipped`` warning, never a clean empty result).
* Refused per entry (a ``context_path_refused`` warning, the file is never read): a secret-looking name
  (``DENY``, case-insensitive on every path part, a superset of the repo's own fences in opencode.jsonc),
  a ``.git`` path part, an untracked file.
* Every slice goes through ``redact_source`` before packing, and a slice that still holds a private-key
  block is dropped (``context_path_refused`` ... ``secret-looking content``).

Cost: ``build`` runs once per spawn.  It lists the tracked files with one ``git ls-files``, reads each
sliced file once (streaming, stopping at its last wanted line, holding at most the char cap per slice) and
counts lines of an unsliced named file once; every leg then reuses the result.

Lines: one convention for slice numbers, file length and the big-file count -- a line ends at ``\\n``,
``\\r\\n`` or ``\\r`` (universal newlines), never at the other separators ``str.splitlines`` knows.  A range
past the end of the file adds a ``context_range_past_eof`` warning (a partly past range is clamped, a range
wholly past the end is not packed).
"""
from __future__ import annotations

import errno
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from plangraph.routing_errors import ParameterRefused
from .env_redact import redact_source, _source_block, _join_prompt
from .inline_diff import InlineDiffError

BIG_FILE_LINES = 400
DEFAULT_MAX_CHARS = 60_000
#: Most ``card.context`` entries one spawn may list (more refuses the spawn).  With the char cap this bounds
#: the work a card can cause: one tracked-file listing, at most this many file reads, this many slices.
MAX_ENTRIES = 64
_SLICE = re.compile(r'^(.+):(\d+)-(\d+)$')
_RANGE = re.compile(r':\d+(?:-\d+)?$')  # 'big.py:1-50' names big.py
_TOKEN = re.compile(r'''[^\s`'"<>()\[\]{},;=]+''')
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY')
#: Secret-looking path parts, fnmatch on every lower-cased part of the path.  The first group mirrors the repo's
#: own read fences in opencode.jsonc (tests/test_spawner_context_guard.py derives its samples from that file).
DENY = (
    '*.env', '*.env.*', '.env*', '*auth.json', '*api_keys*', '*api-keys*', '*client.key*', '*manage.key*',
    '*.claude.json*',
    '*api_key*', '*api-key*', '*apikey*', '*secret*', '*credential*', '*password*', '*passwd*', '*private_key*',
    '*private-key*', '*privatekey*', '*service_account*', '*service-account*', '*vault_pass*', '*vault-pass*',
    '*.key', '*.pem', '*.p12', '*.pfx', '*.p8', '*.jks', '*.keystore', '*.kdbx', '*.ppk', '*.gpg', '*.pgp',
    '*.keytab', 'id_*', '*.token', '*.dsn', '*.tfvars', '*.tfstate', '*.tfstate.*', 'kubeconfig*', '*.kubeconfig',
    '.netrc', '_netrc', '.npmrc', '.pypirc', '.pgpass', '.my.cnf', '.htpasswd', '.dockercfg', '.s3cfg',
    '.git-credentials', '.ssh', '.aws', '.gnupg', '.kube', '.docker', '.azure',
)
#: Data-file suffixes (not source code): there a secret word in the NAME marks secret material (``token.txt``,
#: ``pg_dsn.yml``), while ``token_fold.py`` stays packable.
DATA_SUFFIXES = frozenset(('', '.txt', '.json', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.conf', '.properties',
                           '.xml', '.csv', '.dat', '.log', '.bak', '.old', '.orig'))
_SECRET_WORD = re.compile(r'token|dsn|cookie|(?<![a-z0-9])auth(?![a-z0-9])')
#: GIT_DIR and kin would point the listing at the caller's repo instead of the run cwd's (D-3465).
_GIT_REDIRECT = frozenset(('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY'))


class ContextError(ParameterRefused):
    """A ``context`` entry is malformed, unreadable or outside the run cwd, or the tracked files cannot be listed."""


@dataclass(frozen=True)
class Pack:
    """What one spawn takes from ``card.context`` and the brief, built once by :func:`build`: the CONTEXT PACK
    section (``''`` when nothing is packed) and every warning in order."""
    text: str = ''
    warnings: tuple[str, ...] = ()


def _check_loop(path: Path) -> None:
    # Non-strict resolve() no longer raises on loops on Python 3.13+.
    try:
        os.stat(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise


def _root(cwd) -> Path:
    try:
        _check_loop(Path(cwd))
        return Path(cwd).resolve()
    except (RuntimeError, OSError):
        raise ContextError('the run cwd cannot be resolved (symlink loop or unreadable)') from None


def _locate(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
        raise ContextError('context path must be a non-empty string')
    path = Path(os.path.normpath(root / raw))
    if not path.is_relative_to(root):
        raise ContextError(f'context path {raw!r} is outside the run cwd')
    try:
        _check_loop(path)
        resolved = path.resolve()
        inside, is_file = resolved.is_relative_to(root), resolved.is_file()
    except (RuntimeError, OSError):
        raise ContextError(f'context path {raw!r} cannot be resolved (symlink loop or unreadable)') from None
    if not inside:
        raise ContextError(f'context path {raw!r} is a symlink out of the run cwd')
    if not is_file:
        raise ContextError(f'context path {raw!r} is not a file in the run cwd')
    return resolved


def _lines(path: Path, limit: int = 65536):
    """The lines of ``path`` without terminators: the one line convention (see the module docstring).  Undecodable
    bytes become U+FFFD and the last unterminated line counts.  A line over ``limit`` chars comes back cut to
    ``limit + 1`` chars and the rest of it is read and dropped, so memory stays bounded."""
    with path.open('r', encoding='utf-8', errors='replace', newline=None) as fh:
        while True:
            line = fh.readline(limit + 1)
            if not line:
                return
            if line.endswith('\n'):
                yield line[:-1]
                continue
            if len(line) <= limit:
                yield line   # the last line, no terminator
                return
            rest = fh.readline(65536)
            while rest and not rest.endswith('\n'):
                rest = fh.readline(65536)
            yield line


def _line_count(path: Path) -> int:
    """Number of lines (the one convention), streamed: never the whole file in memory."""
    return sum(1 for _ in _lines(path))


def entries(card) -> list[tuple[str, int, int]]:
    """``card["context"]`` as ``(path, start, end)`` tuples; malformed or too many entries refuse."""
    raw = card.get('context') if isinstance(card, dict) else None
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ContextError('context must be a list')
    if len(raw) > MAX_ENTRIES:
        raise ContextError(f'context lists {len(raw)} slices; the cap is {MAX_ENTRIES}')
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            m = _SLICE.match(item)
            parts = (m[1], int(m[2]), int(m[3])) if m else None
        elif isinstance(item, dict):
            start, end = item.get('start'), item.get('end')
            parts = (item.get('path'), start, end) if type(start) is int and type(end) is int else None
        else:
            parts = None
        if not parts or not isinstance(parts[0], str) or not 1 <= parts[1] <= parts[2]:
            raise ContextError(f'context[{i}] must be "path:start-end" or {{path, start, end}} with 1 <= start <= end')
        out.append(parts)
    return out


def tracked(root: Path) -> frozenset[str]:
    """Git-tracked paths under ``root``, relative to it.  Fails closed: when git cannot list them (``root`` is no
    work tree, git is missing, fails or times out) this raises ``ContextError`` instead of judging paths against
    a listing nobody read."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT}
    try:
        out = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True, check=True,
                             timeout=30, env=env).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContextError('context needs the tracked files of a git work tree at the run cwd, but git ls-files '
                           f'failed ({type(exc).__name__})') from None
    return frozenset(n for n in out.decode('utf-8', 'replace').split('\0') if n)


def _secret_part(part: str) -> bool:
    if any(fnmatch.fnmatchcase(part, pat) for pat in DENY):
        return True
    stem, suffix = os.path.splitext(part)
    return suffix in DATA_SUFFIXES and _SECRET_WORD.search(stem) is not None


def _refusal(rel: str, files: frozenset[str]) -> str | None:
    # A Windows stream suffix (':x') or trailing dots/spaces name the same file as the bare part.
    parts = [p.split(':', 1)[0].rstrip('. ') for p in rel.lower().split('/')]
    if '.git' in parts or any(_secret_part(p) for p in parts):
        return 'secret-looking path'
    return None if rel in files else 'not tracked by git'


def _extract(path: Path, wanted, cap: int):
    """One streaming pass over ``path`` for the ``(start, end)`` ranges in ``wanted``: each range's text (None once
    it passes ``cap`` chars: it can only be dropped) and the file's line count (None when the pass stopped at the
    last wanted line, i.e. the file has at least that many)."""
    last = max(end for _, end in wanted)
    chunks, sizes, n = [[] for _ in wanted], [0] * len(wanted), 0
    for line in _lines(path, max(cap, 0)):
        n += 1
        for i, (start, end) in enumerate(wanted):
            if start <= n <= end and chunks[i] is not None:
                sizes[i] += len(line) + 1
                if sizes[i] > cap:
                    chunks[i] = None
                else:
                    chunks[i].append(line)
        if n >= last:
            return [None if c is None else '\n'.join(c) for c in chunks], None
    return [None if c is None else '\n'.join(c) for c in chunks], n


def _slices(root: Path, ents, files: frozenset[str], cap: int):
    """``(kept, notes)``: ``(path, raw, start, end, text)`` per readable entry in card order (text None = over the
    cap, end clamped to the file length) and the per-entry warnings in card order.  Each file is read once."""
    plan, notes = {}, [[] for _ in ents]
    for i, (raw, start, end) in enumerate(ents):
        path = _locate(root, raw)
        rel = path.relative_to(root).as_posix()
        why = _refusal(Path(os.path.normpath(raw)).as_posix(), files) or _refusal(rel, files)
        if why:
            notes[i].append(f'context_path_refused: {raw} ({why})')
        else:
            plan.setdefault(path, []).append((i, raw, start, end))
    found = {}
    for path, wanted in plan.items():
        try:
            texts, total = _extract(path, [(start, end) for _, _, start, end in wanted], cap)
        except OSError:
            for i, raw, _, _ in wanted:
                notes[i].append(f'context_path_refused: {raw} (unreadable)')
            continue
        for (i, raw, start, end), text in zip(wanted, texts):
            if total is not None and start > total:
                notes[i].append(f'context_range_past_eof: {raw}:{start}-{end} (file has {total} lines; not packed)')
                continue
            if total is not None and end > total:
                notes[i].append(f'context_range_past_eof: {raw}:{start}-{end} (file has {total} lines; clamped)')
                end = total
            if text is not None:
                text = redact_source(text)
                if _PRIVATE_KEY.search(text):
                    notes[i].append(f'context_path_refused: {raw} (secret-looking content)')
                    continue
            found[i] = (path, raw, start, end, text)
    return [found[i] for i in sorted(found)], [w for per in notes for w in per]


def max_chars() -> int:
    try:
        return int(os.environ.get('PLANGRAPH_CONTEXT_PACK_MAX_CHARS', DEFAULT_MAX_CHARS))
    except ValueError:
        return DEFAULT_MAX_CHARS


def _assemble(blocks, omitted) -> str:
    text = '\n\n=== CONTEXT PACK ===\n' + _join_prompt((b for _, b in blocks), '\n')
    if omitted:
        text += '\nomitted: ' + ', '.join(omitted)
    return text + '\n=== END CONTEXT PACK ===\n'


def _fit(kept, cap: int):
    """``(pack text or '', warnings)``: the text never exceeds ``cap``; if even the framing plus the ``omitted:``
    line cannot fit, no pack and one ``context_pack_unfit`` warning (cap, needed chars)."""
    if not kept:
        return '', []
    shown, omitted = [], []
    for _, raw, start, end, text in kept:
        label = f'{raw}:{start}-{end}'
        item = (label, f'--- {label} ---\n```\n' + (_source_block(text) if text is not None else '') + '\n```')
        if text is not None and len(_assemble(shown + [item], omitted)) <= cap:
            shown.append(item)
        else:
            omitted.append(label)
    while shown and len(_assemble(shown, omitted)) > cap:   # the omitted line may itself force another drop
        omitted.append(shown.pop()[0])
    if shown:
        return _assemble(shown, omitted), []
    need = len(_assemble([], omitted))
    return '', [f'context_pack_unfit: cap {cap} chars, needs {need}; raise PLANGRAPH_CONTEXT_PACK_MAX_CHARS or name smaller slices']


def _brief_names(prompt: str) -> dict:
    """Path-like tokens of the brief, in order, without a trailing ``:N-M`` range or sentence punctuation."""
    return dict.fromkeys(t for t in (_RANGE.sub('', x.removeprefix('./').rstrip('.:')).rstrip('.:')
                                     for x in _TOKEN.findall(prompt)) if '/' in t or '.' in t)


def _big_files(root: Path, names, files: frozenset[str], covered) -> list[str]:
    big = []
    for name in names:
        if name not in files or _refusal(name, files):
            continue
        try:
            path = _locate(root, name)
            if path in covered:   # sliced already: no second read
                continue
            count = _line_count(path)
        except (ContextError, OSError):
            continue
        if count > BIG_FILE_LINES:
            big.append(f'big_file_unsliced: {name} {count} lines; name one function per slice in card.context')
    return big


def build(prompt: str, card, cwd) -> Pack:
    """The pack and every warning of one spawn.  Validates ``context`` (a malformed entry, too many entries, a path
    outside the cwd, a symlink loop, or no tracked-file listing refuses via ``ContextError``).  Pass the result to
    every leg: nothing here is repeated per leg."""
    root, ents, names = _root(cwd), entries(card), _brief_names(prompt)
    skipped = []
    try:
        files = tracked(root) if ents or names else frozenset()
    except ContextError as exc:
        if ents:
            raise
        # Nothing is packed without entries; only the advisory scan lost its listing.  Report that instead of
        # passing a scan nobody ran as clean (D-3649 r2).
        files, skipped = frozenset(), [f'big_file_scan_skipped: {exc}']
    cap = max_chars()
    kept, notes = _slices(root, ents, files, cap)
    text, unfit = _fit(kept, cap)
    big = _big_files(root, names, files, {k[0] for k in kept})
    # Opt-in runtime policy, never a caller-card override. Only counts/reason codes leave this refusal.
    if big and os.environ.get('PLANGRAPH_CONTEXT_STRICT') == '1':
        raise ContextError(f'big_file_unsliced: count {len(big)}')
    return Pack(text, tuple(notes + unfit + skipped + big))


class PromptBoundError(ContextError, InlineDiffError):
    """A context bound refusal also handled by the launcher's inline-payload boundary."""


def bound_prompt(prompt: str, max_request_tokens: int) -> str:
    """Refuse the entire inline payload; conservative three characters per estimated token.

    Requirements are never truncated. Diagnostics contain sizes and a reason code, not prompt content.
    """
    if len(prompt) > max_request_tokens * 3:
        raise PromptBoundError(f'inline_prompt_oversized: chars {len(prompt)} '
                           f'estimated_tokens {(len(prompt) + 2) // 3} max_request_tokens {max_request_tokens}')
    return prompt


def pack_prompt(prompt: str, card, cwd, pack: Pack | None = None) -> str:
    """The prompt plus a CONTEXT PACK section no longer than the cap (framing and ``omitted:`` included).  ``pack``
    is the spawn's :func:`build` result; strict admission also scans the brief when built here."""
    brief = prompt if os.environ.get('PLANGRAPH_CONTEXT_STRICT') == '1' else ''
    return prompt + (pack if pack is not None else build(brief, card, cwd)).text


def warnings(prompt: str, card, cwd) -> list[str]:
    """Every warning of :func:`build`: ``context_path_refused``, ``context_range_past_eof``, ``context_pack_unfit``,
    ``big_file_scan_skipped`` (no entries and no tracked-file listing), and one ``big_file_unsliced`` per tracked
    file the brief names."""
    return list(build(prompt, card, cwd).warnings)


def annotate(record: dict, prompt: str, card, cwd) -> Pack:
    """Validate ``context`` (refuses a bad entry), put the warnings on ``record`` and its events, and return the
    :class:`Pack` the legs reuse."""
    pack = build(prompt, card, cwd)
    if pack.warnings:
        record['warnings'] = list(pack.warnings)
        record.setdefault('events', []).extend({'event': w.split(':', 1)[0], 'detail': w} for w in pack.warnings)
    return pack
