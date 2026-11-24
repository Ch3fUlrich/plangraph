"""Quiet loop and flake naming of tools/affected_tests.py, on real pytest in a fixture tree."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools/affected_tests.py"
HAS_XDIST = __import__("importlib").util.find_spec("xdist") is not None
needs_xdist = pytest.mark.skipif(not HAS_XDIST, reason="pytest-xdist missing")

CONFTEST = '''
import json, os
def pytest_configure(config):
    if not hasattr(config, "workerinput"):
        with open(os.environ["FIXTURE_ARGV_LOG"], "a") as log:
            log.write(json.dumps(list(config.invocation_params.args)) + "\\n")
'''
FLAKY = '''
import os
from pathlib import Path
def test_flaky():
    marker = Path(os.environ["FIXTURE_MARKER"])
    if not marker.exists():
        marker.write_text("seen")
        assert False, "first run fails"
'''


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.tree = tmp_path / "tree"
        self.tree.mkdir()
        self.marker = tmp_path / "marker"
        self.argv_log = tmp_path / "argv.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(FIXTURE_MARKER=str(self.marker), FIXTURE_ARGV_LOG=str(self.argv_log),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="T", GIT_COMMITTER_NAME="T",
                        GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.env.pop("PYTEST_ADDOPTS", None)
        self.env.pop("PYTEST_XDIST_WORKER", None)
        (self.tree / "tests").mkdir()
        (self.tree / "conftest.py").write_text(CONFTEST)
        (self.tree / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.tree, env=self.env, check=True, capture_output=True)

    def write(self, source, name="test_x.py"):
        (self.tree / "tests" / name).write_text(textwrap.dedent(source))

    def commit(self):
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def run(self, *extra, workers="0"):
        return subprocess.run(
            [sys.executable, str(TOOL), "--base", "HEAD", "--root", str(self.tree),
             "--changed", "tests/test_x.py", "--run", "-n", workers, *extra],
            cwd=self.tree, env=self.env, capture_output=True, text=True, timeout=300)

    def invocations(self):
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines()]


@pytest.fixture
def fx(tmp_path, monkeypatch):
    return Fixture(tmp_path, monkeypatch)


def test_run_is_quiet_and_loud_restores_the_default_report(fx):
    fx.write("def test_ok(): pass\n")
    fx.commit()
    quiet = fx.run()
    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    args = fx.invocations()[0]
    assert {"-q", "--tb=short", "--no-header"} <= set(args)
    assert "platform " not in quiet.stdout and "rootdir" not in quiet.stdout
    assert "affected wall seconds:" in quiet.stdout
    fx.argv_log.unlink()
    loud = fx.run("--loud")
    assert not {"-q", "--tb=short", "--no-header"} & set(fx.invocations()[0])
    assert "platform " in loud.stdout
    assert len(loud.stdout) > len(quiet.stdout)


def test_a_pass_runs_pytest_once_and_prints_no_label(fx):
    fx.write("def test_ok(): pass\n")
    fx.commit()
    result = fx.run()
    assert len(fx.invocations()) == 1
    assert "FLAKY" not in result.stdout and "FAILED-" not in result.stdout


def test_a_failure_that_passes_both_reruns_is_named_flaky_and_stays_red(fx):
    fx.write(FLAKY)
    fx.commit()
    result = fx.run()
    assert result.returncode == 1
    assert ("FLAKY (rerun -n0; a parallel race is not excluded): tests/test_x.py::test_flaky"
            in result.stdout.splitlines())
    assert len(fx.invocations()) == 3
    assert "-n" in fx.invocations()[1] and fx.invocations()[1][fx.invocations()[1].index("-n") + 1] == "0"


@needs_xdist
def test_flaky_is_classified_the_same_under_two_workers(fx):
    fx.write(FLAKY)
    fx.commit()
    result = fx.run(workers="2")
    assert result.returncode == 1
    assert "FLAKY (rerun -n0; a parallel race is not excluded): tests/test_x.py::test_flaky" in result.stdout
    reruns = fx.invocations()[1:]
    assert [args[args.index("-n") + 1] for args in reruns] == ["0", "2"]


def test_fail_then_fail_prints_no_label(fx):
    fx.write("def test_bad(): assert False\n")
    fx.commit()
    result = fx.run()
    assert result.returncode == 1
    assert "FLAKY" not in result.stdout and "FAILED-" not in result.stdout
    assert len(fx.invocations()) == 3


@needs_xdist
def test_a_failure_only_under_workers_is_named_failed_in_parallel(fx):
    fx.write('''
        import os
        def test_race():
            assert "PYTEST_XDIST_WORKER" not in os.environ
    ''')
    fx.commit()
    result = fx.run(workers="2")
    assert result.returncode == 1
    assert "FAILED-IN-PARALLEL: tests/test_x.py::test_race" in result.stdout
    assert "FLAKY" not in result.stdout


def test_a_pass_after_a_tree_change_is_named_failed_then_passed(fx):
    (fx.tree / "flag.txt").write_text("a")
    fx.write('''
        from pathlib import Path
        def test_flip():
            flag = Path(__file__).parents[1] / "flag.txt"
            if flag.read_text() == "a":
                flag.write_text("b")
                assert False, "edits a tracked file, then fails"
    ''')
    fx.commit()
    result = fx.run()
    assert result.returncode == 1
    assert "FAILED-THEN-PASSED: tests/test_x.py::test_flip" in result.stdout
    assert "FLAKY" not in result.stdout


def test_a_collection_error_does_not_rerun(fx):
    fx.write("def test_a(:\n")
    fx.commit()
    result = fx.run()
    assert result.returncode == 2
    assert len(fx.invocations()) == 1


@pytest.mark.parametrize("count,reruns", [(50, 3), (51, 1)])
def test_more_than_fifty_failures_do_not_rerun(fx, count, reruns):
    fx.write("".join(f"def test_f{i}(): assert False\n" for i in range(count)))
    fx.commit()
    result = fx.run()
    assert result.returncode == 1
    assert len(fx.invocations()) == reruns


def test_a_stale_lastfailed_entry_prints_no_label(fx):
    fx.write("def test_stale(): pass\ndef test_real(): assert False\n")
    fx.commit()
    cache = fx.tree / ".pytest_cache/v/cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text(json.dumps({"tests/test_x.py::test_stale": True}))
    result = fx.run()
    assert result.returncode == 1
    assert "FLAKY" not in result.stdout and "FAILED-" not in result.stdout


@needs_xdist
def test_the_recorder_lists_each_nodeid_once_under_workers(fx):
    fx.write("""
        import pytest
        @pytest.mark.parametrize('n', range(6))
        def test_p(n): assert n < 5
    """)
    fx.commit()
    result = fx.run(workers="2")
    assert result.returncode == 1
    assert "FLAKY" not in result.stdout and "FAILED-" not in result.stdout
    reruns = fx.invocations()[1:]
    assert len(reruns) == 2 and reruns[0][-1] == "tests/test_x.py::test_p[5]" and reruns[0].count("tests/test_x.py::test_p[5]") == 1


def test_rerun_output_is_not_echoed(fx):
    fx.write(FLAKY)
    fx.commit()
    result = fx.run()
    report = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("AFFECTED "))
    assert report.count("1 failed") <= 1


def test_a_change_committed_by_the_run_is_not_a_flake(fx):
    """Clean tree before and after (the run commits its edit): the trees differ, so no FLAKY."""
    (fx.tree / "flag.txt").write_text("a")
    fx.write("""
        import subprocess
        from pathlib import Path
        def test_commit():
            root = Path(__file__).parents[1]
            flag = root / "flag.txt"
            if flag.read_text() == "a":
                flag.write_text("b")
                subprocess.run(["git", "commit", "-qam", "edit"], cwd=root, check=True)
                assert False, "commits a tracked-file change, then fails"
    """)
    fx.commit()
    result = fx.run()
    assert result.returncode == 1
    assert "FAILED-THEN-PASSED: tests/test_x.py::test_commit" in result.stdout
    assert "FLAKY" not in result.stdout


def test_collect_only_keeps_maxfail(fx):
    fx.write("def test_ok(): pass\n")
    fx.commit()
    result = fx.run("--collect-only", "--maxfail", "3")
    assert result.returncode == 0, result.stdout + result.stderr
    args = fx.invocations()[0]
    assert "--collect-only" in args and args[args.index("--maxfail") + 1] == "3"
    assert "-q" not in args


@pytest.mark.parametrize("workers,auto_workers", [("0", None), ("00", None), ("auto", "0")])
def test_a_serial_fail_pass_fail_sequence_is_not_failed_in_parallel(fx, workers, auto_workers):
    """Whatever spelling makes xdist start no workers, no label claims a parallel failure."""
    if auto_workers is not None:
        fx.env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = auto_workers
    fx.write("""
        import os
        from pathlib import Path
        def test_sequence():
            counter = Path(os.environ["FIXTURE_MARKER"] + ".count")
            runs = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(runs))
            assert runs == 2, f"run {runs}: fails on the first and third run"
    """)
    fx.commit()
    result = fx.run(workers=workers)
    assert result.returncode == 1
    assert len(fx.invocations()) == 3
    assert "FAILED-IN-PARALLEL" not in result.stdout and "FLAKY" not in result.stdout


def test_flake_labels_name_a_parallel_failure_only_when_workers_ran():
    import importlib.util
    spec = importlib.util.spec_from_file_location("affected_tests_labels", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ids, same = ["t::a"], ["h"] * 4
    passed, failed = {"t::a": "passed"}, {"t::a": "failed"}
    assert module.flake_labels(ids, passed, failed, same) == ["FAILED-IN-PARALLEL: t::a"]
    assert module.flake_labels(ids, passed, failed, same, parallel=False) == []


@pytest.mark.parametrize("text", [
    '{"outcomes": {"t::a": "failed"}, "workers": "false"}',
    '{"outcomes": {"t::a": "failed"}, "workers": 1}',
    '{"outcomes": {"t::a": "failed"}, "workers": null}',
    '{"outcomes": null, "workers": true}',
    '{"outcomes": ["t::a"], "workers": true}',
    '{"outcomes": {"t::a": 1}, "workers": true}',
    '{"outcomes": {"t::a": "failed"}}', '{"workers": true}', "[]", "null", "not json", ""])
def test_a_malformed_recorder_file_means_no_outcomes_and_no_workers(text):
    import importlib.util
    spec = importlib.util.spec_from_file_location("affected_tests_record", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read_record(text) == ({}, False)
    assert module.read_record('{"outcomes": {"t::a": "failed"}, "workers": true}') == ({"t::a": "failed"}, True)


def test_an_unreadable_recorder_file_means_no_outcomes_and_no_workers(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("affected_tests_record_file", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    good = tmp_path / "good.json"
    good.write_text('{"outcomes": {"t::a": "failed"}, "workers": true}')
    assert module.read_record_file(good) == ({"t::a": "failed"}, True)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"outcomes": {"t::\xff": "failed"}, "workers": true}')
    assert module.read_record_file(bad) == ({}, False)
    assert module.read_record_file(tmp_path / "missing.json") == ({}, False)
    assert module.read_record_file(tmp_path) == ({}, False)       # a directory
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000)
    assert module.read_record_file(deep) == ({}, False)


def test_collection_attempt_and_retries_get_isolated_basetemps(fx):
    fx.write(FLAKY)
    fx.commit()
    collection = fx.run("--collect-only")
    assert collection.returncode == 0, collection.stdout + collection.stderr
    run = fx.run()
    assert run.returncode == 1, run.stdout + run.stderr

    invocations = fx.invocations()
    assert len(invocations) == 4
    bases = [Path(args[args.index("--basetemp") + 1]) for args in invocations]
    assert len(set(bases)) == 4
    assert len({base.parent for base in bases[1:]}) == 1
    assert all(base.parent.name.startswith("pg-affected-") for base in bases)
    assert all(not base.parent.exists() for base in bases)


# LE-FAST-CHECKS-RESULT-READ-FROM-TAIL: --run ends with one machine-read result line.
PGTEST = Path(__file__).resolve().parents[1] / "tools/pgtest.py"
LINE = re.compile(r"^AFFECTED rc=(\d+) batches=(\d+) failed=(\d+)$")


def selector():
    spec = importlib.util.spec_from_file_location("pgtest_result", PGTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def last_line(result):
    return result.stdout.rstrip("\n").splitlines()[-1]


def test_early_failing_batch_with_passing_last_batch_is_nonzero_and_counted():
    summary = selector().affected_summary([("pytest", 1), ("keeper-postgres", 0)])
    assert summary == (1, "AFFECTED rc=1 batches=2 failed=1")


def test_every_batch_green_is_zero():
    assert selector().affected_summary([("pytest", 0), ("keeper-postgres", 0)]) == (
        0, "AFFECTED rc=0 batches=2 failed=0")


def test_a_failed_batch_keeps_its_own_exit_code():
    assert selector().affected_summary([("pytest", 0), ("keeper-postgres", 5)])[0] == 5


def test_failing_run_ends_with_the_result_line_and_exits_nonzero(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    assert False\n")
    fixture.commit()
    result = fixture.run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert LINE.match(last_line(result)), result.stdout
    assert last_line(result) == "AFFECTED rc=1 batches=1 failed=1"


def test_passing_run_ends_with_the_result_line(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    pass\n")
    fixture.commit()
    result = fixture.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert last_line(result) == "AFFECTED rc=0 batches=1 failed=0"


def test_an_empty_selection_still_ends_with_the_result_line(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    pass\n")
    fixture.commit()
    result = subprocess.run(
        [sys.executable, str(TOOL), "--base", "HEAD", "--root", str(fixture.tree),
         "--changed", "docs/none.md", "--run", "-n", "0", "--within", "tests/test_x.py"],
        cwd=fixture.tree, env=fixture.env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert last_line(result) == "AFFECTED rc=0 batches=0 failed=0", result.stdout


def test_a_refused_selection_still_ends_with_the_result_line(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    pass\n")
    fixture.commit()
    result = fixture.run("--within", "no/such/path")
    assert result.returncode == 2, result.stdout + result.stderr
    assert last_line(result) == "AFFECTED rc=2 batches=0 failed=1", result.stdout


def test_argparse_exits_and_collection_only_runs_end_with_the_result_line(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    pass\n")
    fixture.commit()
    bad = subprocess.run([sys.executable, str(TOOL), "--run"], cwd=fixture.tree, env=fixture.env,
                         capture_output=True, text=True, timeout=300)
    assert bad.returncode == 2
    assert last_line(bad) == "AFFECTED rc=2 batches=0 failed=1", bad.stdout + bad.stderr
    collect = fixture.run("--collect-only")
    assert collect.returncode == 0, collect.stdout + collect.stderr
    assert last_line(collect) == "AFFECTED rc=0 batches=0 failed=0", collect.stdout


def test_an_early_failing_batch_then_a_passing_batch_through_the_cli(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    assert False\n")
    fixture.commit()
    tool = load_tool()
    # A second batch that passes: the CLI wiring still reports the first failure last.
    monkeypatch.setattr(tool, "keeper_postgres_files", lambda root, changed: ["tests/x"])
    monkeypatch.setattr(tool, "run_keeper_postgres", lambda *args, **kwargs: 0)
    for key, value in fixture.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(fixture.tree)
    monkeypatch.setattr(sys, "argv", [str(TOOL), "--base", "HEAD", "--root", str(fixture.tree),
                                      "--changed", "tests/test_x.py", "--run", "-n", "0"])
    assert tool.main() == 1
    lines = capsys.readouterr().out.rstrip("\n").splitlines()
    assert lines[-1] == "AFFECTED rc=1 batches=2 failed=1", lines[-3:]


def load_tool():
    spec = importlib.util.spec_from_file_location("affected_tests_cli", TOOL)
    tool = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOL.parent))
    try:
        spec.loader.exec_module(tool)
    finally:
        sys.path.pop(0)
    return tool


def test_the_abbreviated_run_flag_and_operational_errors_end_with_a_result_line(tmp_path, monkeypatch, capsys):
    fixture = Fixture(tmp_path, monkeypatch)
    fixture.write("def test_x():\n    pass\n")
    fixture.commit()
    # `--ru` is argparse's own abbreviation of `--run`: one run-mode decision, the parser's.
    short = subprocess.run([sys.executable, str(TOOL), "--ru", "--within", "no/such/path", "--base", "HEAD"],
                           cwd=fixture.tree, env=fixture.env, capture_output=True, text=True, timeout=300)
    assert short.returncode == 2
    assert last_line(short) == "AFFECTED rc=2 batches=0 failed=1", short.stdout + short.stderr
    ambiguous = subprocess.run([sys.executable, str(TOOL), "--ru"], cwd=fixture.tree, env=fixture.env,
                               capture_output=True, text=True, timeout=300)
    assert last_line(ambiguous) == "AFFECTED rc=2 batches=0 failed=1", ambiguous.stdout
    # One diagnostic, not one per parse: a usage error and --help each print their text once.
    assert short.stderr.count("usage:") == 0 and ambiguous.stderr.count("usage:") == 1, ambiguous.stderr
    # After `--` a `--run` is a value, not a flag: the usage error stays free of a result line.
    after = subprocess.run([sys.executable, str(TOOL), "--base", "HEAD", "--", "--run"], cwd=fixture.tree,
                           env=fixture.env, capture_output=True, text=True, timeout=300)
    assert after.returncode == 2 and "AFFECTED" not in after.stdout + after.stderr, after.stdout + after.stderr
    helped = subprocess.run([sys.executable, str(TOOL), "--help"], cwd=fixture.tree, env=fixture.env,
                            capture_output=True, text=True, timeout=300)
    assert helped.returncode == 0 and helped.stdout.count("usage:") == 1 and "AFFECTED" not in helped.stdout
    # An operational error (no scratch directory) is nonzero and says so, then still raises.
    tool = load_tool()

    def no_scratch(*args, **kwargs):
        raise OSError("No usable temporary directory found")

    monkeypatch.setattr(tool, "affected_pytest", no_scratch)
    for key, value in fixture.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(fixture.tree)
    monkeypatch.setattr(sys, "argv", [str(TOOL), "--base", "HEAD", "--root", str(fixture.tree),
                                      "--changed", "tests/test_x.py", "--run", "-n", "0"])
    with pytest.raises(OSError):
        tool.main()
    assert capsys.readouterr().out.rstrip("\n").splitlines()[-1] == "AFFECTED rc=1 batches=0 failed=1"
