#!/usr/bin/env python3
"""Conservative AST reverse-import selection; JSON plan by default, --run executes it."""
import argparse
import ast
import json
import io
import importlib.util
import os
import re
from pathlib import Path
import subprocess
import shutil
import shlex
import sys
import time
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgtest import affected_pytest, keeper_postgres_files, postgres_bin, run_keeper_postgres, test_environment


# Quiet loop: a pass prints little; --loud restores pytest's default report.
# --durations stays on, listing only tests slower than a second.
QUIET_FLAGS = ["-q", "--tb=short", "--no-header"]
QUIET_DURATIONS = ["--durations-min=1.0"]
# A failed run with at most this many failed ids is rerun to name flakes.
MAX_RERUN_FAILURES = 50
# Controller-only outcome recorder (xdist workers carry workerinput and stay silent),
# written to a temp dir and loaded with -p; .pytest_cache is never read.
OUTCOME_PLUGIN = """import json, os
_outcomes = {}
_config = None


def pytest_configure(config):
    global _config
    _config = config


def pytest_runtest_logreport(report):
    if hasattr(_config, "workerinput"):
        return
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif _outcomes.get(report.nodeid) != "failed":
        if report.when == "call" and report.passed:
            _outcomes[report.nodeid] = "passed"
        elif report.skipped:
            _outcomes[report.nodeid] = "skipped"


def pytest_sessionfinish(session):
    if not hasattr(session.config, "workerinput"):
        # xdist's scheduler exists only when workers were really started.
        workers = session.config.pluginmanager.get_plugin("dsession") is not None
        with open(os.environ["AFFECTED_OUTCOMES"], "w") as out:
            json.dump({"outcomes": _outcomes, "workers": workers}, out)
"""


def read_record(text):
    """(outcomes, workers started) from a recorder file; anything malformed is ({}, False)."""
    try:
        record = json.loads(text)
        outcomes, workers = record["outcomes"], record["workers"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}, False
    if (not isinstance(outcomes, dict) or not isinstance(workers, bool)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in outcomes.items())):
        return {}, False
    return outcomes, workers


def read_record_file(path):
    """read_record of a file; a missing, unreadable or non-UTF-8 file is ({}, False)."""
    try:
        return read_record(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}, False


def tree_hash(root):
    """Hash of tracked changes plus untracked, non-ignored files; None when git cannot say."""
    import hashlib
    try:
        # The HEAD commit is part of the identity: a run that commits a change moves it.
        digest = hashlib.sha256(subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL))
        digest.update(subprocess.check_output(
            ["git", "diff", "--binary", "HEAD"], cwd=root, stderr=subprocess.DEVNULL))
        listed = subprocess.check_output(
            ["git", "ls-files", "-z", "-o", "--exclude-standard"], cwd=root,
            stderr=subprocess.DEVNULL)
        for name in sorted(item for item in listed.split(b"\0") if item):
            digest.update(name + b"\0")
            try:
                digest.update((Path(root) / os.fsdecode(name)).read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
        return digest.hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def flake_labels(failed, first_rerun, second_rerun, hashes, parallel=True):
    """FLAKY needs an explicit pass on both reruns and one tree hash around every run.

    FAILED-IN-PARALLEL needs workers that actually ran (parallel); a serial sequence has none."""
    stable = None not in hashes and len(set(hashes)) == 1
    lines = []
    for nodeid in failed:
        low, high = first_rerun.get(nodeid), second_rerun.get(nodeid)
        if "passed" in (low, high) and not stable:
            lines.append(f"FAILED-THEN-PASSED: {nodeid}")
        elif parallel and low == "passed" and high == "failed":
            lines.append(f"FAILED-IN-PARALLEL: {nodeid}")
        elif low == high == "passed":
            lines.append(f"FLAKY (rerun -n0; a parallel race is not excluded): {nodeid}")
    return lines


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
LOCAL_TEST_LIMIT = 39
WORKFLOW_TESTS = {
    "tests/test_ci_select.py",
    "tests/test_ci_select_rust_inputs.py",
    "tests/test_workflow_archive.py",
    "tests/test_split_ci_test_affected_tests.py",
    "tests/test_ci_ui_browser_deps.py",
}
SMOKE_TEST = "tests/test_affected_tests.py"
# A change under these packages reaches most of the suite through the runner; the local bound
# would drop tests CI then fails (LE-AFFECTED-TESTS-MISS-SPAWNER-TARGET-CHANGES), so it is lifted.
UNBOUNDED_PREFIXES = ("src/plangraph/spawner/", "src/plangraph/planner/", "src/plangraph/routing_support/")
LOCAL_POLICY_TESTS = {
    "tests/test_spawner_utf8.py",
    "tests/test_check_file_size.py",
    "tests/test_check_new_code_lang.py",
    "tests/test_check_changelog_docs.py",
}


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
    selected = named | near
    if SMOKE_TEST in texts:
        selected.add(SMOKE_TEST)
    return selected


def initializer_changes(root, changed, texts, base, head):
    """Map changed initializers to changed bindings, or None for dynamic changes."""
    initializers = {path: None for path in changed
                    if Path(path).name == "__init__.py" and path in texts}
    if not initializers:
        return initializers
    try:
        revision = subprocess.check_output(
            ["git", "merge-base", base, head or "HEAD"], cwd=root,
            stderr=subprocess.DEVNULL, text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError):
        return initializers
    for path in initializers:
        try:
            before = subprocess.check_output(
                ["git", "show", f"{revision}:{path}"], cwd=root,
                stderr=subprocess.DEVNULL, encoding="utf-8", timeout=30)
            initializers[path] = changed_bindings(before, texts[path])
        except (OSError, subprocess.SubprocessError, SyntaxError, UnicodeError):
            continue
    return initializers


def changed_bindings(before, after):
    def statements(text):
        return {ast.dump(node): node for node in ast.parse(text).body}

    old, new = statements(before), statements(after)
    changed = set()
    for key in old.keys() ^ new.keys():
        node = (old if key in old else new)[key]
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            changed.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            changed.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            changed.update(child.id for child in ast.walk(node)
                           if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store))
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                  and isinstance(node.value.value, str)):
            return None
    return None if changed & {"*", "__all__", "__getattr__"} else changed


def dependency_graph(texts, *, local=False, init_changes=None):
    """Reverse imports plus static module launches and tool-script consumers."""
    modules = {}
    tool_aliases = {}
    for rel in texts:
        p = Path(rel)
        if p.suffix != ".py":
            continue
        parts = list(p.with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = rel
        if p.parts[0] == "tests":
            modules.setdefault(p.stem, rel)
        elif p.parts[0] == "tools":
            # Executable tools add their own directory to sys.path. A bare
            # import may name several tools; retaining all candidates is safe.
            tool_aliases.setdefault(p.stem, set()).add(rel)
    reverse = {}
    init_changes = init_changes or {}

    def from_import(prefix, aliases):
        names = [f"{prefix}.{alias.name}" for alias in aliases]
        module = modules.get(prefix)
        if not local or not module or Path(module).name != "__init__.py":
            return [prefix, *names]
        symbols = init_changes.get(module)
        if any(alias.name == "*" or
               (symbols is None and name not in modules) or
               (symbols is not None and alias.name in symbols)
               for alias, name in zip(aliases, names)):
            names.append(prefix)
        return names

    tool_paths = [rel for rel in texts if rel.startswith("tools/")]
    for rel, text in texts.items():
        names = []
        launches = []
        if rel.endswith(".py"):
            tree = ast.parse(text)
            module = next(k for k, v in modules.items() if v == rel)
            package = module if Path(rel).name == "__init__.py" else module.rpartition(".")[0]
            strings = []
            tool_commands = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    method = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if method in {"import_module", "run_module"} and node.args:
                        argument = node.args[0]
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            launches.append(argument.value)
                    if method in {"run", "Popen", "call", "check_output", "check_call", "run_path"}:
                        arguments = node.args[:1] + [kw.value for kw in node.keywords
                                                     if kw.arg in {"args", "path_name"}]
                        for argument in arguments:
                            tool_commands.extend(child.value for child in ast.walk(argument)
                                                 if isinstance(child, ast.Constant) and isinstance(child.value, str))
                            if isinstance(argument, (ast.List, ast.Tuple)):
                                for flag, value in zip(argument.elts, argument.elts[1:]):
                                    if (isinstance(flag, ast.Constant) and flag.value == "-m"
                                            and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                                        launches.append(value.value)
                if isinstance(node, ast.Import):
                    names.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = node.module or ""
                    if node.level:
                        parent = package.split(".")
                        prefix = ".".join(parent[:len(parent) - node.level + 1] + ([prefix] if prefix else []))
                    names.extend(from_import(prefix, node.names))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    strings.append(node.value)
            # Literal argv entries, runpy module names, and shell command strings.
            launches.extend(value for value in strings
                            if (value in modules or value + ".__main__" in modules)
                            and not (local and Path(modules.get(value, "")).name == "__init__.py"))
            command_text = "\n".join(strings)
        else:
            # Shell scripts may contain Python heredocs as well as -m commands.
            command_text = text
            # Shell expansion/operators can make a literal Python body
            # unparsable before execution. Keep its statically named imports
            # conservatively, even inside quotes or command substitution.
            names.extend(re.findall(r"\bfrom\s+([\w.]+)\s+import\b", text))
            for prefix, imported in re.findall(
                    r"\b(?:from\s+([\w.]+)\s+)?import\s+\(?\s*"
                    r"([\w.*]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.*]+(?:\s+as\s+\w+)?)*)", text):
                names.extend((prefix + "." if prefix else "") + alias.split()[0]
                             for alias in imported.split(","))
            snippets = re.findall(r"^\s*(?:from\s+[\w.]+\s+import|import)\s+[^\n]+", text, re.M)
            # Parse complete heredocs so parenthesized and continued imports
            # retain their child modules. Non-Python heredocs are ignored.
            snippets.extend(match.group(3) for match in re.finditer(
                r"<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n(.*?)^[ \t]*\2[ \t]*$",
                text, re.M | re.S))
            commands, scanned = [text], set()
            while commands:
                command = commands.pop()
                if command in scanned:
                    continue
                scanned.add(command)
                try:
                    words = shlex.split(command, comments=True)
                except ValueError:
                    # Heredoc prose may have shell-unbalanced quotes; valid
                    # same-line commands elsewhere still carry dependencies.
                    commands.extend(command.splitlines() if "\n" in command else [])
                    continue
                for index, word in enumerate(words):
                    body = (words[index + 1] if word == "-c" and index + 1 < len(words)
                            else word[2:] if word.startswith("-c") and len(word) > 2 else "")
                    if body:
                        snippets.append(body)
                        if "-c" in body:
                            commands.append(body)  # Literal nested shell command.
            for snippet in snippets:
                try:
                    statements = ast.walk(ast.parse(snippet.strip()))
                except SyntaxError:
                    continue
                for statement in statements:
                    if isinstance(statement, ast.ImportFrom):
                        prefix = statement.module or ""
                        names.extend(from_import(prefix, statement.names))
                    elif isinstance(statement, ast.Import):
                        names.extend(alias.name for alias in statement.names)
        launches.extend(re.findall(r"(?<!\w)-m\s+['\"]?([\w.]+)", command_text))
        names.extend(launches)
        names.extend(name + ".__main__" for name in launches if name + ".__main__" in modules)
        for name in names:
            # CI includes implicit package initialization when importing children.
            parts = name.split(".")
            for end in range(len(parts) if local else 1, len(parts) + 1):
                prefix = ".".join(parts[:end])
                deps = set(tool_aliases.get(prefix, ()))
                if prefix in modules:
                    deps.add(modules[prefix])
                for dep in deps:
                    reverse.setdefault(dep, set()).add(rel)
        for script in tool_paths:
            # Production prose may recommend a Python tool without executing it.
            references = ("\n".join(tool_commands) if script.endswith(".py")
                          and rel.endswith(".py") and not rel.startswith("tests/") else text)
            if script in references or Path(script).name in references:
                reverse.setdefault(script, set()).add(rel)
    return reverse


def base_dependency_graph(root, base, head):
    """Read the comparison's merge base without restoring deleted checkout files."""
    revision = subprocess.check_output(
        ["git", "merge-base", base, head or "HEAD"], cwd=root,
        stderr=subprocess.DEVNULL, text=True, timeout=30).strip()
    archive = subprocess.check_output(["git", "archive", revision], cwd=root,
                                      stderr=subprocess.DEVNULL, timeout=30)
    texts = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for entry in tree:
            p = Path(entry.name)
            if entry.isfile() and p.parts[0] in {"src", "tests", "tools"} and (
                    p.suffix == ".py" or p.parts[0] == "tools" and p.suffix == ".sh"):
                texts[entry.name] = tree.extractfile(entry).read().decode("utf-8")
    return dependency_graph(texts)


def selection_plan(selected, within, reason, *, bounded=False, priority=(), direct=()):
    tests = filter_within(sorted(selected), within)
    plan = {"tests": tests, "reason": reason}
    if bounded and len(tests) > LOCAL_TEST_LIMIT:
        ranked = sorted(tests, key=lambda path: (path not in priority, path not in direct, path))
        plan["tests"] = sorted(ranked[:LOCAL_TEST_LIMIT])
        plan["ci_deferred"] = len(tests) - LOCAL_TEST_LIMIT
        plan["reason"] += "; additional files deferred to CI"
    return plan


def select_tests(root, changed, within=None, *, files_only=False, local=False,
                 base="HEAD", head=None, keep_local_bound=False):
    root = Path(root)
    changed = [Path(path).as_posix() for path in changed]
    bounded = local or files_only
    if changed and all(path.startswith(".github/workflows/") for path in changed):
        regressions = WORKFLOW_TESTS | {path.relative_to(root).as_posix()
                                       for path in (root / "tests").glob("test_affected_tests*.py")}
        if local:
            regressions |= LOCAL_POLICY_TESTS
        return selection_plan({path for path in regressions if (root / path).is_file()},
                              within, "workflow regressions", bounded=bounded,
                              priority=LOCAL_POLICY_TESTS)
    texts = {}
    try:
        for folder in ("src", "tests", "tools"):
            for p in sorted((root / folder).rglob("*")):
                if p.is_file() and (p.suffix == ".py" or folder == "tools" and p.suffix == ".sh"):
                    texts[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
        init_changes = initializer_changes(root, changed, texts, base, head) if local else {}
        reverse = dependency_graph(texts, local=local, init_changes=init_changes)
    except (SyntaxError, UnicodeError, OSError) as exc:
        if files_only:
            return {"tests": [], "reason": "graph unreadable: " + str(exc), "error": True}
        return {"tests": filter_within(["tests"], within), "reason": "graph unreadable: " + str(exc)}
    # Repository-wide checks have no import edge from many files they govern.
    # Keep them outside affected-file selection, including docs-only local runs.
    selected = LOCAL_POLICY_TESTS.intersection(texts) if local else set()
    priority = LOCAL_POLICY_TESTS | set(changed)
    direct = set()
    asset_cache = {}
    base_reverse = None
    for path in changed:
        # CI must enforce the subprocess guard when either test language changes.
        if (path.startswith('rust/') and path.endswith('.rs')
                or path.startswith('tests/') and path.endswith('.py')):
            guard = 'tests/test_spawner_import_guard.py'
            if guard in texts:
                selected.add(guard)
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
        # Most docs/status and root files cannot affect tests; untracked scratch outside the
        # source roots is skipped; tracked files elsewhere map to tests naming them or full suite.
        if not p.parts or (len(p.parts) == 1 and p.name not in {"conftest.py", "rust-toolchain.toml"}):
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
        if p.parts[0] in {"src", "tests", "tools"} and path not in texts and p.suffix == ".py":
            if p.name == "conftest.py":
                return fallback("deleted shared fixture")
            try:
                if base_reverse is None:
                    base_reverse = base_dependency_graph(root, base, head)
                for dep, consumers in base_reverse.items():
                    reverse.setdefault(dep, set()).update(consumers)
            except (OSError, subprocess.SubprocessError, tarfile.TarError,
                    SyntaxError, UnicodeError):
                return fallback("deleted module graph unreadable")
            seen, todo = set(), [path]
            while todo:
                current = todo.pop()
                if current not in seen:
                    seen.add(current)
                    todo.extend(reverse.get(current, ()))
            hits = {s for s in seen if s in texts and s.startswith("tests/")
                    and Path(s).name.startswith("test_") and not s.startswith("tests/browser/")}
            hits.update(s for s, text in texts.items() if s.startswith("tests/")
                        and Path(s).name.startswith("test_") and not s.startswith("tests/browser/")
                        and (p.name in text or path in text))
            if not hits:
                return fallback("deleted module importers unresolved")
            selected.update(hits)
            continue
        if p.name == "conftest.py" or (not local and path.startswith("tests/") and not p.name.startswith("test_")):
            if files_only:
                selected.update(local_control_tests(texts, path))
                continue
            return fallback("shared fixture")
        seen, todo = set(), [path]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            if local and Path(current).name == "__init__.py":
                if current == path:
                    seen.update(reverse.get(current, ()))
                continue
            todo.extend(reverse.get(current, ()))
        hits = {s for s in seen if s.startswith("tests/") and Path(s).name.startswith("test_")}
        # Include subprocess/dynamic consumers naming the file or module.
        named = {s for s, text in texts.items() if s.startswith("tests/")
                 and Path(s).name.startswith("test_")
                 and (path in text or (p.name != "__init__.py" or not local) and p.name in text)}
        hits.update(named)
        direct.update(name for name in named if path in texts[name])
        direct.update(reverse.get(path, ()))
        if not hits and p.suffix in {".md", ".rst"}:
            continue  # prose no test names cannot change a test result (D-5827)
        if not hits:
            selected.update(smoke_tests(texts, path))
            continue
        selected.update(hits)
    selected = {t for t in selected if not t.startswith("tests/browser/")}  # own CI job (D-2821)
    reason = "reverse imports and explicit file references"
    if bounded and not keep_local_bound and any(path.startswith(UNBOUNDED_PREFIXES) for path in changed):
        bounded, reason = False, reason + "; spawner/planner core change, no local bound"
    return selection_plan(selected, within, reason, bounded=bounded, priority=priority, direct=direct)


def changed_files(root, base, head=None):
    head = head or None
    merge = subprocess.check_output(["git", "merge-base", base, "HEAD" if head is None else head], cwd=root, text=True).strip()
    revisions = [merge, head] if head is not None else [merge]
    tracked = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", *revisions], cwd=root)
    untracked = (b"" if head is not None else subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root))
    return sorted(set(p.decode() for p in (tracked + untracked).split(b"\0") if p))


def pushed_parts_files(root, base, head, union=False, standalone=False):
    """Return interaction paths only for a verifiable batch; otherwise use normal diff.

    union=True returns every file any part changed, so a train selects the tests a part
    breaks in a file no other part touched (imports and fixtures follow from select_tests)."""
    def git(*args):
        return subprocess.check_output(
            ["git", "--no-replace-objects", *args], cwd=root,
            stderr=subprocess.DEVNULL, timeout=30)

    def paths(*args):
        return {os.fsdecode(p) for p in git(*args).split(b"\0") if p}

    try:
        # Do not trust incomplete history or walk across a base on a side branch
        # (notably the origin/main refresh base chosen by the hook).
        if git("rev-parse", "--is-shallow-repository").strip() != b"false":
            return None
        if standalone:
            # Only the two first-parent histories define the range. A part independently
            # merged into main is a side parent and cannot replace the train's base.
            commits = git("rev-list", "--first-parent", "--exclude-first-parent-only",
                          "--reverse", "--parents", head, "^" + base).decode().splitlines()
            first = commits[0].split() if commits else []
            if len(first) < 2:
                return None
            previous = first[1]
        else:
            previous = git("rev-parse", "--verify", base + "^{commit}").strip().decode()
            commits = git("rev-list", "--first-parent", "--reverse", "--parents",
                          f"{base}..{head}").decode().splitlines()
        # A previously pushed feature/batch tip can already contain interacting
        # changes outside this range. Keep its normal selection, including #957's
        # divergent-main fallback; optimize only a base in known main history.
        git("merge-base", "--is-ancestor", previous, "refs/remotes/origin/main")
        selected, touched, parts = set(), set(), 0
        for row in commits:
            commit, *parents = row.split()
            if not parents or parents[0] != previous or len(parents) > 2:
                return None
            previous = commit
            if len(parents) == 1:
                selected.update(paths("diff", "--name-only", "--no-renames", "-z",
                                      parents[0], commit))
                continue
            # Exact ancestry, never patch equivalence: a rewritten/unpushed
            # head must not inherit the original head's test exemption.
            refs = git("for-each-ref", "--format=%(refname)",
                       "--contains=" + parents[1], "refs/remotes/origin/")
            if not any(not ref.endswith(b"/HEAD") for ref in refs.splitlines()):
                return None
            common = git("merge-base", *parents).strip().decode()
            changed = paths("diff", "--name-only", "--no-renames", "-z", common, parents[1])
            selected.update(touched & changed)
            touched.update(changed)
            # Unlike the first-parent diff this includes only resolutions and
            # edits made by the merge itself, including clean evil merges.
            selected.update(paths("show", "--remerge-diff", "--format=",
                                  "--name-only", "--no-renames", "-z", commit))
            parts += 1
        if union:
            selected.update(touched)
        return (sorted(selected), parts) if parts else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def main(root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", help="committed comparison endpoint; exclude local changes")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--fleet-spawnd-prebuilt", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--collect-only", action="store_true", help="validate the unsharded selection without running tests")
    parser.add_argument("--local", action="store_true", help="follow shared-asset consumers; refuse full-suite execution")
    parser.add_argument("--files-only", action="store_true", help="local hook: explicit affected files and bounded suite-control checks; never collect the whole tree")
    parser.add_argument("--loud", action="store_true", help="pytest's default report, no quiet flags")
    parser.add_argument("--keep-local-bound", action="store_true", help="keep the local test bound for spawner/planner core changes (the pre-push hook has a fixed deadline)")
    parser.add_argument("--parts-union", action="store_true", help="a pushed batch or train selects the union of every part's changed files, not only the files parts share")
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
    changed = args.changed
    batch = None
    pushed_range = os.environ.get("PLANGRAPH_PUSH_RANGE", "").split(":")
    hook_selection = args.files_only and args.local and args.head and args.changed is None
    if hook_selection and args.parts_union and "PLANGRAPH_PUSH_RANGE" not in os.environ:
        batch = pushed_parts_files(root, args.base, args.head, union=True, standalone=True)
    elif hook_selection and len(pushed_range) == 2 and pushed_range[1] == args.head:
        batch = pushed_parts_files(root, pushed_range[0], args.head, union=args.parts_union)
    if changed is None:
        changed = batch[0] if batch else changed_files(root, args.base, args.head)
    plan = select_tests(root, changed, within=args.within, files_only=args.files_only, local=args.local,
                        base=args.base, head=args.head, keep_local_bound=args.keep_local_bound)
    # Preflight's timeout diagnostics parse the first combined output line.
    print(json.dumps(plan), flush=True)
    if hook_selection:
        print(f"pre-push selection: mode={'pushed-parts' if batch else 'normal'} "
              f"parts={batch[1] if batch else 0} delta_files={len(changed)} "
              f"selected_tests={len(plan['tests'])}", file=sys.stderr, flush=True)
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
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(root / "src"), env.get("PYTHONPATH")) if part)
        start = time.monotonic()
        fleet = (args.local and not args.collect_only
                 and 'tests/test_fleet_snapshot.py' in plan['tests'])
        if fleet and not args.fleet_spawnd_prebuilt:
            # Push preflight runs in a detached checkout, even when the writer
            # already built pg-spawnd in the worktree that launched the hook.
            print('fleet tests: building pg-spawnd from the selected checkout', flush=True)
            try:
                build = subprocess.run(
                    ['cargo', 'build', '--locked', '--manifest-path', 'rust/Cargo.toml', '-p', 'pg-spawnd'],
                    cwd=root, env=env)
                if build.returncode:
                    return build.returncode
            except OSError as error:
                print(f'CARGO FAIL: could not prepare pg-spawnd ({error})', file=sys.stderr)
                return 1
        if args.collect_only:
            env.pop("PYTEST_SHARD", None)
        parallel = (["-n", args.workers, "--dist", "loadgroup"]
                    if not args.collect_only and importlib.util.find_spec("xdist") is not None else [])
        quiet = [] if args.loud else QUIET_FLAGS
        with affected_pytest() as (scratch, run_pytest):
            if fleet:
                staged = str(Path(scratch) / ('pg-spawnd.exe' if os.name == 'nt' else 'pg-spawnd'))
                verify = subprocess.run([sys.executable, 'tests/_spawnd.py', staged], cwd=root, env=env)
                if verify.returncode:
                    return verify.returncode
                env["PLANGRAPH_TEST_FLEET_SPAWND"] = staged
            if args.collect_only:
                result = run_pytest([sys.executable, "-m", "pytest", *parallel, "--durations=30",
                                     "--collect-only",
                                     *(["--maxfail", str(args.maxfail)] if args.maxfail else []),
                                     *plan["tests"]], cwd=root, env=env)
                print(f"affected wall seconds: {time.monotonic() - start:.3f}")
                return result.returncode
            Path(scratch, "affected_outcomes.py").write_text(OUTCOME_PLUGIN)
            runs = []

            def pytest(options, capture=False):
                """One pytest run: (exit code, {nodeid: outcome}, workers started) from the controller's recorder."""
                outcomes = Path(scratch) / f"outcomes-{len(runs)}.json"
                runs.append(outcomes)
                run_env = dict(env, AFFECTED_OUTCOMES=str(outcomes), PYTHONPATH=os.pathsep.join(
                    part for part in (scratch, env.get("PYTHONPATH")) if part))
                result = run_pytest(
                    [sys.executable, "-m", "pytest", "-p", "affected_outcomes", *options],
                    cwd=root, env=run_env,
                    **({"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if capture else {}))
                return result.returncode, *read_record_file(outcomes)

            hashes = [tree_hash(root)]
            code, first, _ = pytest([*parallel, "--durations=30", *quiet, *([] if args.loud else QUIET_DURATIONS),
                                  *(["--maxfail", str(args.maxfail)] if args.maxfail else []),
                                  *plan["tests"]])
            print(f"affected wall seconds: {time.monotonic() - start:.3f}")
            failed = sorted(nodeid for nodeid, outcome in first.items() if outcome == "failed")
            # Exit 1 only: a collection or usage error (2, 3, 4) is not a test failure.
            if code == 1 and 0 < len(failed) <= MAX_RERUN_FAILURES and not os.environ.get("CI_CLASSIFY_RETRY"):   # CI: one same-SHA retry classifies
                hashes.append(tree_hash(root))
                serial = ["-n", "0"] if parallel else []
                _, low, _ = pytest([*serial, *QUIET_FLAGS, *failed], capture=True)
                hashes.append(tree_hash(root))
                _, high, ran_workers = pytest([*parallel, *QUIET_FLAGS, *failed], capture=True)
                hashes.append(tree_hash(root))
                for line in flake_labels(failed, low, high, hashes, parallel=ran_workers):
                    print(line)
            if args.within is not None:
                return code
            postgres = run_keeper_postgres(root, changed, env, runner=run_pytest)
            return code or postgres
    return 0


if __name__ == "__main__":
    sys.exit(main())
