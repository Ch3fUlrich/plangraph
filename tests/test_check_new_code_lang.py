"""Exercise the language gate against isolated, committed git histories."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "tools/check_new_code_lang.py"


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
        path.write_text(text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "change")
    return subprocess.run([sys.executable, str(CHECKER)], cwd=root,
                          env={**os.environ, "NEWCODE_BASE": base},
                          capture_output=True, text=True)


@pytest.mark.parametrize("suffix", ["py", "sh", "js", "ts", "mjs", "cjs", "tsx"])
def test_added_source_fails(repo, suffix):
    result = check(repo, {f"new.{suffix}": "new code\n"})
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"FAIL new.{suffix}: new code must be Rust (D-4691)"]


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
    from tools.ci_select import ALWAYS, gate

    needs = {name: {"result": "success"} for name in ALWAYS}
    needs["select"] = {"result": "success", "outputs": {}}
    needs["new-code-lang"] = {"result": result}
    assert f"new-code-lang: {result}" in gate(needs)
