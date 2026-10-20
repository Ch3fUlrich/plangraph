"""Repository telemetry module measuring git diff and write_set features.

Classifies task complexity into S0..S4 buckets:
  - S0: trivial / docs-only / <= 1 file, <= 20 lines, fanout == 0
  - S1: small single-module change (<= 2 files, <= 80 lines, fanout <= 2)
  - S2: standard implementation (<= 5 files, <= 250 lines, fanout <= 5)
  - S3: multi-module feature/refactor (<= 12 files or fanout <= 12)
  - S4: large cross-cutting architectural change (> 12 files or fanout > 12, triggers should_decompose = True)
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

DOCS_EXTENSIONS = frozenset((".md", ".txt", ".rst", ".adoc", ".png", ".jpg", ".svg", ".drawio"))
GENERIC_TEST_TOKENS = frozenset(("test", "tests", "py", "ps1", "sh", "js", "ts"))

UBIQUITOUS_SYMBOLS_BLOCKLIST: frozenset[str] = frozenset((
    "run", "get", "set", "resolve", "validate", "to_dict", "from_dict",
    "__init__", "__str__", "__repr__", "__eq__", "__hash__", "__post_init__",
    "items", "keys", "values", "update", "parse", "close", "setup",
    "teardown", "main", "start", "stop", "execute", "handle", "process",
    "load", "dump", "dumps", "loads", "read", "write", "send", "receive",
    "call", "check", "test", "name", "id", "status", "data", "value",
    "clean", "reset", "clear", "add", "remove", "pop", "copy", "format",
))


def is_ubiquitous_symbol(symbol: str) -> bool:
    """Whether symbol is a ubiquitous method/function name that causes false fanout explosions."""
    s = str(symbol or "").strip()
    if s.startswith("__") and s.endswith("__"):
        return True
    return s.lower() in UBIQUITOUS_SYMBOLS_BLOCKLIST


def _normalize_path(path: str) -> str:
    """Normalize path with forward slashes and strip leading/trailing whitespace."""
    return str(path).strip().replace("\\", "/")


def _module_of(path: str) -> str:
    """Top-level directory component of a path, or '.' for root files."""
    normalized = _normalize_path(path)
    if "/" not in normalized:
        return "."
    return normalized.split("/", 1)[0]


def is_doc_file(path: str) -> bool:
    """Whether a path represents documentation or non-executable text assets."""
    p = _normalize_path(path)
    if p.startswith("docs/") or "/docs/" in p or p.startswith("references/") or "/references/" in p:
        return True
    base = p.rsplit("/", 1)[-1]
    if base.upper() in ("README.MD", "LICENSE", "CHANGELOG.MD", "CONTRIBUTING.MD", "AGENTS.MD", "GEMINI.MD", "CLAUDE.MD"):
        return True
    ext = os.path.splitext(base)[1].lower()
    return ext in DOCS_EXTENSIONS


def is_docs_only(paths: Sequence[str]) -> bool:
    """True if all paths are documentation or non-executable text assets."""
    if not paths:
        return True
    return all(is_doc_file(p) for p in paths)


def is_test_file(path: str) -> bool:
    """Whether path is a test file by directory location or naming convention."""
    p = _normalize_path(path)
    if p.startswith("tests/") or "/tests/" in p:
        return True
    base = p.rsplit("/", 1)[-1]
    if (base.startswith("test_") and base.endswith(".py")) or base.endswith("_test.py"):
        return True
    if base.endswith(".Tests.ps1") or base.endswith(".test.ts") or base.endswith(".spec.ts"):
        return True
    return False


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ceil(len(text) / 4)."""
    return (len(text) + 3) // 4


def classify_bucket(
    file_count: int,
    line_count: int,
    modules: int = 1,
    fanout: int = 0,
    docs_only: bool = False,
) -> tuple[str, bool]:
    """Classify complexity bucket into S0..S4 and determine whether decomposition is required.

    Returns:
        (bucket_name, should_decompose)
    """
    if docs_only and fanout == 0:
        return "S0", False

    # S0: trivial / docs-only / <= 1 file, <= 20 lines, fanout == 0
    if file_count <= 1 and line_count <= 20 and fanout == 0:
        return "S0", False

    # S4: large cross-cutting architectural change (> 12 files or fanout > 12)
    if file_count > 12 or fanout > 12:
        return "S4", True

    # S1: small single-module change (<= 2 files, <= 80 lines, fanout <= 2)
    if file_count <= 2 and line_count <= 80 and fanout <= 2:
        return "S1", False

    # S2: standard implementation (<= 5 files, <= 250 lines, fanout <= 5)
    if file_count <= 5 and line_count <= 250 and fanout <= 5:
        return "S2", False

    # S3: multi-module feature/refactor (<= 12 files or fanout <= 12)
    if file_count <= 12 and fanout <= 12:
        return "S3", False

    return "S4", True


@dataclass
class Measurement:
    """Telemetry measurement of a task or write set."""
    files: list[str] = field(default_factory=list)
    file_count: int = 0
    lines: int = 0
    modules: int = 0
    fanout: int = 0
    has_tests: bool = True
    bucket: str = "S0"
    should_decompose: bool = False
    is_docs_only: bool = False
    need_tokens: int = 0
    sources: dict[str, str] = field(default_factory=dict)


def grep_fanout(repo: str | Path | None, symbol: str) -> int:
    """Count whole-word references to symbol across tracked files in repository.

    Ignores ubiquitous method names (e.g., 'run', 'get', 'resolve', 'validate', '__init__')
    to prevent false fanout escalations.
    """
    if not repo or not symbol:
        return 0
    if is_ubiquitous_symbol(symbol):
        return 0
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        return 0
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "grep", "-w", "-c", "-F", "-e", symbol],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 1:
            return 0
        if proc.returncode != 0:
            return 0
        total = 0
        for line in proc.stdout.splitlines():
            _, sep, count = line.rpartition(":")
            if sep:
                try:
                    total += int(count)
                except ValueError:
                    pass
        return total
    except Exception:
        return 0


def ast_caller_fanout(repo: str | Path | None, modified_paths: Sequence[str]) -> int:
    """Count the number of files in the repo that import any of the modified modules using AST analysis.

    Scopes the fanout check so only files that actually import the modified module(s) are counted,
    preventing common method names from falsely escalating fanout across the entire repository.
    """
    if not repo or not modified_paths:
        return 0
    repo_path = Path(repo)
    if not repo_path.is_dir():
        return 0

    target_stems = {Path(p).stem for p in modified_paths if p.endswith(".py")}
    if not target_stems:
        return 0

    norm_targets = {_normalize_path(p) for p in modified_paths}
    importing_files: set[str] = set()

    for py_file in repo_path.rglob("*.py"):
        try:
            rel_path = _normalize_path(str(py_file.relative_to(repo_path)))
        except ValueError:
            continue
        if rel_path in norm_targets or ".venv" in rel_path or ".git" in rel_path or "site-packages" in rel_path:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if any(part in target_stems for part in parts):
                        importing_files.add(rel_path)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                parts = mod.split(".")
                if any(part in target_stems for part in parts):
                    importing_files.add(rel_path)
                for alias in node.names:
                    if alias.name in target_stems:
                        importing_files.add(rel_path)
    return len(importing_files)


def check_has_tests(paths: Sequence[str], repo: str | Path | None = None) -> bool:
    """Check whether touched paths include tests or are covered by existing test files."""
    if not paths:
        return True
    normalized_paths = [_normalize_path(p) for p in paths]
    if any(is_test_file(p) for p in normalized_paths):
        return True

    # If all touched files are documentation, tests are not required
    if is_docs_only(normalized_paths):
        return True

    if not repo:
        return False

    repo_path = Path(repo)
    stems = {
        tok
        for p in normalized_paths
        for tok in re.split(r"[_.\-]", Path(p).stem)
        if tok and tok not in GENERIC_TEST_TOKENS
    }
    tests_dir = repo_path / "tests"
    if not tests_dir.is_dir():
        return False

    for test_candidate in tests_dir.rglob("test_*.py"):
        cand_name = test_candidate.name
        cand_tokens = set(re.split(r"[_.\-]", cand_name))
        if cand_tokens & stems:
            return True
        try:
            content = test_candidate.read_text(encoding="utf-8", errors="replace")
            if any(p in content for p in normalized_paths):
                return True
        except OSError:
            continue

    return False


def measure(
    paths: Sequence[str] | None = None,
    repo: str | Path | None = None,
    lines: int | None = None,
    fanout: int | None = None,
    symbols: Sequence[str] = (),
    has_tests: bool | None = None,
    diff_text: str | None = None,
) -> Measurement:
    """Measure features for a task or write set and bucket its complexity."""
    norm_paths: list[str] = []
    if paths:
        norm_paths = [_normalize_path(p) for p in paths]
    elif diff_text:
        # Extract modified paths from diff headers
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                p = line[6:].strip()
                if p and p != "/dev/null":
                    norm_paths.append(_normalize_path(p))
            elif line.startswith("--- a/") and not norm_paths:
                p = line[6:].strip()
                if p and p != "/dev/null":
                    norm_paths.append(_normalize_path(p))
        norm_paths = list(dict.fromkeys(norm_paths))

    file_count = len(norm_paths)
    modules_set = {_module_of(p) for p in norm_paths} if norm_paths else set()
    modules_count = len(modules_set)

    # Line count determination
    sources: dict[str, str] = {}
    if lines is not None:
        computed_lines = max(0, int(lines))
        sources["lines"] = "declared"
    elif diff_text:
        computed_lines = 0
        for line in diff_text.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                computed_lines += 1
        sources["lines"] = "diff_delta"
    else:
        computed_lines = 20 * file_count
        sources["lines"] = "estimate_20_per_file"

    # Fanout determination
    if fanout is not None:
        computed_fanout = max(0, int(fanout))
        sources["fanout"] = "declared"
    elif symbols and repo:
        best_fanout = 0
        valid_symbols = [s for s in symbols if not is_ubiquitous_symbol(s)]
        for sym in valid_symbols:
            f = grep_fanout(repo, sym)
            if f > best_fanout:
                best_fanout = f
        if not valid_symbols and norm_paths:
            # All provided symbols are ubiquitous (e.g. 'run', 'get') -> scope via AST caller imports
            computed_fanout = ast_caller_fanout(repo, norm_paths)
            sources["fanout"] = "ast_imports"
        else:
            computed_fanout = best_fanout
            sources["fanout"] = "git_grep"
    elif repo and norm_paths:
        computed_fanout = ast_caller_fanout(repo, norm_paths)
        sources["fanout"] = "ast_imports"
    else:
        computed_fanout = 0
        sources["fanout"] = "default_zero"

    docs_only = is_docs_only(norm_paths) if norm_paths else False

    # Test coverage check
    if has_tests is not None:
        coverage = bool(has_tests)
        sources["tests"] = "declared"
    else:
        coverage = check_has_tests(norm_paths, repo=repo)
        sources["tests"] = "detected"

    bucket_name, should_decompose = classify_bucket(
        file_count=file_count,
        line_count=computed_lines,
        modules=modules_count,
        fanout=computed_fanout,
        docs_only=docs_only,
    )

    # Estimate tokens needed for planning/prompting
    need_tokens = 500 + (file_count * 300) + (computed_lines * 4)

    return Measurement(
        files=norm_paths,
        file_count=file_count,
        lines=computed_lines,
        modules=modules_count,
        fanout=computed_fanout,
        has_tests=coverage,
        bucket=bucket_name,
        should_decompose=should_decompose,
        is_docs_only=docs_only,
        need_tokens=need_tokens,
        sources=sources,
    )
