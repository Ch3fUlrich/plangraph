#!/usr/bin/env python3
"""Conservative AST reverse-import selection; JSON plan by default, --run executes it."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
import time


def select_tests(root, changed):
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
        return {"tests": ["tests"], "reason": "graph unreadable: " + str(exc)}
    selected = set()
    for raw in changed:
        path = Path(raw).as_posix()
        def fallback(reason):
            return {"tests": ["tests"], "reason": reason + ": " + path}
        p = Path(path)
        if p.name == "conftest.py" or (path.startswith("tests/") and not p.name.startswith("test_")):
            return fallback("shared fixture")
        if path not in texts:
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
    return {"tests": sorted(selected), "reason": "reverse imports and explicit file references"}


def changed_files(root, base):
    merge = subprocess.check_output(["git", "merge-base", base, "HEAD"], cwd=root, text=True).strip()
    tracked = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", merge], cwd=root)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
    return sorted(set(p.decode() for p in (tracked + untracked).split(b"\0") if p))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("-n", "--workers", default="auto")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    plan = select_tests(root, changed_files(root, args.base))
    print(json.dumps(plan), flush=True)
    if args.run and plan["tests"]:
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "pytest", "-n", args.workers, *plan["tests"]], cwd=root)
        print(f"affected wall seconds: {time.monotonic() - start:.3f}")
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
