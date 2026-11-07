import importlib.util
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("red_proof", Path(__file__).parents[1] / "tools/red_proof.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


@pytest.fixture
def repo(tmp_path):
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
