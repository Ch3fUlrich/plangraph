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
sys.path.insert(0, str(TOOLS))
import merge_batch_plan as plan  # noqa: E402  (the replay lives with the batch planning)

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
    changed = plan.changed_tests(root, base, head)
    assert changed["python"] == ["tests/test_new.py"]
    assert changed["rust"] == [["rust/pg-spawnd/Cargo.toml", "added_case"]]


RUST_TEST = "#[cfg(test)]\nmod t {\n    #[test]\n    fn kept() {\n        let n = 1;\n        assert_eq!(n, 1);\n    }\n\n    #[test]\n    fn other() {\n        assert!(true);\n    }\n}\n"


def test_a_body_only_edit_of_an_existing_rust_test_selects_that_test_and_no_other(tmp_path):
    base = {**BASE, "rust/pg-spawnd/src/lib.rs": RUST_TEST}
    root, b, head = tree(tmp_path, {"rust/pg-spawnd/src/lib.rs": RUST_TEST.replace("let n = 1;", "let n = 2;").replace("assert_eq!(n, 1)", "assert_eq!(n, 2)")}, base)
    assert plan.changed_tests(root, b, head)["rust"] == [["rust/pg-spawnd/Cargo.toml", "kept"]]


def test_a_deleted_line_and_an_edit_outside_any_test_select_the_enclosing_test_only(tmp_path):
    base = {**BASE, "rust/pg-spawnd/src/lib.rs": RUST_TEST}
    root, b, head = tree(tmp_path, {"rust/pg-spawnd/src/lib.rs": RUST_TEST.replace("        let n = 1;\n", "").replace("mod t {", "mod t {\n    const X: u8 = 1;")}, base)
    assert plan.changed_tests(root, b, head)["rust"] == [["rust/pg-spawnd/Cargo.toml", "kept"]]


def test_a_predictable_one_in_five_failure_is_flaky_and_keeps_both_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_flip.py": ONE_IN_FIVE}, BASE)
    out = tmp_path / "logs"
    result = plan.replay(root, plan.changed_tests(root, base, head), out, workers=False)
    assert result["flaky"] == ["tests/test_flip.py::test_one_in_five"] and result["ok"] is False
    failing, passing = result["logs"]["tests/test_flip.py::test_one_in_five"]
    assert "FAILED" in Path(failing).read_text() and "PASSED" in Path(passing).read_text()
    assert len(list(out.glob("python-run-*.log"))) == 5


def test_a_stable_passing_test_clears_the_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_stable.py": STABLE}, BASE)
    result = plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs", workers=False, head=head)
    assert result == {"flaky": [], "failed": [], "incomplete": [], "logs": {}, "ok": True, "runs": 5, "head": head,
                      "changed": {"python": ["tests/test_stable.py"], "rust": []}}


def test_a_test_that_always_fails_is_a_failure_not_a_flake(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_bad.py": "def test_bad():\n    assert 0\n"}, BASE)
    result = plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs", workers=False)
    assert result["flaky"] == [] and result["failed"] == ["tests/test_bad.py::test_bad"] and result["ok"] is False


@pytest.mark.parametrize("lines,flaky,failed", [
    (["PASSED a::t"] * 5, [], []),
    (["PASSED a::t"] * 4 + ["FAILED a::t - boom"], ["a::t"], []),
    (["test x::y ... ok"] * 4 + ["test x::y ... FAILED"], ["x::y"], []),
    (["FAILED a::t"] * 5, [], ["a::t"]),
])
def test_flaky_means_both_a_pass_and_a_failure_in_the_runs(tmp_path, monkeypatch, lines, flaky, failed):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    commands = [([sys.executable, "-c", f"print({line!r})"], []) for line in lines]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: commands)
    result = plan.replay(root, {}, tmp_path / "logs", head=head)
    assert (result["flaky"], result["failed"]) == (flaky, failed)
    assert len(list((tmp_path / "logs").glob("*-run-*.log"))) == 5


def test_five_invocations_that_never_ran_the_changed_test_are_not_a_passing_receipt(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    skipped = [([sys.executable, "-c", "print('PASSED tests/test_other.py::t')"], ["tests/test_a.py::"]) for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: skipped)
    result = plan.replay(root, {}, tmp_path / "logs", head=head)
    assert result["ok"] is False and result["failed"] == [] and result["flaky"] == []
    assert result["incomplete"] == [f"tests/test_a.py:: (run {n})" for n in range(5)]
    rust = [([sys.executable, "-c", "print('test mod::kept ... ok')"], ["kept"]) for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: rust)
    assert plan.replay(root, {}, tmp_path / "logs2", head=head)["ok"] is True   # the name is the last path segment


def test_a_passing_sibling_cannot_stand_in_for_a_changed_test_that_never_reported(tmp_path, monkeypatch):
    """Every collected test id of a changed file needs an outcome on every run, not only one id of the file."""
    two = """import pytest


def test_a():
    pass


def test_b():
    pass


@pytest.mark.parametrize('x', ['p q', 'r'])
def test_c(x):
    pass
"""
    root, base, head = tree(tmp_path, {"tests/test_two.py": two}, BASE)
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    only_a = [([sys.executable, "-c", "print('PASSED tests/test_two.py::test_a')"], ["tests/test_two.py::"]) for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: only_a)
    result = plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs", head=head)
    assert result["ok"] is False
    assert {i.split(" (run")[0] for i in result["incomplete"]} == {"tests/test_two.py::test_b", "tests/test_two.py::test_c[p q]", "tests/test_two.py::test_c[r]"}
    assert len(result["incomplete"]) == 15   # three ids on each of five runs
    everything = [([sys.executable, "-c", "print('PASSED tests/test_two.py::test_a');print('PASSED tests/test_two.py::test_b');"
                    "print('PASSED tests/test_two.py::test_c[p q]');print('PASSED tests/test_two.py::test_c[r]')"], []) for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: everything)
    assert plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs2", head=head)["ok"] is True


def test_the_replay_runs_the_requested_head_in_a_clean_checkout_not_the_working_tree(tmp_path, monkeypatch):
    """A passing working tree and a failing requested commit: the receipt follows the commit, never the checkout."""
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, bad = tree(tmp_path, {"tests/test_flip.py": "def test_flip():\n    assert 0\n"}, BASE)
    (root / "tests/test_flip.py").write_text(STABLE.replace("test_stable", "test_flip"))   # uncommitted fix
    changed = plan.changed_tests(root, base, bad)
    result = plan.replay(root, changed, tmp_path / "logs", workers=False, head=bad)
    assert result["ok"] is False and result["failed"] == ["tests/test_flip.py::test_flip"] and result["head"] == bad
    assert subprocess_out(root, "worktree", "list").count("\n") == 0   # the temporary checkout is gone


def subprocess_out(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_the_replay_command_runs_serial_four_times_then_once_with_every_worker(tmp_path):
    commands = plan.replay_commands({"python": ["tests/test_a.py"], "rust": [["rust/x/Cargo.toml", "case"]]},
                                         workers=True)
    python = [c for c, _ in commands if c[0] != "cargo"]
    assert len(python) == 5 and sum("-n" in c and "auto" in c for c in python) == 1
    assert [wanted for _, wanted in commands][0] == ["tests/test_a.py::"] and [w for _, w in commands][-1] == ["case"]
    cargo = [c for c, _ in commands if c[0] == "cargo"]
    assert len(cargo) == 5 and sum("--test-threads=1" in c for c in cargo) == 4


def test_entry_set_run_blocks_with_flaky_when_the_replay_finds_one(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    root, base, head = tree(tmp_path, {"tests/test_flip.py": ONE_IN_FIVE}, BASE)
    code = ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run",
                           "--replay-dir", str(tmp_path / "logs")])
    err = capfd.readouterr().err
    assert code == 1 and "entry-set: FLAKY: tests/test_flip.py::test_one_in_five" in err
    assert json.loads((tmp_path / "logs" / "replay.json").read_text())["flaky"] == ["tests/test_flip.py::test_one_in_five"]


# --- readiness: a part with changed tests needs a replay receipt bound to its exact head ----------------------------

HEAD = "9" * 40


def receipt(**over):
    return {"head": HEAD, "ok": True, "runs": 5, "flaky": [], "failed": [], "logs": {}, "changed": {"python": ["tests/test_a.py"], "rust": []}} | over


def facts(replay, files=("tests/test_a.py",)):
    return {"fast_checks": True, "entry_set": True, "head": HEAD, "files": list(files), "replay": replay,
            "attempts": [{"attempt": 1, "conclusion": "success"}]}


def test_changed_tests_need_a_replay_receipt_bound_to_the_part_head():
    assert ci_select.ready_problems(facts(receipt())) == []
    for replay in (None, receipt(head="8" * 40), receipt(ok=False), receipt(runs=1)):
        assert any("replay" in p for p in ci_select.ready_problems(facts(replay))), replay


def test_a_one_in_five_failure_blocks_ready_with_flaky_and_both_logs():
    flaky = receipt(ok=False, flaky=["tests/test_a.py::t"], logs={"tests/test_a.py::t": ["fail.log", "pass.log"]})
    assert "FLAKY: tests/test_a.py::t (logs fail.log and pass.log)" in ci_select.ready_problems(facts(flaky))


def test_a_part_that_changes_no_test_needs_no_replay_receipt():
    assert ci_select.ready_problems(facts(None, files=("src/plangraph/planner/provider_stop.py", "docs/x.md"))) == []
    assert any("replay" in p for p in ci_select.ready_problems(facts(None, files=("rust/pg-spawnd/src/lib.rs",))))


def test_entry_set_run_writes_the_receipt_where_readiness_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    root, base, head = tree(tmp_path, {"tests/test_stable.py": STABLE}, BASE)
    assert ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run"]) == 0
    found = plan.replay_receipt(head)
    assert found["head"] == head and found["ok"] is True and found["changed"]["python"] == ["tests/test_stable.py"]
    assert plan.replay_receipt("0" * 40) is None
