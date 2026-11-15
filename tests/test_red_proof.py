import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("red_proof", Path(__file__).parents[1] / "tools/red_proof.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # A pre-push hook exports GIT_DIR; without this the fixture's init/config
    # would target the enclosing real repository (D-5825).
    for key in [k for k in os.environ if k.startswith("GIT_") and k != "GIT_CONFIG_GLOBAL"]:
        monkeypatch.delenv(key)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.STDOUT).decode().strip()
    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (tmp_path / "app.py").write_text("def value():\n    return 0\n")
    (tmp_path / "test_app.py").write_text("def test_value():\n    pass\n")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")

    def run(test, app="raise RuntimeError('HEAD MUST NOT IMPORT')\n", message="head", base_ref=None):
        (tmp_path / "test_app.py").write_text(test)
        (tmp_path / "app.py").write_text(app)
        git("add", ".")
        git("commit", "-m", message)
        return proof.main(["--repo", str(tmp_path), "--base", base_ref or base])
    return run


def test_assertion_red_and_head_unimportable(repo, capsys):
    assert repo("from app import value\ndef test_value():\n    assert value() == 1\n") == 0
    assert "red(assertion): test_app.py::test_value" in capsys.readouterr().out


@pytest.mark.parametrize("extra", ["", "def test_red():\n    assert False\n"])
def test_pass_named_even_beside_red(repo, capsys, extra):
    assert repo("def test_value():\n    assert True\n" + extra) == 1
    assert "not red (pass/error/missing): test_app.py::test_value" in capsys.readouterr().out


@pytest.mark.parametrize("text,label", [
    ("import missing_red_proof_module\ndef test_value():\n    assert False\n", "red(collection)"),
    ("def test_value():\n    raise RuntimeError('not assertion')\n", "red(error)"),
    ("import pytest\n\x40pytest.fixture\ndef broken():\n    assert False\ndef test_value(broken):\n    assert False\n", "red(error)"),
])
def test_errors_not_proof(repo, capsys, text, label):
    assert repo(text) == 1
    assert label in capsys.readouterr().out


def test_parametrized_counts_as_one(repo, capsys):
    assert repo("import pytest\n\x40pytest.mark.parametrize('n', [0, 1])\ndef test_value(n):\n    assert n == 0\n") == 0
    assert capsys.readouterr().out.count("red(assertion):") == 1


@pytest.mark.parametrize("statement,code", [("assert new_name() == 1", 0), ("new_name()", 1)])
def test_new_name_stub(repo, statement, code):
    assert repo("from app import new_name\ndef test_value():\n    " + statement + "\n",
                app="raise RuntimeError('head')\ndef new_name():\n    return 1\n") == code


@pytest.mark.parametrize("reason,code", [("D-123 deliberate exception", 0), ("no decision", 1)])
def test_exemption(repo, capsys, reason, code):
    assert repo("def test_value():\n    assert True\n", message="head\n\nRed-Exempt: " + reason) == code
    assert reason in capsys.readouterr().out


def test_na(repo, capsys):
    assert repo("def test_value():\n    pass\n") == 0
    assert "n/a" in capsys.readouterr().out


def test_broken_base(repo):
    assert repo("def test_value():\n    assert False\n", base_ref="missing-base") == 2


@pytest.mark.parametrize("module,body,statement,code", [
    ("new_module", "", "assert new_module is not None", 1),
    ("new_module", "ANSWER = 42\n", "assert new_module.ANSWER == 42", 0),
    ("app", "ANSWER = 42\n", "assert app.ANSWER == 42", 0),
    ("app", "A, B = (1, 2)\nC: int = 3\n", "assert app.B == 2 and app.C == 3", 0),
    ("app", "from nonexistent_head_module import *\n", "assert app.value() == 1", 0),
    ("new_module", "from nonexistent_head_module import *\n", "assert new_module is None", 0),
])
def test_assignment_and_empty_module_stubs(repo, tmp_path, module, body, statement, code):
    if module == "new_module":
        (tmp_path / "new_module.py").write_text(body)
        app = "raise RuntimeError('head')\n"
    else:
        app = "raise RuntimeError('head')\n" + body
    assert repo(f"import {module}\ndef test_value():\n    {statement}\n", app=app) == code


def test_nested_pytest_rootdir(repo, tmp_path, monkeypatch, capsys):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "pytest.ini").write_text("[pytest]\n")
    (nested / "test_nested.py").write_text("def test_red():\n    assert False\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--rootdir=nested")
    assert repo("from app import value\ndef test_value():\n    assert value() == 1\n") == 0
    output = capsys.readouterr().out
    assert "red(assertion): test_app.py::test_value" in output
    assert "red(assertion): nested/test_nested.py::test_red" in output


def test_option_like_directory_is_not_pytest_option(repo, tmp_path, capsys):
    directory = tmp_path / "--rootdir=x"
    directory.mkdir()
    (directory / "test_option.py").write_text(
        "from app import value\ndef test_option():\n    assert value() == 1\n")
    assert repo("from app import value\ndef test_value():\n    assert value() == 1\n") == 0
    assert "red(assertion): --rootdir=x/test_option.py::test_option" in capsys.readouterr().out


@pytest.mark.parametrize("file", ["test_app.py", "app.py"])
def test_head_syntax_is_collection(repo, capsys, file):
    text = "from app import value\ndef test_value():\n    assert value() == 1\n"
    assert repo("def broken(:" if file == "test_app.py" else text,
                app="def broken(:" if file == "app.py" else "") == 1
    assert "red(collection)" in capsys.readouterr().out


def test_old_exemption_does_not_exempt_head(repo, tmp_path, capsys):
    base = proof.git(tmp_path, "rev-parse", "HEAD").decode().strip()
    assert repo("def test_value():\n    assert True\n", message="old\n\nRed-Exempt: D-123 old reason") == 0
    capsys.readouterr()
    assert repo("def test_value():\n    assert 1 == 1\n", base_ref=base) == 1
    assert proof.git(tmp_path, "rev-list", "--count", f"{base}..HEAD").strip() == b"2"
    assert b"Red-Exempt: D-123" in proof.git(tmp_path, "show", "-s", "--format=%B", "HEAD~1")
    output = capsys.readouterr().out
    assert "Red-Exempt:" not in output
    assert "not red (pass/error/missing): test_app.py::test_value" in output


@pytest.mark.parametrize("marker,assertion", [("skip", "False"), ("xfail", "False"),
                                              ("xfail(reason='')", "n == 1")])
def test_neutral_parameter_beside_assertion(repo, marker, assertion):
    text = ("import pytest\n\x40pytest.mark.parametrize('n', [0, pytest.param(1, marks=pytest.mark."
            + marker + ")])\ndef test_value(n):\n    assert " + assertion + "\n")
    assert repo(text) == 0


@pytest.mark.parametrize("marker,assertion", [("skip", "False"), ("xfail", "False"),
                                              ("xfail", "True"), ("xfail(reason='')", "True")])
def test_only_neutral_is_labelled(repo, capsys, marker, assertion):
    assert repo("import pytest\n\x40pytest.mark." + marker + "\ndef test_value():\n    assert " + assertion + "\n") == 1
    assert "neutral(skip/xfail)" in capsys.readouterr().out


def test_existing_assignment_not_overwritten(repo, tmp_path):
    # Existing function must likewise retain the base implementation.
    assert repo("from app import value\ndef test_value():\n    assert value() == 1\n",
                app="value = 1\n") == 0
    assert proof.stubs("A = 7\n", "A = 8\nB = 9\n") == "B = None\n"


def test_import_names_are_inert(repo):
    assert repo("from app import alias\ndef test_value():\n    assert alias == 1\n",
                app="from nonexistent_head_module import value as alias\n") == 0


def test_empty_exemption_does_not_consume_next_line(repo, capsys):
    assert repo("def test_value():\n    assert True\n",
                message="head\n\nRed-Exempt:\nD-123 unrelated line") == 1
    assert "requires a D-id" in capsys.readouterr().out


def test_parameter_id_with_brackets(repo):
    assert repo("import pytest\n\x40pytest.mark.parametrize('n', [0], ids=['a[b]'])\n"
                "def test_value(n):\n    assert False\n") == 0


@pytest.mark.parametrize("assertion,code", [("False", 0), ("True", 1)])
def test_renamed_test_file(repo, tmp_path, capsys, assertion, code):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args]).decode().strip()

    # Keep contents identical across the rename so Git reports R100, not A/D.
    original = tmp_path / "test_app.py"
    original.write_text(f"def test_value():\n    assert {assertion}\n")
    git("add", ".")
    git("commit", "-m", "prepare rename base")
    base = git("rev-parse", "HEAD")
    original.rename(tmp_path / "test_renamed.py")
    git("add", ".")
    git("commit", "-m", "rename test")
    assert git("diff", "-M", "--name-status", base, "HEAD").startswith("R100")
    assert proof.source(tmp_path, base, "test_renamed.py") == ""
    assert proof.main(["--repo", str(tmp_path), "--base", base]) == code
    output = capsys.readouterr().out
    assert "test_renamed.py::test_value" in output
    assert "n/a" not in output


@pytest.fixture
def enclosing(tmp_path_factory, monkeypatch):
    git_dir = tmp_path_factory.mktemp("enclosing") / "real.git"
    subprocess.check_call(["git", "init", "-q", "--bare", str(git_dir)])
    monkeypatch.setenv("GIT_DIR", str(git_dir))
    return git_dir


def test_fixture_identity_never_reaches_enclosing_repo(enclosing, repo):
    probe = subprocess.run(["git", "config", "--file", str(enclosing / "config"), "--get", "user.name"],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "" and probe.returncode == 1
