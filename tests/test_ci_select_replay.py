"""LE-CI-FLAKE-ENTRY-REPLAY: a part's changed tests run five times on its head; an inconsistent result is FLAKY.

Mutations killed: a changed test that is never replayed, a one-in-five failure that stays READY, a stable
test reported flaky, FLAKY without both logs, a test that always fails read as flaky instead of failed, Rust
tests that are not replayed, and a replay that runs fewer than five times or never uses the full-worker run.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_ci_select_entry import BASE, tree

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("ci_select_replay", TOOLS / "ci_select.py")
ci_select = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_select)

STABLE = "def test_stable():\n    assert True\n"
# Fails on the third run of five: the counter file lives next to the test.
ONE_IN_FIVE = (
    "from pathlib import Path\n\n\ndef test_one_in_five():\n    counter = Path(__file__).with_name('count.txt')\n"
    "    n = int(counter.read_text()) + 1 if counter.exists() else 1\n    counter.write_text(str(n))\n    assert n != 3\n"
)


def test_the_changed_python_test_files_and_new_rust_tests_are_selected(tmp_path):
    root, base, head = tree(tmp_path, {
        "tests/test_new.py": STABLE, "tests/browser/test_ui.py": STABLE, "docs/x.md": "x\n",
        "rust/pg-spawnd/src/lib.rs": "#[cfg(test)]\nmod t {\n    #[test]\n    fn added_case() {}\n    fn helper() {}\n}\n",
    }, BASE)
    changed = ci_select.changed_tests(root, base, head)
    assert changed["python"] == ["tests/test_new.py"]
    assert changed["rust"] == [["rust/pg-spawnd/Cargo.toml", "added_case"]]


def test_a_predictable_one_in_five_failure_is_flaky_and_keeps_both_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_flip.py": ONE_IN_FIVE}, BASE)
    out = tmp_path / "logs"
    result = ci_select.replay(root, ci_select.changed_tests(root, base, head), out, workers=False)
    assert result["flaky"] == ["tests/test_flip.py::test_one_in_five"] and result["ok"] is False
    failing, passing = result["logs"]["tests/test_flip.py::test_one_in_five"]
    assert "FAILED" in Path(failing).read_text() and "PASSED" in Path(passing).read_text()
    assert len(list(out.glob("python-run-*.log"))) == 5


def test_a_stable_passing_test_clears_the_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_stable.py": STABLE}, BASE)
    result = ci_select.replay(root, ci_select.changed_tests(root, base, head), tmp_path / "logs", workers=False)
    assert result == {"flaky": [], "failed": [], "logs": {}, "ok": True, "runs": 5}


def test_a_test_that_always_fails_is_a_failure_not_a_flake(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_bad.py": "def test_bad():\n    assert 0\n"}, BASE)
    result = ci_select.replay(root, ci_select.changed_tests(root, base, head), tmp_path / "logs", workers=False)
    assert result["flaky"] == [] and result["failed"] == ["tests/test_bad.py::test_bad"] and result["ok"] is False


@pytest.mark.parametrize("lines,flaky,failed", [
    (["PASSED a::t"] * 5, [], []),
    (["PASSED a::t"] * 4 + ["FAILED a::t - boom"], ["a::t"], []),
    (["test x::y ... ok"] * 4 + ["test x::y ... FAILED"], ["x::y"], []),
    (["FAILED a::t"] * 5, [], ["a::t"]),
])
def test_flaky_means_both_a_pass_and_a_failure_in_the_runs(tmp_path, monkeypatch, lines, flaky, failed):
    commands = [[sys.executable, "-c", f"print({line!r})"] for line in lines]
    monkeypatch.setattr(ci_select, "replay_commands", lambda *_a, **_kw: commands)
    result = ci_select.replay(tmp_path, {}, tmp_path / "logs")
    assert (result["flaky"], result["failed"]) == (flaky, failed)
    assert len(list((tmp_path / "logs").glob("*-run-*.log"))) == 5


def test_the_replay_command_runs_serial_four_times_then_once_with_every_worker(tmp_path):
    commands = ci_select.replay_commands({"python": ["tests/test_a.py"], "rust": [["rust/x/Cargo.toml", "case"]]},
                                         workers=True)
    python = [c for c in commands if c[0] != "cargo"]
    assert len(python) == 5 and sum("-n" in c and "auto" in c for c in python) == 1
    cargo = [c for c in commands if c[0] == "cargo"]
    assert len(cargo) == 5 and sum("--test-threads=1" in c for c in cargo) == 4


def test_entry_set_run_blocks_with_flaky_when_the_replay_finds_one(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_flip.py": ONE_IN_FIVE}, BASE)
    code = ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run",
                           "--replay-dir", str(tmp_path / "logs")])
    err = capfd.readouterr().err
    assert code == 1 and "entry-set: FLAKY: tests/test_flip.py::test_one_in_five" in err
    assert json.loads((tmp_path / "logs" / "replay.json").read_text())["flaky"] == ["tests/test_flip.py::test_one_in_five"]
