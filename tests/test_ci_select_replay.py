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
                      "changed": {"python": ["tests/test_stable.py"], "rust": [], "browser": [], "files": ["tests/test_stable.py"]}, "environment": "default"}


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


def test_a_stable_parameter_id_with_a_dash_keeps_its_full_node_id(tmp_path, monkeypatch):
    dashed = """import pytest


@pytest.mark.parametrize('x', ['a - b', 'c'])
def test_p(x):
    pass
"""
    root, base, head = tree(tmp_path, {"tests/test_dash.py": dashed}, BASE)
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    result = plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs", workers=False, head=head)
    assert result["ok"] is True and result["incomplete"] == [], result["incomplete"]


def test_a_pass_with_a_failing_teardown_is_one_failed_outcome_per_run_never_flaky(tmp_path, monkeypatch):
    teardown = """import pytest


@pytest.fixture
def boom():
    yield
    raise RuntimeError('teardown')


def test_t(boom):
    pass
"""
    root, base, head = tree(tmp_path, {"tests/test_td.py": teardown}, BASE)
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    result = plan.replay(root, plan.changed_tests(root, base, head), tmp_path / "logs", workers=False, head=head)
    assert result["flaky"] == [] and result["failed"] == ["tests/test_td.py::test_t"] and result["ok"] is False


def test_the_last_rust_run_is_the_whole_crate_with_its_peers_and_default_threads():
    commands = plan.replay_commands({"python": [], "rust": [["rust/x/Cargo.toml", "a"], ["rust/x/Cargo.toml", "b"], ["rust/y/Cargo.toml", "c"]]})
    cargo = [(c, w) for c, w in commands if c[0] == "cargo"]
    assert len(cargo) == 4 * 3 + 2   # four filtered serial runs per test, one whole-crate run per manifest
    whole = [(c, w) for c, w in cargo if "--" not in c]
    assert [(c[3], w) for c, w in whole] == [("rust/x/Cargo.toml", ["a", "b"]), ("rust/y/Cargo.toml", ["c"])]
    assert all("--test-threads=1" in c for c, _ in cargo if "--" in c)


def test_a_runner_error_or_interrupted_run_is_a_failure_even_when_it_printed_passes(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    for code in (2, 3, 4, 5):
        runs = [([sys.executable, "-c", f"print('PASSED tests/test_a.py::t'); raise SystemExit({code})"], []) for _ in range(5)]
        monkeypatch.setattr(plan, "replay_commands", lambda *_a, runs=runs, **_kw: runs)
        result = plan.replay(root, {}, tmp_path / f"logs{code}", head=head)
        assert result["ok"] is False and len(result["failed"]) == 5 and f"exit {code}" in result["failed"][0], code
    # exit 1 with a parsed failure is an ordinary failure; exit 0 and cargo's 101 with failures stay as before
    runs = [([sys.executable, "-c", "print('PASSED tests/test_a.py::t')"], []) for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: runs)
    assert plan.replay(root, {}, tmp_path / "logs-ok", head=head)["ok"] is True


def test_a_stable_test_with_a_platform_skip_is_not_incomplete_but_a_missing_one_still_is(tmp_path, monkeypatch):
    skipping = """import pytest


def test_here():
    pass


@pytest.mark.skip(reason='another platform')
def test_elsewhere():
    pass


def test_never_printed():
    pass
"""
    root, base, head = tree(tmp_path, {"tests/test_skip.py": skipping}, BASE)
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    changed = plan.changed_tests(root, base, head)
    result = plan.replay(root, changed, tmp_path / "logs", workers=False, head=head)
    assert result["ok"] is True and result["incomplete"] == [] and result["failed"] == []
    # the full-worker run prints xdist's "[gw0] [ 66%] SKIPPED <id>" form
    workers = plan.replay(root, changed, tmp_path / "logs-workers", workers=True, head=head)
    assert workers["ok"] is True and workers["incomplete"] == [], workers["incomplete"]
    out = [([sys.executable, "-c", "print('PASSED tests/test_skip.py::test_here');print('tests/test_skip.py::test_elsewhere SKIPPED (another platform) [ 66%]')"], [])
           for _ in range(5)]
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: out)
    partial = plan.replay(root, changed, tmp_path / "logs2", head=head)
    assert {i.split(" (run")[0] for i in partial["incomplete"]} == {"tests/test_skip.py::test_never_printed"}


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
    assert len(cargo) == 5 and sum("--test-threads=1" in c for c in cargo) == 4 and "--" not in cargo[-1]


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
    return {"head": HEAD, "ok": True, "runs": 5, "flaky": [], "failed": [], "logs": {},
            "changed": {"python": ["tests/test_a.py"], "rust": [], "files": ["tests/test_a.py"]}} | over


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


def test_a_receipt_that_omits_a_changed_rust_test_of_the_same_file_list_does_not_make_the_part_ready():
    """Both diffs list lib.rs, but the part changes two tests and the replay covered one of them."""
    files = ["rust/pg-spawnd/src/lib.rs"]
    one = receipt(changed={"python": [], "rust": [["rust/pg-spawnd/Cargo.toml", "kept"]], "files": files})
    two = {"python": [], "rust": [["rust/pg-spawnd/Cargo.toml", "kept"], ["rust/pg-spawnd/Cargo.toml", "other"]]}
    problems = ci_select.ready_problems(facts(one, files=files) | {"expected": two})
    assert any("another comparison base" in p for p in problems)
    full = receipt(changed={**two, "files": files})
    assert ci_select.ready_problems(facts(full, files=files) | {"expected": two}) == []


def test_a_part_that_changes_no_test_needs_no_replay_receipt():
    assert ci_select.ready_problems(facts(None, files=("src/plangraph/planner/provider_stop.py", "docs/x.md"))) == []
    assert any("replay" in p for p in ci_select.ready_problems(facts(None, files=("rust/pg-spawnd/src/lib.rs",))))


def test_a_receipt_made_against_another_comparison_base_does_not_make_the_part_ready():
    """A replay of a different diff (here: zero changed tests, an empty file list) is not this change's replay."""
    wrong = receipt(changed={"python": [], "rust": [], "files": []})
    assert any("another comparison base" in p for p in ci_select.ready_problems(facts(wrong)))
    other = receipt(changed={"python": ["tests/test_a.py"], "rust": [], "files": ["tests/test_a.py", "src/x.py"]})
    assert any("another comparison base" in p for p in ci_select.ready_problems(facts(other)))
    assert ci_select.ready_problems(facts(receipt())) == []


def test_entry_set_run_writes_the_receipt_where_readiness_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TOOLS.parent / "src"))
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    root, base, head = tree(tmp_path, {"tests/test_stable.py": STABLE}, BASE)
    assert ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run"]) == 0
    found = plan.replay_receipt(head)
    assert found["head"] == head and found["ok"] is True and found["changed"]["python"] == ["tests/test_stable.py"]
    assert plan.replay_receipt("0" * 40) is None


# --- skips across the five runs, environments and stacked parts (the slice after the eighth round) ---------------------

def stub_runs(lines, wanted=()):
    return [([sys.executable, "-c", f"print({line!r})"], list(wanted)) for line in lines]


def test_a_changed_test_that_passed_four_times_and_skipped_once_is_flaky_with_both_logs(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    test_id = "tests/test_a.py::t"
    runs = stub_runs([f"PASSED {test_id}"] * 4 + [f"{test_id} SKIPPED (platform) [100%]"], [test_id])
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: runs)
    result = plan.replay(root, {}, tmp_path / "logs", head=head)
    assert result["ok"] is False and result["flaky"] == [test_id] and result["failed"] == [] and result["incomplete"] == []
    skipped_log, passing_log = result["logs"][test_id]
    assert "SKIPPED" in Path(skipped_log).read_text() and "PASSED" in Path(passing_log).read_text()


def test_a_test_skipped_on_every_run_is_consistently_inapplicable_and_a_fail_and_skip_mix_is_flaky(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    test_id = "tests/test_a.py::t"
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: stub_runs([f"{test_id} SKIPPED (x) [100%]"] * 5, [test_id]))
    result = plan.replay(root, {}, tmp_path / "logs", head=head)
    assert result["ok"] is True and result["flaky"] == [] and result["failed"] == [] and result["incomplete"] == []
    mixed = stub_runs([f"FAILED {test_id} - boom"] * 4 + [f"{test_id} SKIPPED (x) [100%]"], [test_id])
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: mixed)
    result = plan.replay(root, {}, tmp_path / "logs2", head=head)
    assert result["flaky"] == [test_id] and result["ok"] is False


def test_browser_tests_are_selected_separately_from_the_default_python_tests(tmp_path):
    root, base, head = tree(tmp_path, {"tests/test_new.py": STABLE, "tests/browser/test_ui.py": STABLE}, BASE)
    changed = plan.changed_tests(root, base, head)
    assert changed["python"] == ["tests/test_new.py"] and changed["browser"] == ["tests/browser/test_ui.py"]


def test_the_environments_a_change_needs_follow_its_tests(tmp_path):
    pg = "def test_pg():\n    pass\n"
    keeper = "from plangraph import keeper_util\n\n\ndef test_k():\n    pass\n"
    sqlite_only = "import sqlite3\nfrom plangraph import keeper_util\n\n\ndef test_s():\n    pass\n"
    root, base, head = tree(tmp_path, {"tests/contract/test_c.py": pg, "tests/test_keeper_x.py": keeper, "tests/test_sql.py": sqlite_only,
                                       "tests/browser/test_ui.py": STABLE,
                                       "rust/pg-spawnd/src/lib.rs": "#[cfg(test)]\nmod t {\n    #[test]\n    fn added() {}\n}\n"}, BASE)
    changed = plan.changed_tests(root, base, head)
    assert plan.postgres_files(root, head, changed["python"]) == ["tests/contract/test_c.py", "tests/test_keeper_x.py"]
    assert plan.applicable_environments(root, head, changed) == ["default", "postgres", "browser", "windows"]
    (tmp_path / "two").mkdir()
    root2, base2, head2 = tree(tmp_path / "two", {"tests/test_new.py": STABLE}, BASE)
    assert plan.applicable_environments(root2, head2, plan.changed_tests(root2, base2, head2)) == ["default"]


def test_the_postgres_replay_needs_a_server_and_runs_with_the_postgres_backend(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    changed = {"python": ["tests/contract/test_c.py"], "rust": [], "browser": [], "files": []}
    monkeypatch.delenv("PG_DSN", raising=False)
    with pytest.raises(plan.MergeBatchError, match="PG_DSN"):
        plan.replay(root, changed, tmp_path / "none", head=head, environment="postgres")
    monkeypatch.setenv("PG_DSN", "postgresql://example.invalid/keeper")
    probe = "import os;print('PASSED tests/contract/test_c.py::t' if os.environ.get('PG_KEEPER_BACKEND') == 'postgres' and os.environ.get('PG_DSN') else 'FAILED tests/contract/test_c.py::t')"
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: [([sys.executable, "-c", probe], []) for _ in range(5)])
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    result = plan.replay(root, changed, plan.replay_dir(head), head=head, environment="postgres")
    assert result["ok"] is True and result["environment"] == "postgres"
    assert plan.replay_receipt(head, "postgres")["ok"] is True and plan.replay_receipt(head) is None


def test_the_windows_replay_only_runs_on_a_windows_host(tmp_path, monkeypatch):
    root, _, head = tree(tmp_path, {"docs/x.md": "x\n"}, BASE)
    changed = {"python": [], "rust": [["rust/pg-spawnd/Cargo.toml", "case"]], "browser": [], "files": []}
    monkeypatch.setattr(plan.sys, "platform", "linux")
    with pytest.raises(plan.MergeBatchError, match="Windows host"):
        plan.replay(root, changed, tmp_path / "none", head=head, environment="windows")
    monkeypatch.setattr(plan.sys, "platform", "win32")
    monkeypatch.setattr(plan, "replay_commands", lambda *_a, **_kw: stub_runs(["test mod::case ... ok"] * 5, ["case"]))
    assert plan.replay(root, changed, tmp_path / "win", head=head, environment="windows")["ok"] is True


def test_every_environment_the_change_needs_has_a_passing_receipt_for_this_head_at_ready():
    base = facts(receipt(), files=("tests/test_a.py",)) | {"environments": ["postgres", "windows"], "replays": {}}
    problems = ci_select.ready_problems(base)
    assert any("postgres environment" in p for p in problems) and any("windows environment" in p for p in problems)
    ok = {"ok": True, "head": HEAD, "runs": 5, "flaky": [], "logs": {}}
    assert ci_select.ready_problems(base | {"replays": {"postgres": ok, "windows": ok}}) == []
    mixed = ok | {"ok": False, "flaky": ["tests/contract/test_c.py::t"], "logs": {"tests/contract/test_c.py::t": ["f.log", "p.log"]}}
    problems = ci_select.ready_problems(base | {"replays": {"postgres": mixed, "windows": ok}})
    assert "FLAKY (postgres): tests/contract/test_c.py::t (logs f.log and p.log)" in problems
    assert any("postgres environment" in p for p in ci_select.ready_problems(base | {"replays": {"postgres": ok | {"head": "8" * 40}, "windows": ok}}))


def test_entry_set_replays_one_environment_and_refuses_one_the_host_does_not_have(tmp_path, monkeypatch, capfd):
    root, base, head = tree(tmp_path, {"tests/test_stable.py": STABLE}, BASE)
    monkeypatch.setenv("PLANGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.setattr(plan.sys, "platform", "linux")
    code = ci_select.main(["entry-set", "--root", str(root), "--base", base, "--head", head, "--run", "--environment", "windows"])
    assert code == 1 and "Windows host" in capfd.readouterr().err
    assert plan.replay_receipt(head, "windows") is None
