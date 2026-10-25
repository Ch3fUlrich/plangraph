#!/usr/bin/env python3
"""Conservative AST reverse-import selection; JSON plan by default, --run executes it."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
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


def select_tests(root, changed, within=None):
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
            return fallback("suite configuration")
        if not p.parts or (len(p.parts) == 1 and p.name != "conftest.py"):
            continue
        if p.parts[0] not in {"src", "tests", "tools"}:
            if p.name != "conftest.py":
                if p.parts[0] in {"docs", "status"} or p.suffix in {".md", ".txt", ".rst"}:
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
        if p.name == "conftest.py" or (path.startswith("tests/") and not p.name.startswith("test_")):
            return fallback("shared fixture")
        if p.parts[0] in {"src", "tests", "tools"} and path not in texts and p.suffix == ".py":
            return fallback("unmapped file")
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
        if not hits:
            return fallback("no mapped tests")
        selected.update(hits)
    return {"tests": filter_within(sorted(selected), within), "reason": "reverse imports and explicit file references"}


def changed_files(root, base):
    merge = subprocess.check_output(["git", "merge-base", base, "HEAD"], cwd=root, text=True).strip()
    tracked = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", merge], cwd=root)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
    return sorted(set(p.decode() for p in (tracked + untracked).split(b"\0") if p))


def main(root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--run", action="store_true")
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
    plan = select_tests(root, args.changed if args.changed is not None else changed_files(root, args.base), within=args.within)
    print(json.dumps(plan), flush=True)
    if args.within is not None and not plan["tests"]:
        print("no pg-affected tests")
        return 0
    if args.run and plan["tests"]:
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "pytest", "-n", args.workers,
                                 "--dist", "loadgroup", *plan["tests"]], cwd=root)
        print(f"affected wall seconds: {time.monotonic() - start:.3f}")
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
