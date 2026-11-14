#!/usr/bin/env python3
"""Conservative AST reverse-import selection; JSON plan by default, --run executes it."""
import argparse
import ast
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time


def revision_asset(root, path, cache=None):
    """True when git counts this non-Python asset as part of the revision.

    Ignored and untracked scratch files are skippable; a tracked asset under
    src/tests/tools (SQL, JSON, WASM, TOML, ...) is read by production code
    at runtime, so it must select the tests naming it or fall back to the
    whole suite. When git cannot answer (not a work tree, git missing) the
    answer is False, keeping the pre-repository behaviour of skipping.
    """
    if cache is None:
        cache = {}
    if "untracked" not in cache:
        try:
            out = subprocess.check_output(
                ["git", "ls-files", "-z", "--others", "--exclude-standard"],
                cwd=root, stderr=subprocess.DEVNULL)
            cache["untracked"] = {os.fsdecode(item) for item in out.split(b"\0") if item}
        except (OSError, subprocess.CalledProcessError):
            cache["untracked"] = None
    untracked = cache["untracked"]
    if untracked is None:
        return False
    if path in untracked:
        return False
    # check-ignore exits 0 for an ignored path, 1 otherwise; tracked files are
    # never reported as ignored unless --no-index is passed, and any other
    # exit is a git error where the conservative answer is "in the revision".
    probe = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode != 0


def filter_within(selected_tests, within):
    if within is None:
        return selected_tests
    within_norm = [Path(w).as_posix() for w in within]
    if "tests" in selected_tests:
        return sorted(set(within_norm))
    matched = []
    for t in selected_tests:
        t_norm = Path(t).as_posix()
        for w in within_norm:
            if t_norm == w or t_norm.startswith(w.rstrip("/") + "/"):
                matched.append(t)
                break
    return sorted(set(matched))


# Under docs/ and status/ only these documentation files select no tests.
DOC_SUFFIXES = {".md", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
SMOKE_LIMIT = 12
SMOKE_TEST = "tests/test_affected_tests.py"


def smoke_tests(texts, path):
    """Bounded stand-in for the whole suite when a code file maps to no test.

    Tests naming the file's directory (two or more path parts deep, at most
    SMOKE_LIMIT, sorted) plus the selector's own test; CI keeps the full suite.
    """
    parent = Path(path).parent.as_posix()
    near = sorted(s for s, text in texts.items() if s.startswith("tests/")
                  and Path(s).name.startswith("test_") and len(Path(parent).parts) > 1
                  and parent in text)[:SMOKE_LIMIT]
    return set(near) | ({SMOKE_TEST} if SMOKE_TEST in texts else set())


def local_control_tests(texts, path):
    """Bounded file checks for suite controls; CI owns exhaustive fixture coverage."""
    named = {name for name, text in texts.items()
             if name.startswith('tests/') and Path(name).name.startswith('test_')
             and not name.startswith('tests/browser/')
             and (path in text or Path(path).name in text)}
    near = smoke_tests(texts, path)
    selected = set(sorted(named | near)[:SMOKE_LIMIT])
    if SMOKE_TEST in texts:
        selected.add(SMOKE_TEST)
    return selected


def select_tests(root, changed, within=None, *, files_only=False, local=False):
    root = Path(root)
    paths = sorted(p for folder in ("src", "tests") for p in (root / folder).rglob("*.py"))
    modules = {}
    for p in paths:
        rel = p.relative_to(root)
        parts = list(rel.with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = rel.as_posix()
        if rel.parts[0] == "tests":
            modules.setdefault(p.stem, rel.as_posix())
    reverse = {}
    texts = {}
    try:
        for p in paths:
            rel = p.relative_to(root).as_posix()
            texts[rel] = p.read_text(encoding="utf-8")
            tree = ast.parse(texts[rel])
            module = next(k for k, v in modules.items() if v == rel)
            package = module if p.name == "__init__.py" else module.rpartition(".")[0]
            names = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = node.module or ""
                    if node.level:
                        parent = package.split(".")
                        prefix = ".".join(parent[:len(parent) - node.level + 1] + ([prefix] if prefix else []))
                    names.append(prefix)
                    names.extend(prefix + "." + a.name for a in node.names)
            for name in names:
                # Importing a child also executes package __init__ modules.
                for end in range(1, len(name.split(".")) + 1):
                    dep = modules.get(".".join(name.split(".")[:end]))
                    if dep:
                        reverse.setdefault(dep, set()).add(rel)
    except (SyntaxError, UnicodeError, OSError) as exc:
        if files_only:
            return {"tests": [], "reason": "graph unreadable: " + str(exc), "error": True}
        return {"tests": filter_within(["tests"], within), "reason": "graph unreadable: " + str(exc)}
    selected = set()
    asset_cache = {}
    for raw in changed:
        path = Path(raw).as_posix()
        def fallback(reason):
            return {"tests": filter_within(["tests"], within), "reason": reason + ": " + path}
        p = Path(path)
        # Runtime/build/status artifacts do not affect Python tests. Keep
        # explicit suite configuration conservative, even outside source roots.
        suite_config = (p.name in {"pyproject.toml", "pytest.ini", "setup.cfg", "setup.py", "tox.ini"}
                        or p.name.startswith("requirements")
                        or path.startswith(".github/workflows/"))
        if suite_config:
            if files_only:
                selected.update(local_control_tests(texts, path))
                continue
            return fallback("suite configuration")
        # docs/status trees and root files cannot affect tests; untracked scratch outside the
        # source roots is skipped; tracked files elsewhere map to tests naming them or full suite.
        if not p.parts or (len(p.parts) == 1 and p.name != "conftest.py"):
            continue
        if p.parts[0] not in {"src", "tests", "tools"}:
            if p.name != "conftest.py":
                if (p.parts[0] in {"docs", "status"} and p.suffix.lower() in DOC_SUFFIXES) or (
                        p.parts[0] == "changelog.d" and p.suffix == ".md"):
                    continue
                if not revision_asset(root, path, asset_cache):
                    continue
        elif p.name != "conftest.py" and p.suffix != ".py":
            # A tracked non-Python asset under the source roots is read by
            # production code at runtime, so it must reach the tests naming
            # it or fall back to the whole suite. Only ignored/untracked
            # scratch files stay skippable here.
            if not revision_asset(root, path, asset_cache):
                continue
        if p.name == "conftest.py" or (not local and path.startswith("tests/") and not p.name.startswith("test_")):
            if files_only:
                selected.update(local_control_tests(texts, path))
                continue
            return fallback("shared fixture")
        if p.parts[0] in {"src", "tests", "tools"} and path not in texts and p.suffix == ".py":
            selected.update(smoke_tests(texts, path))
            continue
        seen, todo = set(), [path]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            todo.extend(reverse.get(current, ()))
        hits = {s for s in seen if s.startswith("tests/") and Path(s).name.startswith("test_")}
        # Include subprocess/dynamic consumers naming the file or module.
        hits.update(s for s, text in texts.items() if s.startswith("tests/")
                    and Path(s).name.startswith("test_")
                    and (p.name in text or path in text))
        if not hits and p.suffix in {".md", ".rst"}:
            continue  # prose no test names cannot change a test result (D-5827)
        if not hits:
            selected.update(smoke_tests(texts, path))
            continue
        selected.update(hits)
    selected = {t for t in selected if not t.startswith("tests/browser/")}  # own CI job (D-2821)
    return {"tests": filter_within(sorted(selected), within), "reason": "reverse imports and explicit file references"}


def changed_files(root, base, head=None):
    head = head or None
    merge = subprocess.check_output(["git", "merge-base", base, "HEAD" if head is None else head], cwd=root, text=True).strip()
    revisions = [merge, head] if head is not None else [merge]
    tracked = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", *revisions], cwd=root)
    untracked = (b"" if head is not None else subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root))
    return sorted(set(p.decode() for p in (tracked + untracked).split(b"\0") if p))


def test_environment(root, env):
    """Detach test subprocesses from every Git-advertised repository binding."""
    local = set(subprocess.check_output(
        ['git', 'rev-parse', '--local-env-vars'], cwd=root, env=env,
        text=True, stderr=subprocess.DEVNULL, timeout=30).upper().splitlines())
    local.add('GIT_PREFIX')
    return {key: value for key, value in env.items()
            if key.upper() not in local and not key.upper().startswith('GIT_CONFIG')}


def main(root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", help="committed comparison endpoint; exclude local changes")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--local", action="store_true", help="follow shared-asset consumers; refuse full-suite execution")
    parser.add_argument("--files-only", action="store_true", help="local hook: explicit affected files and bounded suite-control checks; never collect the whole tree")
    parser.add_argument("--maxfail", type=int, default=0)
    parser.add_argument("--changed", action="append", help="explicit sample path (repeatable); bypass git diff")
    parser.add_argument("-n", "--workers", default="auto")
    parser.add_argument("--within", nargs="+", action="extend", help="restrict selection to given paths")
    parser.add_argument("--root", default=None, help="repository root")
    args = parser.parse_args()
    if root is None:
        root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    else:
        root = Path(root).resolve()
    if args.within is not None:
        # A stale or typo --within entry must never pass vacuously.
        for w in args.within:
            if not (root / w).exists():
                print(f"unknown --within path: {w}", file=sys.stderr)
                return 2
    plan = select_tests(root, args.changed if args.changed is not None else changed_files(root, args.base, args.head), within=args.within, files_only=args.files_only, local=args.local)
    print(json.dumps(plan), flush=True)
    if args.local and args.run and "tests" in plan["tests"]:
        print('local preflight refuses the full suite; specify affected test files with --within', file=sys.stderr)
        return 2
    if args.files_only and (plan.get('error') or any(not (root / path).is_file() for path in plan['tests'])):
        print('FAIL local-selection: cannot determine explicit test files; repair the graph or selection; full collection refused', file=sys.stderr)
        return 1
    if args.within is not None and not plan["tests"]:
        print("no pg-affected tests")
        return 0
    if args.run and not plan["tests"]:
        print("no affected tests")
        return 0
    if args.run and plan["tests"]:
        if importlib.util.find_spec("pytest") is None:
            print(f"FAIL pytest-missing: {sys.executable}; install with {sys.executable} -m pip install -e .[test]", file=sys.stderr)
            return 1
        placement = any('placement' in Path(path).name or
                        (root / path).is_dir() and any((root / path).rglob('*placement*.py'))
                        for path in plan['tests'])
        if placement and not shutil.which('cargo'):
            print('CARGO FAIL: cargo is required for selected placement tests', file=sys.stderr)
            return 1
        try:
            env = test_environment(root, os.environ)
        except (OSError, subprocess.SubprocessError):
            print('FAIL test-env: cannot discover Git repository bindings', file=sys.stderr)
            return 1
        start = time.monotonic()
        parallel = (["-n", args.workers, "--dist", "loadgroup"]
                    if importlib.util.find_spec("xdist") is not None else [])
        result = subprocess.run([sys.executable, "-m", "pytest", *parallel, "--durations=30",
                                 *(["--maxfail", str(args.maxfail)] if args.maxfail else []), *plan["tests"]], cwd=root, env=env)
        print(f"affected wall seconds: {time.monotonic() - start:.3f}")
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
