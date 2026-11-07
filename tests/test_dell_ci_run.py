import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location(
    "dell_ci_run", Path(__file__).parents[1] / "tools/dell_ci_run.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SHA = "ab" * 20


class Fake:
    def __init__(self, head=SHA, dirty="", fail=False, timeout=False):
        self.head, self.dirty = head, dirty
        self.fail, self.timeout = fail, timeout
        self.calls = []

    def __call__(self, argv, **kwargs):
        command = argv[-1]
        self.calls.append(command)
        assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if "rev-parse HEAD" in command:
            output = self.head + "\n"
        elif "status --porcelain" in command:
            output = self.dirty
        else:
            output = ""
        if "scrub_check.py" in command:
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if self.fail:
                return subprocess.CompletedProcess(argv, 7, "", "secret")
        return subprocess.CompletedProcess(argv, 0, output, "")


def invoke(tmp_path, fake, sha=SHA, jobs="scrub"):
    code = ci.main(["--sha", sha, "--host", "test-host", "--jobs", jobs,
                    "--out", str(tmp_path)], runner=fake)
    files = list(tmp_path.glob("*.json"))
    return code, json.loads(files[0].read_text()) if files else None


@pytest.mark.parametrize("sha", ["abc123", "main", "g" * 40, "a" * 41])
def test_prefix_and_refs_refused(tmp_path, sha):
    fake = Fake()
    with pytest.raises(SystemExit):
        invoke(tmp_path, fake, sha)
    assert not fake.calls


@pytest.mark.parametrize("fake", [Fake(head="cd" * 20), Fake(dirty="?? dirty\n")])
def test_moved_or_dirty_refused(tmp_path, fake):
    code, record = invoke(tmp_path, fake)
    assert code != 0 and record["result"] == "failure"
    assert not any("scrub_check.py" in c for c in fake.calls)
    assert any("worktree remove" in c for c in fake.calls)


def test_failure_record_and_cleanup(tmp_path, capsys):
    fake = Fake(fail=True)
    code, record = invoke(tmp_path, fake)
    assert code != 0 and record["result"] == "failure"
    assert record["jobs"][0]["result"] == "failure"
    assert record["jobs"][0]["exit"] == 7
    assert "worktree remove" in fake.calls[-1]
    assert "secret" not in capsys.readouterr().out


def test_timeout(tmp_path):
    code, record = invoke(tmp_path, Fake(timeout=True))
    assert code != 0
    assert record["jobs"][0]["result"] == "timeout"
    assert record["jobs"][0]["exit"] is None


def test_exact_identity_and_commands(tmp_path):
    fake = Fake()
    code, record = invoke(tmp_path, fake, jobs="select,scrub,file-size,changelog-docs,pytest")
    assert code == 0
    assert record["sha"] == SHA and record["host"] == "test-host"
    assert record["started"]
    assert all(j["duration_s"] >= 0 for j in record["jobs"])
    assert any("--detach" in c and SHA in c for c in fake.calls)
    assert any("PYTHONPATH=" in c and "/src" in c for c in fake.calls)
    assert any('python3 tools/affected_tests.py --base origin/main --run -n auto' in c
               for c in fake.calls)
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 5
    assert all("python3 " in c and "python " not in c for c in commands)


def test_slice_two_skipped(tmp_path):
    fake = Fake()
    code, record = invoke(tmp_path, fake, jobs="pytest-postgres,rust,rust-worker,rust-spike")
    assert code == 2
    assert record["result"] == "partial"
    assert [j["job"] for j in record["jobs"]] == [
        "pytest-postgres", "rust", "rust-worker", "rust-spike"]
    assert all(j["result"] == "skipped" and j["reason"] == "slice 2"
               for j in record["jobs"])
    assert not any(c.startswith("timeout ") for c in fake.calls)


@pytest.mark.parametrize("jobs", ["rust", "scrub,rust", "rust,scrub"])
def test_partial_requested_jobs(tmp_path, jobs):
    code, record = invoke(tmp_path, Fake(), jobs=jobs)
    assert code == 2 and record["result"] == "partial"
    assert [j["job"] for j in record["jobs"]] == jobs.split(",")


@pytest.mark.parametrize("jobs", ["scrub,rust", "rust,scrub"])
def test_failure_takes_precedence_over_partial(tmp_path, jobs):
    code, record = invoke(tmp_path, Fake(fail=True), jobs=jobs)
    assert code == 1 and record["result"] == "failure"


def test_default_jobs_success_lists_omissions(tmp_path):
    fake = Fake()
    code = ci.main(["--sha", SHA, "--out", str(tmp_path)], runner=fake)
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert code == 0 and record["result"] == "success"
    assert [j["job"] for j in record["jobs"]] == [
        "scrub", "file-size", "changelog-docs", "pytest"]
    assert record["not_requested"] == [
        "select", "pytest-postgres", "rust", "rust-worker", "rust-spike"]
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 4
    for command, script in zip(commands, [
            "tools/split/scrub_check.py", "tools/check_file_size.py",
            "tools/check_changelog_docs.py", "tools/affected_tests.py"]):
        assert "python3 " + script in command
    assert all(j["result"] == "success" and j["exit"] == 0 for j in record["jobs"])
