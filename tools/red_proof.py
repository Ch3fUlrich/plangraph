#!/usr/bin/env python3
"""Prove changed pytest tests fail assertions against base code, never head code."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def source(repo, ref, path):
    try:
        return git(repo, "show", f"{ref}:{path}").decode()
    except subprocess.CalledProcessError:
        return ""


def tests(text):
    tree = ast.parse(text)
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            result[node.name] = ast.dump(node, include_attributes=False)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test"):
                    result[f"{node.name}::{method.name}"] = ast.dump(method, include_attributes=False)
    return result


def bound_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for target in targets for n in ast.walk(target)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        return {name for name in names if name.isidentifier()}
    return set()


def stubs(old, new):
    """Only copy names/signatures as inert placeholders, never head bodies."""
    old_names = set().union(*(bound_names(n) for n in ast.parse(old).body))
    additions = []
    for node in ast.parse(new).body:
        if getattr(node, "name", None) in old_names:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            additions.append(f"{prefix}def {node.name}(*args, **kwargs):\n    pass\n")
        elif isinstance(node, ast.ClassDef):
            additions.append(f"class {node.name}:\n    def __init__(self, *args, **kwargs):\n        pass\n")
        else:
            additions.extend(f"{name} = None\n" for name in sorted(bound_names(node) - old_names))
        old_names |= bound_names(node)
    return "\n".join(additions)


PLUGIN = '''import json
import os
import pytest
outcomes = {}
collection = []
def pytest_collectreport(report):
    if report.failed:
        collection.append(report.nodeid)
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    key = item.parent.nodeid + "::" + getattr(item, "originalname", item.name)
    status = "pass"
    if hasattr(report, "wasxfail"):
        status = "skip"
    elif report.failed:
        status = "assertion" if report.when == "call" and call.excinfo and call.excinfo.errisinstance(AssertionError) else "error"
    elif report.skipped:
        status = "skip"
    if report.when == "call" or status != "pass":
        outcomes.setdefault(key, []).append(status)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["RED_PROOF_REPORT"], "w") as handle:
        json.dump(dict(outcomes=outcomes, collection=collection), handle)
'''


def verify(repo, base, head):
    git(repo, "rev-parse", "--verify", base + "^{commit}")
    git(repo, "rev-parse", "--verify", head + "^{commit}")
    message = git(repo, "show", "-s", "--format=%B", head).decode()
    exemptions = re.findall(r"^Red-Exempt:[ \t]*(.*)$", message, re.M)
    if exemptions:
        if all(re.search(r"\bD-\d+\b", reason) for reason in exemptions):
            print("Red-Exempt: " + "; ".join(exemptions))
            return 0
        print("Red-Exempt requires a D-id: " + "; ".join(exemptions))
        return 1
    paths = git(repo, "diff", "-M", "--name-only", "--diff-filter=AMR", base, head).decode().splitlines()
    selected = []
    overlays = {}
    for path in paths:
        if path.endswith(".py"):
            try:
                ast.parse(source(repo, head, path))
            except SyntaxError as error:
                print(f"red(collection): {path}: {error.msg}")
                return 1
    for path in paths:
        if path.endswith(".py") and (Path(path).name.startswith("test_") or Path(path).name.endswith("_test.py")):
            old, new = source(repo, base, path), source(repo, head, path)
            previous = tests(old)
            selected.extend(f"{path}::{name}" for name, body in tests(new).items()
                            if previous.get(name) != body)
            overlays[path] = new
    if not selected:
        print("n/a: no changed test cases")
        return 0
    with tempfile.TemporaryDirectory(prefix="red-proof-") as directory:
        root = Path(directory)
        archive = root / "base.tar"
        archive.write_bytes(git(repo, "archive", base))
        checkout = root / "checkout"
        checkout.mkdir()
        with tarfile.open(archive) as handle:
            handle.extractall(checkout, filter="data")
        for path, text in overlays.items():
            target = checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for path in paths:
            if path.endswith(".py") and path not in overlays:
                addition = stubs(source(repo, base, path), source(repo, head, path))
                if addition or not (checkout / path).exists():
                    target = checkout / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a") as handle:
                        handle.write("\n" + addition)
        (root / "red_proof_plugin.py").write_text(PLUGIN)
        report = root / "report.json"
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   PYTHONPATH=os.pathsep.join([str(root), str(checkout), str(checkout / "src")]),
                   RED_PROOF_REPORT=str(report))
        completed = subprocess.run([sys.executable, "-m", "pytest", "-p", "red_proof_plugin",
                                    "--rootdir", str(checkout), "-q", *(f"./{node}" for node in selected)], cwd=checkout, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        if not report.exists():
            print("red(error): pytest did not produce a report\n" + completed.stdout)
            return 1
        data = json.loads(report.read_text())
        failed = bool(data["collection"]) or completed.returncode not in (0, 1)
        for node in data["collection"]:
            print(f"red(collection): {node}")
        for node in selected:
            outcomes = data["outcomes"].get(node, [])
            # Parametrizations are one test: at least one assertion is required,
            # while collection/setup/teardown errors never establish red proof.
            valid = "assertion" in outcomes and "error" not in outcomes
            label = "red(assertion)" if valid else (
                "red(error)" if "error" in outcomes else
                "neutral(skip/xfail)" if "skip" in outcomes else "not red (pass/error/missing)")
            print(f"{label}: {node}")
            failed |= not valid
        return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        return verify(args.repo, args.base, args.head)
    except (subprocess.SubprocessError, OSError, ValueError, SyntaxError, tarfile.TarError) as error:
        print(f"red-proof infrastructure error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
