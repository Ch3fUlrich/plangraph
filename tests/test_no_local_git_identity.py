"""Static guard: tests must never write user.name or user.email via git config.

The scan is AST based over string-bearing nodes, after folding f-strings (a
formatted part becomes ``{}``) and ``"a" + "b"`` concatenations, so the list
form, the f-string form, the shell-string form and simple indirection are all
seen.  It is best effort: a key built from a variable defined elsewhere
escapes, which is why the allow-list is also kept to one marker, one file.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOW_MARKER = "# allow-local-identity: tests the pre-push refusal"
ALLOWED_FILE = ROOT / "test_hooks_prepush_identity.py"

CONFIG_WORD = re.compile(r"\bconfig\b")
IDENTITY_KEY = re.compile(r"user\.(?:name|email|\{\})")
LOCAL_IDENTITY_LINE = re.compile(r"--local\b.*user\.(?:name|email)")


class _Fold(ast.NodeTransformer):
    """Fold f-strings and string concatenation into plain string constants."""

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        parts = [v.value if isinstance(v, ast.Constant) else "{}" for v in node.values]
        return ast.copy_location(ast.Constant("".join(map(str, parts))), node)

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if (isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return ast.copy_location(ast.Constant(node.left.value + node.right.value), node)
        return node


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)):
                ids.add(id(body[0].value))
    return ids


def _strings(node: ast.AST, skip: set[int]) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def _writes_identity(strings: list[str]) -> bool:
    if any(CONFIG_WORD.search(s) and IDENTITY_KEY.search(s) for s in strings):
        return True  # one string: a shell command line, or "config user.name=..."
    joined = " ".join(strings)
    return bool(CONFIG_WORD.search(joined) and IDENTITY_KEY.search(joined))


def find_violations(text: str, rel: str, *, allow_marker_file: bool = False) -> list[str]:
    """Violations in *text*; the marker excuses only a one-line ``--local`` write."""
    try:
        tree = _Fold().visit(ast.parse(text))
    except SyntaxError:
        return []
    lines = text.splitlines()
    skip = _docstring_nodes(tree)
    found: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.List, ast.Tuple, ast.Assign, ast.Constant)):
            continue
        if isinstance(node, ast.Constant) and (
                not isinstance(node.value, str) or id(node) in skip):
            continue
        if not _writes_identity(_strings(node, skip)):
            continue
        first = lines[node.lineno - 1]
        one_line = getattr(node, "end_lineno", node.lineno) == node.lineno
        if (allow_marker_file and one_line and ALLOW_MARKER in first
                and LOCAL_IDENTITY_LINE.search(first)):
            continue
        found.setdefault(node.lineno, f"{rel}:{node.lineno}: {first.strip()}")
    return [found[k] for k in sorted(found)]


def _scan(path: Path) -> list[str]:
    rel = path.relative_to(ROOT.parent).as_posix()
    return find_violations(path.read_text(encoding="utf-8"), rel,
                           allow_marker_file=path.resolve() == ALLOWED_FILE.resolve())


def _test_files() -> list[Path]:
    self_path = Path(__file__).resolve()
    return [p for p in sorted(ROOT.rglob("*.py")) if p.resolve() != self_path]


def test_no_local_git_identity_in_tests():
    all_violations = [v for p in _test_files() for v in _scan(p)]
    assert not all_violations, (
        "Forbidden git config user.name/user.email calls found in tests/:\n"
        + "\n".join(all_violations)
    )


def test_allow_marker_restricted_to_prepush_identity():
    found_files: dict[str, list[tuple[int, str]]] = {}
    for py_file in _test_files():
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if ALLOW_MARKER in line:
                rel = py_file.relative_to(ROOT.parent).as_posix()
                found_files.setdefault(rel, []).append((lineno, line))

    allowed_rel = ALLOWED_FILE.relative_to(ROOT.parent).as_posix()
    assert found_files.get(allowed_rel), (
        f"Allow marker '{ALLOW_MARKER}' must be present in {allowed_rel}")
    for file_path, hits in found_files.items():
        assert file_path == allowed_rel, (
            f"Allow marker '{ALLOW_MARKER}' is only permitted in {allowed_rel}, "
            f"but found in {file_path}: lines {[n for n, _ in hits]}")
    for lineno, line in found_files[allowed_rel]:
        assert LOCAL_IDENTITY_LINE.search(line), (
            f"{allowed_rel}:{lineno}: the marker may only sit on a '--local user.*' write")


# --- the detector itself, on synthetic sources -------------------------------

import pytest  # noqa: E402

BAD = {
    "list": 'import subprocess\nsubprocess.run(["git", "config", "user.name", "x"])\n',
    "list_c": 'run(["git", "-C", p, "config", "--local", "user.email", "a@b"])\n',
    "fstring": 'run(["git", "config", f"user.name={x}", "v"])\n',
    "shell": 'subprocess.run("git config user.email a@b", shell=True)\n',
    "shell_dash_c": 'subprocess.run("git -C /tmp/r config user.name x", shell=True)\n',
    "shell_var": 'cmd = "git config user.name x"\nsubprocess.run(cmd, shell=True)\n',
    "fstring_key": 'run(["git", "config", f"user.{key}", "v"])\n',
    "concat_key": 'run(["git", "config", "user." + "name", "v"])\n',
    "multiline": 'run([\n    "git",\n    "config",\n    f"user.name={x}",\n])\n',
}


@pytest.mark.parametrize("src", BAD.values(), ids=BAD.keys())
def test_detector_catches_write_form(src):
    assert find_violations(src, "tests/x.py")


@pytest.mark.parametrize("src", [
    'run(["git", "config", "commit.gpgsign", "false"])\n',
    'args = ["-c", f"user.name={n}", "-c", f"user.email={e}"]\n',
    '"""A docstring about git config user.name is prose."""\nx = 1\n',
    'run(["git", "commit", "-m", "x"], env={"GIT_CONFIG_GLOBAL": "/dev/null"})\n',
])
def test_detector_ignores_clean_sources(src):
    assert find_violations(src, "tests/x.py") == []


LOCAL = f'run(["git", "config", "--local", "user.name", n])  {ALLOW_MARKER}\n'


def test_marker_excuses_only_a_local_write_in_the_allowed_file():
    assert find_violations(LOCAL, "t.py", allow_marker_file=True) == []
    assert find_violations(LOCAL, "t.py", allow_marker_file=False)
    no_local = f'run(["git", "config", "user.name", n])  {ALLOW_MARKER}\n'
    assert find_violations(no_local, "t.py", allow_marker_file=True)


def test_marker_on_the_opening_line_of_a_multiline_call_excuses_nothing():
    src = (f'run([  {ALLOW_MARKER} --local user.name\n'
           '    "git", "config", "user.name", n,\n])\n')
    assert find_violations(src, "t.py", allow_marker_file=True)
