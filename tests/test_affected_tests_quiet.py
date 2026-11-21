"""Quiet loop and flake naming of tools/affected_tests.py, on real pytest in a fixture tree."""
import json
import os
from pathlib import Path
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
    assert result.stdout.count("1 failed") <= 1
