"""LE-FAST-CHECKS-RESULT-READ-FROM-TAIL: affected_tests --run ends with one machine-read result line."""
import importlib.util
import re
from pathlib import Path

from test_affected_tests_quiet import Fixture

TOOL = Path(__file__).resolve().parents[1] / "tools/affected_tests.py"
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
