"""Exercise the language gate against isolated, committed git histories."""
import os
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "tools/check_new_code_lang.py"
spec = importlib.util.spec_from_file_location("ci_select", CHECKER.with_name("ci_select.py"))
assert spec is not None and spec.loader is not None
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Gate Test")
    git(tmp_path, "config", "user.email", "gate@example.invalid")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/new_code_allowlist.txt").write_text(
        "tests/test_*.py | Existing Python module tests\n", encoding="utf-8")
    (tmp_path / "existing.py").write_text("print('existing')\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def check(repo, files):
    root, base = repo
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "change")
    return subprocess.run([sys.executable, str(CHECKER)], cwd=root,
                          env={**os.environ, "NEWCODE_BASE": base},
                          capture_output=True, text=True)


@pytest.mark.parametrize("suffix", ["py", "sh", "js", "ts", "mjs", "cjs", "tsx",
                                    "jsx", "mts", "cts", "bash", "PY", "SH", "TS"])
def test_added_source_fails(repo, suffix):
    result = check(repo, {f"new.{suffix}": "new code\n"})
    assert result.returncode == 1
    assert result.stdout.startswith(f"FAIL new.{suffix}: new code must be Rust (D-4691)")
    assert "blocked suffixes:" in result.stdout


def test_allowlisted_file_passes(repo):
    assert check(repo, {"tests/test_existing.py": "assert True\n"}).returncode == 0


def test_existing_edit_passes(repo):
    assert check(repo, {"existing.py": "print('edited')\n"}).returncode == 0


def test_rust_crate_passes(repo):
    assert check(repo, {"Cargo.toml": '[package]\nname = "new"\n',
                        "src/main.rs": "fn main() {}\n"}).returncode == 0


@pytest.mark.parametrize("entry", ["tests/*.py", "tests/*.py | "])
def test_missing_reason_fails(repo, entry):
    result = check(repo, {"tools/new_code_allowlist.txt": entry + "\n"})
    assert result.returncode == 1
    assert "entry needs a glob | reason" in result.stdout


def test_rename_and_delete_pass(repo):
    root, _ = repo
    git(root, "mv", "existing.py", "renamed.py")
    assert check(repo, {"README.md": "Documentation\n"}).returncode == 0
    (root / "renamed.py").unlink()
    assert check(repo, {"README.md": "Updated documentation\n"}).returncode == 0


@pytest.mark.parametrize("base", ["missing-ref", "HEAD"])
def test_invalid_base_fails(repo, base):
    root, _ = repo
    result = subprocess.run([sys.executable, str(CHECKER)], cwd=root,
                            env={**os.environ, "NEWCODE_BASE": base},
                            capture_output=True, text=True)
    assert result.returncode == 1


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_ci_gate_requires_language_job(result):
    needs = {name: {"result": "success"} for name in ci_select.ALWAYS}
    needs["select"] = {"result": "success", "outputs": {}}
    needs["new-code-lang"] = {"result": result}
    assert f"new-code-lang: {result}" in ci_select.gate(needs)


@pytest.mark.parametrize("interpreter", ["/usr/bin/python3", "/bin/sh", "/bin/bash",
    "/usr/bin/node", "/usr/bin/env python", "/usr/bin/env node", "/usr/bin/env sh",
    "/usr/bin/env -S python3 -u"])
@pytest.mark.parametrize("name", ["script", "script.txt"])
def test_shebang_fails(repo, interpreter, name):
    assert check(repo, {name: f"#!{interpreter}\ncode\n"}).returncode == 1


@pytest.mark.parametrize("content", [b"\x00\xffbinary", b"plain text\n",
    b"#!/bin/sh\n\x00", b"#!/bin/sh\n\xff", b"#!/usr/bin/ruby\n"])
def test_extensionless_non_script_passes(repo, content):
    assert check(repo, {"data": content}).returncode == 0


def test_rename_text_to_python_fails(repo):
    root, _ = repo
    (root / "notes.txt").write_text("notes\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "notes base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "mv", "notes.txt", "exploit.py")
    assert check((root, base), {}).returncode == 1


def test_copy_python_fails(repo):
    root, _ = repo
    assert check(repo, {"copy.py": (root / "existing.py").read_text()}).returncode == 1


def test_rename_shebang_script_passes(repo):
    root, _ = repo
    (root / "old").write_text("#!/bin/sh\necho hello\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "script base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "mv", "old", "new.txt")
    assert check((root, base), {}).returncode == 0


def test_shebang_scan_is_bounded(repo):
    assert check(repo, {"data": "x" * 256 + "\n#!/bin/sh\n"}).returncode == 0


def test_shebang_reads_head_not_worktree(repo):
    root, base = repo
    assert check(repo, {"script": "#!/bin/sh\n"}).returncode == 1
    (root / "script").write_text("plain text\n")
    result = subprocess.run([sys.executable, str(CHECKER), "--base", base],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 1


def test_nested_test_not_allowlisted(repo):
    assert check(repo, {"tests/sub/test_x.py": "assert True\n"}).returncode == 1


@pytest.mark.parametrize("name", ["tests/test_x.py", "tests/sub/test_x.py",
                                      "tests/sub/deep/test_x.py"])
def test_recursive_allowlist(repo, name):
    assert check(repo, {"tools/new_code_allowlist.txt":
                       "tests/**/test_*.py | Recursive tests\n",
                       name: "assert True\n"}).returncode == 0


def test_missing_allowlist_fails(repo):
    root, _ = repo
    (root / "tools/new_code_allowlist.txt").unlink()
    assert check(repo, {"README.md": "docs\n"}).returncode == 1


def test_ci_gate_missing_language_job():
    needs = {name: {"result": "success"} for name in ci_select.ALWAYS
             if name != "new-code-lang"}
    needs["select"] = {"result": "success", "outputs": {}}
    assert "new-code-lang: missing" in ci_select.gate(needs)


def test_workflow_requires_language_job():
    import yaml

    workflow = yaml.safe_load((CHECKER.parents[1] / ".github/workflows/tests.yml").read_text())
    assert "new-code-lang" in workflow["jobs"]
    assert "new-code-lang" in workflow["jobs"]["ci-gate"]["needs"]


@pytest.mark.parametrize('path,expected', [('src/new_module.py', 0), ('tools/new_tool.sh', 0),
                                          ('src/new_module.js', 1),
                                          ('tests/nested/test_regression.py', 0)])
def test_project_allowlist_retains_main_operator_interim_policy(repo, path, expected):
    rules = CHECKER.with_name('new_code_allowlist.txt').read_text()
    result = check(repo, {'tools/new_code_allowlist.txt': rules, path: 'new content\n'})
    assert result.returncode == expected, result.stdout
