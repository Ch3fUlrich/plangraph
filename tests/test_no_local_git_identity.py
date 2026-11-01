"""Static guard: tests must never write user.name or user.email via git config."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOW_MARKER = "# allow-local-identity: tests the pre-push refusal"
ALLOWED_FILE = ROOT / "test_hooks_prepush_identity.py"

CONFIG_USER_RE = re.compile(
    r"""['"]config['"]\s*,\s*(?:['"][^'"]*['"]\s*,\s*)*?['"]user\.(?:name|email)['"]"""
)


def _find_violations_in_file(path: Path) -> list[str]:
    violations: list[str] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rel = path.relative_to(ROOT.parent).as_posix()

    for lineno, line in enumerate(lines, start=1):
        if ALLOW_MARKER in line:
            continue
        if CONFIG_USER_RE.search(line):
            violations.append(f"{rel}:{lineno}: {line.strip()}")
            continue

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            str_args = [
                arg.value for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if "config" in str_args:
                idx = str_args.index("config")
                if any(k in str_args[idx + 1:] for k in ("user.name", "user.email")):
                    lineno = node.lineno
                    if lineno <= len(lines) and ALLOW_MARKER in lines[lineno - 1]:
                        continue
                    v_str = f"{rel}:{lineno}: {lines[lineno - 1].strip()}"
                    if v_str not in violations:
                        violations.append(v_str)

        elif isinstance(node, (ast.List, ast.Tuple)):
            str_elts = [
                elt.value for elt in node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if "config" in str_elts:
                idx = str_elts.index("config")
                if any(k in str_elts[idx + 1:] for k in ("user.name", "user.email")):
                    lineno = node.lineno
                    if lineno <= len(lines) and ALLOW_MARKER in lines[lineno - 1]:
                        continue
                    v_str = f"{rel}:{lineno}: {lines[lineno - 1].strip()}"
                    if v_str not in violations:
                        violations.append(v_str)

    return violations


def test_no_local_git_identity_in_tests():
    self_path = Path(__file__).resolve()
    all_violations: list[str] = []

    for py_file in sorted(ROOT.rglob("*.py")):
        if py_file.resolve() == self_path:
            continue
        violations = _find_violations_in_file(py_file)
        all_violations.extend(violations)

    assert not all_violations, (
        "Forbidden git config user.name/user.email calls found in tests/:\n"
        + "\n".join(all_violations)
    )


def test_allow_marker_restricted_to_prepush_identity():
    self_path = Path(__file__).resolve()
    found_files: dict[str, list[int]] = {}

    for py_file in sorted(ROOT.rglob("*.py")):
        if py_file.resolve() == self_path:
            continue
        text = py_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            if ALLOW_MARKER in line:
                rel = py_file.relative_to(ROOT.parent).as_posix()
                found_files.setdefault(rel, []).append(lineno)

    allowed_rel = ALLOWED_FILE.relative_to(ROOT.parent).as_posix()
    assert allowed_rel in found_files and len(found_files[allowed_rel]) >= 1, (
        f"Allow marker '{ALLOW_MARKER}' must be present in {allowed_rel}"
    )
    for file_path in found_files:
        assert file_path == allowed_rel, (
            f"Allow marker '{ALLOW_MARKER}' is only permitted in {allowed_rel}, "
            f"but found in {file_path}: lines {found_files[file_path]}"
        )
