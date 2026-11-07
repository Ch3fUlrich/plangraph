import importlib.util
import json
from pathlib import Path
import re
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
        elif command.startswith('df -Pk "$HOME"/.cache/dell-ci/'):
            output = 'Filesystem 1024-blocks Used Available Capacity Mounted\ndisk 90000000 0 90000000 0% /home\n'
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


@pytest.mark.parametrize("fake,reason", [
    (Fake(head="cd" * 20), "HEAD mismatch"),
    (Fake(dirty="?? dirty\n"), "dirty worktree"),
])
def test_moved_or_dirty_refused(tmp_path, fake, reason):
    code, record = invoke(tmp_path, fake)
    assert code != 0 and record["result"] == "failure"
    assert record["reason"] == reason
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
    assert fake.calls[:2] == ["git fetch origin", f"git fetch origin {SHA}"]
    assert record["sha"] == SHA and record["host"] == "test-host"
    assert record["started"]
    assert all(j["duration_s"] >= 0 for j in record["jobs"])
    assert any("--detach" in c and SHA in c for c in fake.calls)
    assert any("PYTHONPATH=" in c and "/src" in c for c in fake.calls)
    assert any('python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests' in c
               for c in fake.calls)
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 5
    assert all("python3 " in c and "python " not in c for c in commands)


def test_requested_sha_fetch_failure(tmp_path):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1] == f"git fetch origin {SHA}":
            return subprocess.CompletedProcess(argv, 1, "", "secret")
        return result

    code, record = invoke(tmp_path, runner)
    assert code == 1
    assert record["reason"] == "requested SHA fetch failed"
    assert not any("worktree add" in command for command in fake.calls)
    assert not record["jobs"]


def test_failed_worktree_add_still_removes_private_directory(tmp_path):
    directory = tmp_path / "private"
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        command = argv[-1]
        if "worktree add" in command:
            directory.mkdir(mode=0o700)
            (directory / "leftover-target").mkdir()
            (directory / "leftover-target" / "artifact").write_text("partial build")
            return subprocess.CompletedProcess(argv, 1, "", "setup failed")
        if "worktree remove" in command:
            # Execute the real cleanup shell with a failing git removal.
            command = re.sub(r'"\$HOME"/.cache/dell-ci/[0-9a-f]+',
                             str(directory), command)
            return subprocess.run(["sh", "-c", "git() { return 1; }; " + command],
                                  capture_output=True, text=True, check=False)
        return result

    code, record = invoke(tmp_path, runner)
    assert code == 1
    assert record["reason"] == "worktree setup failed"
    assert record["cleanup"] == "failure"
    assert not directory.exists()
    assert not record["jobs"]


def test_slice_two_skipped(tmp_path):
    fake = Fake()
    code, record = invoke(tmp_path, fake, jobs="pytest-postgres,rust,rust-worker,rust-spike")
    assert code == 0
    assert record["result"] == "success"
    assert [j["job"] for j in record["jobs"]] == [
        "pytest-postgres", "rust", "rust-worker", "rust-spike"]
    assert all(j["result"] == "success"
               for j in record["jobs"])
    assert sum(c.startswith("timeout ") for c in fake.calls) == 4


@pytest.mark.parametrize("jobs", ["rust", "scrub,rust", "rust,scrub"])
def test_partial_requested_jobs(tmp_path, jobs):
    code, record = invoke(tmp_path, Fake(), jobs=jobs)
    assert code == 0 and record["result"] == "success"
    assert [j["job"] for j in record["jobs"]] == jobs.split(",")


@pytest.mark.parametrize("jobs", ["scrub,rust", "rust,scrub"])
def test_failure_takes_precedence_over_partial(tmp_path, jobs):
    code, record = invoke(tmp_path, Fake(fail=True), jobs=jobs)
    assert code == 1 and record["result"] == "failure"


def test_default_jobs_success_lists_omissions(tmp_path):
    fake = Fake()
    code = ci.main(["--sha", SHA, "--out", str(tmp_path), "--jobs",
                    "scrub,file-size,changelog-docs,pytest"], runner=fake)
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert code == 0 and record["result"] == "success"
    assert [j["job"] for j in record["jobs"]] == [
        "scrub", "file-size", "changelog-docs", "pytest"]
    assert record["not_requested"] == [
        "select", "pytest-postgres", "rust", "rust-worker", "rust-spike", "browser", "ci-gate"]
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 4
    for command, script in zip(commands, [
            "tools/split/scrub_check.py", "tools/check_file_size.py",
            "tools/check_changelog_docs.py", "-m pytest"]):
        assert "python3 " + script in command
    assert all(j["result"] == "success" and j["exit"] == 0 for j in record["jobs"])


@pytest.mark.parametrize("name", ["pytest-postgres", "rust", "rust-worker", "rust-spike"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_service_result(tmp_path, name, exit_code):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith("timeout "):
            return subprocess.CompletedProcess(argv, exit_code, "password-secret", "password-secret")
        return result

    code, record = invoke(tmp_path, runner, jobs=name)
    assert code == bool(exit_code)
    assert record["jobs"][0]["result"] == ("failure" if exit_code else "success")
    assert "password-secret" not in json.dumps(record)


@pytest.mark.parametrize("name", ["pytest-postgres", "rust", "rust-worker", "rust-spike"])
def test_service_cleanup(tmp_path, name):
    fake = Fake()
    invoke(tmp_path, fake, jobs=name)
    command = next(c for c in fake.calls if c.startswith("timeout "))
    assert "trap cleanup EXIT" in command
    assert 'rm -rf -- "$scratch"' in command
    assert 'docker rm -f "$container"' in command
    assert 'kill "$server"' in command
    assert "worktree remove" in fake.calls[-1]


@pytest.mark.parametrize("name", ["rust", "rust-worker", "rust-spike"])
@pytest.mark.parametrize("available", [4194304, 20971519])
def test_low_space_refuses(tmp_path, name, available):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith('df -Pk "$HOME"/.cache/dell-ci/'):
            result.stdout = f'Filesystem Blocks Used Available Capacity Mounted\ndisk 90000000 0 {available} 67% /home\n'
        return result

    code, record = invoke(tmp_path, runner, jobs=name)
    assert code == 1
    assert record["jobs"][0]["reason"] == "insufficient ~/.cache/dell-ci space: requires at least 20 GB free"
    assert not any(c.startswith("timeout ") for c in fake.calls)


@pytest.mark.parametrize("available,df_exit", [(20971520, 0), (90000000, 1)])
def test_disk_space_boundary(tmp_path, available, df_exit):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith("df -Pk "):
            result.stdout = f'Filesystem Blocks Used Available Capacity Mounted\ndisk 90000000 0 {available} 0% /home\n'
            result.returncode = df_exit
        return result

    code, _ = invoke(tmp_path, runner, jobs="rust")
    assert code == df_exit
    assert any(c.startswith("timeout ") for c in fake.calls) == (df_exit == 0)
    assert all("/tmp" not in c for c in fake.calls)


@pytest.mark.parametrize("exit_code", [0, 7, 143])
def test_remote_home_expansion_and_scratch_cleanup(tmp_path, monkeypatch, exit_code):
    home = tmp_path / "remote home"
    root = home / ".cache/dell-ci/run"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setitem(ci.service_script.__globals__["RUST"], "rust-spike",
                        'test "$CARGO_TARGET_DIR" = "$scratch/target"\n'
                        'mkdir "$CARGO_TARGET_DIR"\n'
                        f'exit {exit_code}')
    script = ci.service_script("rust-spike", '"$HOME"/.cache/dell-ci/run/rust-spike')
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == exit_code
    assert not (root / "rust-spike").exists()
