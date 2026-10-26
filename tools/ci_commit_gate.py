'''CI commit gate: Writer attribution, hand-edit, gate-edit, test-removal, open_pr_merged reasons.

Trailer checks use ``git log --no-merges base..head``. Merge parents are checked
separately when open PR heads are supplied. Exempt commits skip only trailer rules;
an exempt entry does not waive open_pr_merged. A repo-relative
exempt path is read from the BASE commit, never from the working tree, so a PR
cannot exempt the commits it introduces.

--head must be the PR head SHA, not GitHub's speculative pull_request merge ref:
that ref's second parent is the PR's own head. The --open-heads file must be
regenerated per run from OPEN PRs only (drop closed-unmerged PRs). The merge
check is inert until a caller writes the file and supplies --open-heads.
The PR under test's own head may be listed. The caller must fetch open PR heads first
so every listed SHA resolves to a commit in the local object database.
'''

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
import posixpath

#: Strict Writer trailer line: key case-sensitive, no space before the colon,
#: one space after it, and a non-empty value.
WRITER_RE = re.compile(r'^Writer: (\S.*)$')

#: Case-insensitive writer-like line that is not the strict form above.
MALFORMED_WRITER_RE = re.compile(r'^\s*writer\s*:', re.IGNORECASE)

#: Strict Hand-Edit trailer line.
HAND_EDIT_RE = re.compile(r'^Hand-Edit: (\S.*)$')

#: Case-insensitive Hand-Edit-like line that is not the strict form above.
MALFORMED_HAND_EDIT_RE = re.compile(r'^\s*hand-edit\s*:', re.IGNORECASE)

#: Plan-Item trailer line.
PLAN_ITEM_RE = re.compile(r'^Plan-Item: (\S.*)$')

#: Route-Decision trailer line above ROUTE_DECISION_LINES.
ROUTE_DECISION_LINES = 50
ROUTE_DECISION_RE = re.compile(r'^Route-Decision: route-[0-9a-f]{32}$')

#: A test-removal trailer, accepted case-insensitively on any line.
TEST_REMOVAL_RE = re.compile(r'^Test-Removal:\s*D-\d+\s*$', re.IGNORECASE)

#: A gate-edit commit must carry exactly ``Gate-Edit: D-<digits>``.
GATE_EDIT_RE = re.compile(r'^Gate-Edit: D-\d+$')

#: A non-blank line that can appear in the trailer block.
TRAILER_LINE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9-]*: \S')

#: Case-insensitive markers in the Writer value that identify a Claude writer.
CLAUDE_MARKERS: tuple[str, ...] = (
    'claude',
    'anthropic',
    'sonnet',
    'opus',
    'haiku',
    'fable',
)

#: Test file paths: under a tests/test directory, ``test_*`` files,
#: ``*_test.*`` files, or ``*.test.*``/``*.spec.*`` files, any extension.
TEST_PATH_RE = re.compile(
    r'(?:^|/)(?:tests?)/'
    r'|(?:^|/)test_[^/]*$'
    r'|(?:^|/)[^/]*_test\.[^/]+$'
    r'|(?:^|/)[^/]*\.(?:test|spec)\.[^/]+$'
)

PY_TEST_RE = re.compile(r'^\s*(?:async\s+)?def\s+(test_\w+)', re.MULTILINE)

JS_TEST_RE = re.compile(r'\b(?:it|test)(?:\.\w+)*\s*\(\s*([\x27\x22`])(.+?)\1')

BATS_TEST_RE = re.compile(r'^\s*@test\s+([\x27\x22])(.+?)\1', re.MULTILINE)
SH_TEST_FUNC_RE = re.compile(r'^\s*(?:function\s+)?(test_\w+)\s*\(\)', re.MULTILINE)

JS_TEST_EXTS = frozenset({'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'})
SHELL_TEST_EXTS = frozenset({'.sh', '.bats'})

#: Hand-Edit commits may not touch these auth/door surfaces.
AUTH_DOOR_PATHS: tuple[str, ...] = (
    'src/plangraph/keeper/*',
    'src/plangraph/*/auth*',
    'src/plangraph/*auth*',
    '*/roles*',
    '*/scopes*',
    'roles*',
    'scopes*',
    'tools/seat_verdict.py*',
    'ops/pg.sh*',
    'tools/ci_commit_gate.py*',
    'tools/ci_commit_exempt.txt*',
    'tools/gate_wiring_check.py*',
    '.github/*',
)

#: Touching any of these paths requires a ``Gate-Edit: D-<digits>`` trailer.
GATE_PATHS: tuple[str, ...] = (
    'tools/ci_commit_gate.py*',
    'tools/ci_commit_exempt.txt*',
    '.github/workflows/*',
    'tools/gate_wiring_check.py*',
)

EXEMPT_SHA_RE = re.compile(r'^[0-9a-fA-F]{40}$')
OPEN_HEAD_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
OPEN_PR_SKIP_NOTICE = 'ci-commit-gate: open PR merge check skipped (no --open-heads)\n'

MISSING_WRITER = 'missing Writer trailer'
CLAUDE_WRITER = 'claude writer'
TEST_REMOVAL_VIOLATION = 'test removal without Test-Removal trailer'
MALFORMED_WRITER = 'malformed Writer trailer'
MALFORMED_HAND_EDIT = 'malformed Hand-Edit trailer'
GATE_EDIT_VIOLATION = 'gate edit without Gate-Edit trailer'
#: A Hand-Edit commit may change at most this many lines (added + deleted).
#: Per-commit ceiling (ORCH-EFFICIENCY-ENFORCE E1a); above ROUTE_DECISION_LINES a
#: Route-Decision trailer is required, so 50 stays the interim cap until E3c.
HAND_EDIT_MAX_LINES = 400

#: Matcher for secret-shaped file names (lower-cased basename), copied from gates/privacy.py.
SECRET_NAME_PATTERNS: tuple[str, ...] = (
    "*.key", "*.pem", "*.p12", "*.pfx", "*.kdbx", "*.p8", "*.jks",
    ".env", ".env.*", "credentials*.json", "api_keys.*", "api-keys.*", "id_rsa*", "id_ed25519*",
    "*api-key*", "*api_key*", "*apikey*", "*secret*", "*credential*",
    "service-account*.json",
)
#: Directory names whose whole content counts as secret, copied from gates/privacy.py.
SECRET_DIRS: frozenset[str] = frozenset({"secrets", "secret", "vault", ".ssh", ".gnupg"})
#: An AutoOS spawner run id (``YYYYMMDD-HHMMSS-<name>-<hex>``); the plangraph
#: spawner writes ``run-<32 hex>``.  D-2749: no agent spawns through AutoOS.
AUTOOS_RUN_RE = re.compile(r'(?:^|[\s-])\d{8}-\d{6}-[\w.-]*-[0-9a-f]{6}\b')
PLANGRAPH_RUN_RE = re.compile(r'\brun-[0-9a-f]{32}\b')
AUTOOS_SPAWN = 'autoos spawner run id (D-2749: spawn through the plangraph spawner)'

#: Default exempt file, REPO-RELATIVE so CI reads it from the base commit.
DEFAULT_EXEMPT = 'tools/ci_commit_exempt.txt'


class ExemptError(Exception):
    '''The exempt file is missing or malformed.'''


class GateError(Exception):
    '''Git itself failed, so the range could not be inspected.'''


def _parse_exempt_text(text: str, source: str) -> set[str]:
    '''Parse exempt-file text read from source (path label for errors).'''
    exempt: set[str] = set()
    for lineno, raw in enumerate(text.removeprefix('\ufeff').splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if EXEMPT_SHA_RE.match(line) is None:
            raise ExemptError(f'exempt file {source}:{lineno}: not a 40-hex sha: {line!r}')
        exempt.add(line.lower())
    return exempt


def load_exempt(path: str | Path) -> set[str]:
    '''Parse the exempt file on disk: 40-hex shas, comments and blanks.'''
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise ExemptError(f'cannot read exempt file {path}: {exc}') from exc

    return _parse_exempt_text(text, str(path))


def _git_text(args: list[str], cwd: str | Path | None = None) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', errors='replace').strip().splitlines()
        message = detail[0] if detail else 'unknown error'
        raise GateError(f'git {args[0]} failed: {message}')
    return proc.stdout.decode('utf-8', errors='replace')


def _iter_commits(base: str, head: str, cwd: str | Path | None = None) -> list[tuple[str, str]]:
    '''Return (sha, message) for git log --no-merges base..head (newest first).'''
    out = _git_text(
        ['log', '--no-merges', '-z', '--format=%H%n%B', f'{base}..{head}'], cwd=cwd
    )
    commits: list[tuple[str, str]] = []
    for record in out.split('\0'):
        if not record.strip():
            continue
        sha, _, message = record.partition('\n')
        commits.append((sha.strip(), message))
    return commits


def _history_commits(head: str, cwd: str | Path | None = None) -> list[tuple[str, str]]:
    '''Return (sha, message) reachable from head, oldest first.'''
    out = _git_text(['log', '--no-merges', '--reverse', '-z', '--format=%H%n%B', head], cwd=cwd)
    commits: list[tuple[str, str]] = []
    for record in out.split('\0'):
        if not record.strip():
            continue
        sha, _, message = record.partition('\n')
        commits.append((sha.strip(), message))
    return commits


def canon_path(path: str | Path) -> str | None:
    '''Canonicalise repo path: backslash -> '/', leading './' dropped, posixpath.normpath.

    Returns None for absolute paths or any '..' segment (counts as malformed/blocked).
    '''
    p = str(path).replace('\\', '/')
    while p.startswith('./'):
        p = p[2:]
    norm = posixpath.normpath(p)
    if norm.startswith('/') or posixpath.isabs(norm) or any(seg == '..' for seg in norm.split('/')):
        return None
    return norm


def _secret_path(path: str | Path) -> bool:
    '''True when path (any separator) names a secret-shaped file or sits in a secret dir.'''
    parts = [p for p in PurePosixPath(str(path).replace('\\', '/').lower()).parts if p not in ('/', '.')]
    if not parts:
        return False
    if any(p.rstrip('/') in SECRET_DIRS for p in parts):
        return True
    name = parts[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAME_PATTERNS)


def gate_path_verdict(path: str | Path) -> str | None:
    '''Return 'malformed', 'door', 'secret', or None.'''
    canon = canon_path(path)
    if canon is None:
        return 'malformed'
    if any(fnmatch.fnmatchcase(canon, pattern) for pattern in AUTH_DOOR_PATHS):
        return 'door'
    if _secret_path(canon):
        return 'secret'
    return None


#: Strict trailer lines that must live in the final trailer block (D-2879).
STRICT_TRAILER_RES: tuple[re.Pattern[str], ...] = (
    WRITER_RE, HAND_EDIT_RE, PLAN_ITEM_RE, ROUTE_DECISION_RE, GATE_EDIT_RE, TEST_REMOVAL_RE,
)


def _trailer_block_lines(message: str, cwd: str | Path | None = None) -> list[str]:
    '''Return trailer lines exactly as git reads them: `git interpret-trailers --parse`.

    Only the final paragraph is a trailer block (D-2879): earlier paragraphs
    contribute nothing and a mixed final paragraph yields none, which is the
    set the keeper's %(trailers) readers see.  Strict trailer-shaped lines the
    parser drops are refused separately via _stray_trailer_lines.
    '''
    proc = subprocess.run(
        ['git', 'interpret-trailers', '--parse'],
        input=message.encode('utf-8', errors='replace'),
        cwd=cwd,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', errors='replace').strip().splitlines()
        raise GateError(
            f'git interpret-trailers failed: {detail[0] if detail else "unknown error"}'
        )
    out = proc.stdout.decode('utf-8', errors='replace')
    return [line for line in out.splitlines() if line.strip()]


def _stray_trailer_lines(message: str) -> list[str]:
    '''Strict trailer-shaped lines outside the final paragraph (D-2879).

    git drops these, so a commit could otherwise pass carrying Writer /
    Hand-Edit trailers the keeper never sees.
    '''
    paragraphs: list[list[str]] = [[]]
    for line in message.splitlines():
        if line.strip():
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    if len(paragraphs) > 1 and not paragraphs[-1]:
        paragraphs.pop()
    stray: list[str] = []
    for paragraph in paragraphs[:-1]:
        stray.extend(
            line for line in paragraph
            if any(pattern.match(line) for pattern in STRICT_TRAILER_RES)
        )
    return stray


def _trailer_values(lines: list[str], pattern: re.Pattern[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        match = pattern.match(line)
        if match:
            values.append(match.group(1))
    return values


def _has_trailer(lines: list[str], pattern: re.Pattern[str]) -> bool:
    return any(pattern.match(line) is not None for line in lines)


def _has_test_removal_trailer(lines: list[str]) -> bool:
    return any(TEST_REMOVAL_RE.match(line) is not None for line in lines)


def _parent_shas(sha: str, cwd: str | Path | None = None) -> list[str]:
    out = _git_text(['rev-list', '--parents', '-n', '1', sha], cwd=cwd)
    return out.split()[1:]


def _parent_sha(sha: str, cwd: str | Path | None = None) -> str | None:
    parents = _parent_shas(sha, cwd=cwd)
    return parents[0] if parents else None


def _cc_header_path(rest: str) -> str:
    '''Path from the tail of a `diff --cc <path>` header (git-quoted or plain).'''
    if len(rest) > 1 and rest.startswith('"') and rest.endswith('"'):
        return rest[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return rest


def _merge_diff(
    sha: str, parents: list[str], cwd: str | Path | None = None
) -> tuple[list[str], int | None]:
    '''Combined diff of a merge: (paths of its hunks, added + deleted lines).

    Only conflict-resolution content counts (``git diff-tree --cc -p`` shows
    nothing for a clean merge), so a clean merge measures 0 lines and no paths
    (E1a design row).  ``--name-only``/``--numstat`` would fall back to the
    first-parent diff and report the merged branch work instead.  A binary
    resolution yields ``None``, which callers refuse.
    '''
    out = _git_text(['diff-tree', '--no-commit-id', '--cc', '-p', sha], cwd=cwd)
    paths: list[str] = []
    total = 0
    binary = False
    current: str | None = None
    in_hunk = False

    def flush() -> None:
        nonlocal current, in_hunk
        if current is not None and in_hunk:
            paths.append(current)
        current = None
        in_hunk = False

    for line in out.splitlines():
        if line.startswith('diff --cc ') or line.startswith('diff --combined '):
            flush()
            current = _cc_header_path(line.split(' ', 2)[2])
            continue
        if current is None:
            continue
        if line.startswith('Binary files ') or line.startswith('GIT binary patch'):
            binary = True
            continue
        if line.startswith('@@'):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        # Combined-diff lines carry one marker column per parent: '+' lines are
        # the resolution's additions, '-' lines its deletions.
        markers = line[:len(parents)]
        if '+' in markers or '-' in markers:
            total += 1
    flush()
    return paths, (None if binary else total)


def _touched_paths(sha: str, cwd: str | Path | None = None) -> list[str]:
    '''Paths of sha; for a merge, only the paths of its combined-diff hunks.'''
    parents = _parent_shas(sha, cwd=cwd)
    if len(parents) > 1:
        return _merge_diff(sha, parents, cwd=cwd)[0]
    out = _git_text(
        ['diff-tree', '--no-commit-id', '--name-only', '-r', '-z', '--root', sha],
        cwd=cwd,
    )
    return [path for path in out.split('\0') if path]


def _show_file(rev: str | None, path: str, cwd: str | Path | None = None) -> str | None:
    if rev is None:
        return None
    proc = subprocess.run(['git', 'show', f'{rev}:{path}'], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode('utf-8', errors='replace')


def _test_names_from_content(path: str, content: str) -> set[str] | None:
    suffix = Path(path).suffix.lower()
    if suffix == '.py':
        return {match.group(1) for match in PY_TEST_RE.finditer(content)}
    if suffix in JS_TEST_EXTS:
        return {match.group(2) for match in JS_TEST_RE.finditer(content)}
    if suffix in SHELL_TEST_EXTS:
        names = {match.group(2) for match in BATS_TEST_RE.finditer(content)}
        names.update(match.group(1) for match in SH_TEST_FUNC_RE.finditer(content))
        return names
    return None


def _has_name_extractor(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix == '.py' or suffix in JS_TEST_EXTS or suffix in SHELL_TEST_EXTS


def _names_at(rev: str | None, path: str, cwd: str | Path | None = None) -> set[str]:
    if rev is None:
        return set()
    content = _show_file(rev, path, cwd=cwd)
    if content is None:
        return set()
    names = _test_names_from_content(path, content)
    return names if names is not None else set()


def _commit_removes_test(
    sha: str,
    base: str,
    head: str,
    cwd: str | Path | None = None,
    paths: list[str] | None = None,
) -> bool:
    parent = _parent_sha(sha, cwd=cwd)
    if paths is None:
        paths = _touched_paths(sha, cwd=cwd)
    for path in paths:
        if not TEST_PATH_RE.search(path):
            continue
        if not _has_name_extractor(path):
            if (
                _show_file(base, path, cwd=cwd) is not None
                and parent is not None
                and _show_file(parent, path, cwd=cwd) is not None
                and _show_file(sha, path, cwd=cwd) is None
                and _show_file(head, path, cwd=cwd) is None
            ):
                return True
            continue

        parent_names = _names_at(parent, path, cwd=cwd)
        current_names = _names_at(sha, path, cwd=cwd)
        removed_names = parent_names - current_names
        if not removed_names:
            continue
        base_names = _names_at(base, path, cwd=cwd)
        head_names = _names_at(head, path, cwd=cwd)
        for name in removed_names:
            if name in base_names and name not in head_names:
                return True
    return False


def _exempt_at_base(base: str, rel: str, cwd: str | Path | None = None) -> set[str] | None:
    '''Exempt shas recorded in the base commit for repo-relative rel.

    None when the file is absent at base.
    '''
    probe = subprocess.run(
        ['git', 'cat-file', '-e', f'{base}:{rel}'], cwd=cwd, capture_output=True
    )
    if probe.returncode != 0:
        return None
    text = _git_text(['show', f'{base}:{rel}'], cwd=cwd)
    return _parse_exempt_text(text, f'{base}:{rel}')


def _resolve_exempt(
    base: str, exempt: str | Path, cwd: str | Path | None = None
) -> set[str]:
    '''Load exempt; repo-relative paths come from the BASE commit only.

    An absolute path is a plain working-tree load for local use.
    '''
    if Path(exempt).is_absolute():
        return load_exempt(exempt)
    rel = Path(exempt).as_posix()
    at_base = _exempt_at_base(base, rel, cwd=cwd)
    return at_base if at_base is not None else set()


def _changed_lines(sha: str, cwd: str | Path | None = None) -> int | None:
    '''Added plus deleted lines of sha; None when a binary file is touched.

    A merge is measured by its combined diff: only its conflict-resolution
    lines count, and a clean merge counts 0 (E1a design row).
    '''
    parents = _parent_shas(sha, cwd=cwd)
    if len(parents) > 1:
        return _merge_diff(sha, parents, cwd=cwd)[1]
    out = _git_text(['diff-tree', '--no-commit-id', '--numstat', '-r', '--root', sha], cwd=cwd)
    total = 0
    for line in out.splitlines():
        added, _, rest = line.partition('\t')
        deleted = rest.partition('\t')[0]
        if added == '-' or deleted == '-':
            return None
        total += int(added) + int(deleted)
    return total


def hand_edit_commits(
    base: str, head: str, cwd: str | Path | None = None
) -> list[tuple[str, str]]:
    '''Return (sha, reason) for non-merge commits with a strict Hand-Edit.'''
    commits: list[tuple[str, str]] = []
    for sha, message in _iter_commits(base, head, cwd=cwd):
        values = _trailer_values(_trailer_block_lines(message, cwd=cwd), HAND_EDIT_RE)
        if values:
            commits.append((sha, values[0]))
    return commits


def autoos_spawn_commits(
    base: str, head: str, cwd: str | Path | None = None
) -> list[tuple[str, str]]:
    '''Return (sha, writer) for commits whose Writer names an AutoOS run id (D-2749).'''
    commits: list[tuple[str, str]] = []
    for sha, message in _iter_commits(base, head, cwd=cwd):
        for value in _trailer_values(_trailer_block_lines(message, cwd=cwd), WRITER_RE):
            if AUTOOS_RUN_RE.search(value) and not PLANGRAPH_RUN_RE.search(value):
                commits.append((sha, value))
                break
    return commits


def check(
    base: str,
    head: str,
    exempt: str | Path | None = None,
    cwd: str | Path | None = None,
    open_heads: str | Path | None = None,
) -> list[str]:
    '''Return one sha40: reason line per violation in base..head.

    Raises ExemptError for a bad exempt file and GateError when git fails.
    '''
    exempt_set = _resolve_exempt(base, exempt, cwd=cwd) if exempt else set()

    violations: list[str] = []
    if open_heads is not None:
        violations.extend(_open_pr_merges(base, head, open_heads, cwd=cwd))

    history = _history_commits(head, cwd=cwd)
    history_running_sums: dict[str, int] = {}
    exceeded_50: dict[str, bool] = {}
    rd_violations: dict[str, str] = {}

    for h_sha, h_message in history:
        if h_sha.lower() in exempt_set:
            continue
        h_block = _trailer_block_lines(h_message, cwd=cwd)
        if not _trailer_values(h_block, HAND_EDIT_RE):
            continue
        h_items = _trailer_values(h_block, PLAN_ITEM_RE)
        h_item = h_items[0] if h_items else '(none)'
        h_changed = _changed_lines(h_sha, cwd=cwd) or 0
        history_running_sums[h_item] = history_running_sums.get(h_item, 0) + h_changed
        if history_running_sums[h_item] > ROUTE_DECISION_LINES:
            exceeded_50[h_item] = True

        rd_trailers = [
            line for line in h_block
            if line.split(':', 1)[0].strip().lower() == 'route-decision'
        ]
        if exceeded_50.get(h_item):
            if len(rd_trailers) != 1 or not ROUTE_DECISION_RE.match(rd_trailers[0]):
                rd_violations[h_sha] = (
                    f'hand-edit over {ROUTE_DECISION_LINES} lines for {h_item} without Route-Decision'
                )
        elif len(rd_trailers) > 1:
            # Duplicate trailers are refused ("two trailers are refused too"),
            # but the over-50 reason is false here; name the real defect.
            rd_violations[h_sha] = (
                f'hand-edit carries multiple Route-Decision trailers for {h_item}'
            )

    range_bucket_lines: dict[str, int] = {}
    range_bucket_shas: dict[str, list[str]] = {}

    for sha, message in _iter_commits(base, head, cwd=cwd):
        if sha.lower() in exempt_set:
            continue

        all_lines = message.splitlines()
        block_lines = _trailer_block_lines(message, cwd=cwd)
        all_writer_values = _trailer_values(all_lines, WRITER_RE)
        block_writer_values = _trailer_values(block_lines, WRITER_RE)
        block_hand_edit_values = _trailer_values(block_lines, HAND_EDIT_RE)
        has_hand_edit = bool(block_hand_edit_values)

        malformed_writer = any(
            MALFORMED_WRITER_RE.match(line) is not None
            and WRITER_RE.match(line) is None
            for line in all_lines
        )
        if malformed_writer:
            violations.append(f'{sha}: {MALFORMED_WRITER}')
        if any(
            marker in value.lower()
            for value in all_writer_values
            for marker in CLAUDE_MARKERS
        ):
            violations.append(f'{sha}: {CLAUDE_WRITER}')
        if not malformed_writer and not block_writer_values and not has_hand_edit:
            violations.append(f'{sha}: {MISSING_WRITER}')

        if any(
            MALFORMED_HAND_EDIT_RE.match(line) is not None
            and HAND_EDIT_RE.match(line) is None
            for line in all_lines
        ):
            violations.append(f'{sha}: {MALFORMED_HAND_EDIT}')

        if _stray_trailer_lines(message):
            violations.append(f'{sha}: trailer outside final block')

        touched = _touched_paths(sha, cwd=cwd)
        if has_hand_edit:
            for path in touched:
                verdict = gate_path_verdict(path)
                if verdict == 'malformed':
                    violations.append(f'{sha}: hand-edit touches malformed path: {path}')
                elif verdict == 'door':
                    violations.append(f'{sha}: hand-edit touches auth/door surface: {path}')
                elif verdict == 'secret':
                    violations.append(f'{sha}: hand-edit touches secret-shaped path: {path}')

            changed = _changed_lines(sha, cwd=cwd)
            if changed is None or changed > HAND_EDIT_MAX_LINES:
                violations.append(f'{sha}: hand-edit exceeds {HAND_EDIT_MAX_LINES} changed lines')

            items = _trailer_values(block_lines, PLAN_ITEM_RE)
            item = items[0] if items else '(none)'
            lines_count = changed or 0
            range_bucket_lines[item] = range_bucket_lines.get(item, 0) + lines_count
            range_bucket_shas.setdefault(item, []).append(sha)

            if sha in rd_violations:
                violations.append(f'{sha}: {rd_violations[sha]}')

        if (
            any(
                fnmatch.fnmatchcase(canon_path(path) or path, pattern)
                for path in touched
                for pattern in GATE_PATHS
            )
            and not _has_trailer(block_lines, GATE_EDIT_RE)
        ):
            violations.append(f'{sha}: {GATE_EDIT_VIOLATION}')

        if not _has_test_removal_trailer(all_lines) and _commit_removes_test(
            sha, base, head, cwd=cwd, paths=touched
        ):
            violations.append(f'{sha}: {TEST_REMOVAL_VIOLATION}')

    for item, total in range_bucket_lines.items():
        if total > HAND_EDIT_MAX_LINES:
            tipping_sha = range_bucket_shas[item][0]
            violations.append(f'{tipping_sha}: hand-edit total over ceiling for {item}')

    return violations


def _open_pr_merges(
    base: str, head: str, path: str | Path, cwd: str | Path | None = None
) -> list[str]:
    '''Refuse merged-in parents reachable from foreign open heads but not base.'''
    def run_git(
        args: list[str], allowed: tuple[int, ...] = (0,), context: str = ''
    ) -> subprocess.CompletedProcess:
        probe = subprocess.run(['git', *args], cwd=cwd, capture_output=True)
        if probe.returncode not in allowed:
            detail = probe.stderr.decode('utf-8', errors='replace').strip()
            raise GateError(
                f'git {args[0]} failed (exit {probe.returncode}): {detail}{context}'
            )
        return probe

    def is_ancestor(ancestor: str, descendant: str) -> bool:
        # Exit 1 is a valid negative ancestry answer, not a failed probe.
        probe = run_git(['merge-base', '--is-ancestor', ancestor, descendant], allowed=(0, 1))
        return probe.returncode == 0

    reachability: dict[tuple[str, str], bool] = {}

    def reachable(parent: str, tip: str) -> bool:
        key = (parent, tip)
        if key not in reachability:
            probe = run_git(['rev-list', '-1', parent, '--not', tip])
            reachability[key] = not probe.stdout.strip()
        return reachability[key]

    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError) as exc:
        raise GateError(f'cannot read open-heads file {path}: {exc}') from exc
    heads: set[str] = set()
    for lineno, raw in enumerate(lines, start=1):
        sha = raw.strip().lower()
        if not sha:
            continue
        if OPEN_HEAD_SHA_RE.fullmatch(sha) is None:
            raise GateError(f'open-heads file {path}:{lineno}: not a 40-hex sha')
        run_git(
            ['cat-file', '-e', f'{sha}^{{commit}}'],
            context=f'; open-heads file {path}:{lineno}: not a local commit; fetch open PR heads first',
        )
        heads.add(sha)
    # A merged foreign head is an ancestor of head: keep it as evidence.
    # Only the checked head itself and stacked descendants are not foreign PRs.
    heads = {sha for sha in heads
             if sha != head and not is_ancestor(head, sha)}
    probe = run_git(['rev-list', '--min-parents=2', '--parents', f'{base}..{head}'])
    merges = probe.stdout.decode('utf-8', errors='replace')
    violations: list[str] = []
    for line in merges.splitlines():
        merge, *parents = line.split()
        # Parent zero is the lane lineage, not a merged-in branch. It may be
        # reachable from the lane's own listed head or a stacked descendant.
        for parent in parents[1:]:
            # Main remains safe even after base advances past the merged commit.
            if not reachable(parent, base) and any(
                reachable(parent, open_head)
                for open_head in sorted(heads)
            ):
                violations.append(f'{merge}: open_pr_merged {parent}')
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command')

    check_parser = subparsers.add_parser('check')
    check_parser.add_argument('--base', required=True, help='Base commit SHA')
    check_parser.add_argument('--head', required=True, help='PR head SHA, not GitHub speculative pull_request merge ref (whose second parent is the PR head)')
    check_parser.add_argument('--open-heads', help='File regenerated per run from OPEN PRs only; drop closed-unmerged PRs. The PR under test\'s own head may be listed; fetch open PR heads first so each SHA is a local commit. One 40-hex SHA per line, case-insensitive, blanks allowed, UTF-8 BOM accepted. Check is inert until a caller writes and supplies this file.')
    check_parser.add_argument(
        '--exempt',
        default=None,
        help='Exempt sha file (default: repo-relative tools/ci_commit_exempt.txt; '
        'absolute paths are a plain working-tree load, for local use only)',
    )
    check_parser.add_argument(
        '--refuse-autoos', action='store_true',
        help='Fail commits whose Writer names an AutoOS run id (D-2749); without it they are only flagged',
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage; keep main() return-based
        return int(exc.code or 0)

    if args.command != 'check':
        parser.print_help(sys.stderr)
        return 2

    exempt = args.exempt if args.exempt is not None else DEFAULT_EXEMPT
    if args.open_heads is None:
        sys.stderr.write(OPEN_PR_SKIP_NOTICE)
    try:
        violations = check(args.base, args.head, exempt=exempt, cwd=None,
                           open_heads=args.open_heads)
        hand_edits = hand_edit_commits(args.base, args.head, cwd=None)
        autoos = autoos_spawn_commits(args.base, args.head, cwd=None)
    except (ExemptError, GateError) as exc:
        sys.stderr.write(f'ci-commit-gate: {exc}\n')
        return 2

    for sha, reason in hand_edits:
        sys.stderr.write(f'hand-edit {sha}: {reason}\n')
    for sha, writer in autoos:
        if args.refuse_autoos:
            violations.append(f'{sha}: {AUTOOS_SPAWN}')
        else:
            sys.stderr.write(f'autoos-spawn {sha}: {writer} ({AUTOOS_SPAWN})\n')
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == '__main__':
    raise SystemExit(main())
