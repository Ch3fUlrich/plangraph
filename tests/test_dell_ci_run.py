import importlib.util, json, os
from pathlib import Path
import re, subprocess
import pytest
SPEC = importlib.util.spec_from_file_location(
    "dell_ci_run", Path(__file__).parents[1] / "tools/dell_ci_run.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
SHA = "ab" * 20
def test_explicit_base_and_main_first_parent(tmp_path):
    fake = Fake()
    assert ci.main(["--sha", SHA, "--base", "origin/custom", "--jobs",
                    "file-size,changelog-docs", "--out", str(tmp_path)], runner=fake) == 0
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 2
    for command in commands:
        assert "origin/custom" in command
        assert 'base=$(git rev-parse HEAD^)' in command
        assert '"$base" = "$(git rev-parse HEAD)"' in command
@pytest.mark.parametrize("name", ["pytest", "pytest-postgres", "browser"])
def test_python_jobs_private_venv(tmp_path, name):
    fake = Fake()
    invoke(tmp_path, fake, jobs=name)
    command = next(c for c in fake.calls if c.startswith("timeout "))
    assert "python3 -m venv" in command
    assert "/venv/bin/python -m pip install --no-index --find-links /scratch/wheels -r requirements-ci.txt" in command
    assert "export PATH=" in command
    assert f"{name}: venv setup or job failed" in command
@pytest.mark.parametrize("name", ["pytest", "rust", "rust-worker", "rust-spike"])
def test_trusted_fetch_precedes_offline_sandbox(tmp_path, name):
    fake = Fake()
    invoke(tmp_path, fake, jobs=name)
    command = next(c for c in fake.calls if c.startswith("timeout "))
    script = __import__('shlex').split(command)[-1]
    outer, inner = script.split('/usr/bin/bwrap', 1)
    assert 'env -i HOME=' in outer and '--unshare-net' not in outer
    assert '--unshare-net' in inner and '--setenv CARGO_NET_OFFLINE true' in inner
    argv = __import__('shlex').split(outer[outer.index('env -i '):])
    program = argv[argv.index('-c') + 1]
    assert program.count('subprocess.run(') == 1
    assert all(x not in program for x in ('shell=True', 'exec(', 'eval(', 'setup.py', 'build.rs'))
    if name == 'pytest':
        assert '"--only-binary=:all:"' in program
        assert '"download"' in program and 'unsupported CI requirement' in program
        assert '--no-index --find-links /scratch/wheels' in inner
    else:
        assert '"fetch"' in program and 'unsupported Cargo source' in program
        assert 'Cargo.toml").read' not in program
        assert 'cwd=stage' in program
    assert 'pip install' not in outer and 'cargo test' not in outer
@pytest.mark.parametrize("content,expected", [
    ("unrelated", ""), ("keeper_util sqlite3", ""),
    ("keeper_util", "tests/test_sample.py"),
])
def test_postgres_optional_files(tmp_path, content, expected):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(content)
    body = ci.service_script.__globals__["POSTGRES"]
    selection = next(line for line in body.splitlines() if line.startswith("FILES="))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", selection + '\n' + 'printf "%s" "$FILES"'],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
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
    assert "secret" not in json.dumps(record)
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
    assert fake.calls[:2] == ['git -C "$HOME"/code/plangraph fetch origin',
                              f'git -C "$HOME"/code/plangraph fetch origin {SHA}']
    assert record["sha"] == SHA and record["host"] == "test-host"
    assert record["started"]
    assert all(j["duration_s"] >= 0 for j in record["jobs"])
    assert any("--detach" in c and SHA in c for c in fake.calls)
    assert any("--setenv PYTHONPATH /work/src" in c for c in fake.calls)
    assert any('python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests' in c
               for c in fake.calls)
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 5
    assert all("python3 " in c for c in commands)
    assert all(not re.search(r"(?<!/bin/)\bpython ", c) for c in commands)
    assert all("/bin/python -m pip" in c for c in commands if "pytest" in c)
def test_requested_sha_fetch_failure(tmp_path):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1] == f'git -C "$HOME"/code/plangraph fetch origin {SHA}':
            return subprocess.CompletedProcess(argv, 1, "", "secret")
        return result

    code, record = invoke(tmp_path, runner)
    assert code == 1
    assert record["reason"] == "requested SHA fetch failed"
    assert not any("worktree add" in command for command in fake.calls)
    assert not record["jobs"]
@pytest.mark.parametrize("repo", ["~/code/custom", "/srv/plangraph"])
def test_explicit_repository(tmp_path, repo):
    fake = Fake()
    assert ci.main(["--sha", SHA, "--repo", repo, "--jobs", "select",
                    "--out", str(tmp_path)], runner=fake) == 0
    prefix = 'git -C ' + (repo.replace("~/", '"$HOME"/') if repo.startswith("~/") else repo)
    for operation in ["fetch origin", f"fetch origin {SHA}",
                      "worktree add --detach", "worktree remove --force"]:
        assert any(prefix + " " + operation in command for command in fake.calls)
    assert all("git -C " in command for command in fake.calls if "git " in command)
@pytest.mark.parametrize("repo", ["relative/path", "~other/repo", "/repo;true",
                                 "~/repo$(id)", "/repo\ntrue", "--help", ""])
def test_invalid_repository_refused(tmp_path, repo):
    fake = Fake()
    with pytest.raises(SystemExit):
        ci.main(["--sha", SHA, "--repo=" + repo, "--out", str(tmp_path)], runner=fake)
    assert not fake.calls
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
    code = ci.main(["--sha", SHA, "--out", str(tmp_path)], runner=fake)
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert code == 0 and record["result"] == "success"
    assert [j["job"] for j in record["jobs"]] == list(ci.JOBS)
    assert record["not_requested"] == []
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == len(ci.JOBS)
    expected = {
        "select": "python3 tools/ci_select.py select --full",
        "scrub": "python3 tools/split/scrub_check.py .",
        "file-size": "python3 tools/check_file_size.py",
        "changelog-docs": "python3 tools/check_changelog_docs.py",
        "pytest": "python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests",
        "pytest-postgres": "tests/contract tests/test_keeper_pg_unit.py",
        "rust": "cargo check --target x86_64-pc-windows-gnu",
        "rust-worker": "PG_TEMPORAL_ADDR=http://127.0.0.1:$port",
        "rust-spike": "cargo test --manifest-path rust/spikes/temporal-spike/Cargo.toml --test replay",
        "browser": "python3 -m pytest -q tests/browser -m browser",
        "ci-gate": "python3 tools/ci_select.py gate --needs",
    }
    assert set(expected) == set(ci.JOBS)
    for name, command in zip(ci.JOBS, commands):
        assert expected[name] in command, name
    assert all(j["result"] == "success" and j["exit"] == 0 for j in record["jobs"])


def test_subset_jobs_lists_omissions(tmp_path):
    fake = Fake()
    code, record = invoke(tmp_path, fake, jobs="select,browser")
    assert code == 0 and record["result"] == "success"
    assert [job["job"] for job in record["jobs"]] == ["select", "browser"]
    assert record["not_requested"] == [
        "scrub", "file-size", "changelog-docs", "pytest", "pytest-postgres",
        "rust", "rust-worker", "rust-spike", "ci-gate"]
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == 2
    assert "python3 tools/ci_select.py select --full" in commands[0]
    assert "python3 -m pytest -q tests/browser -m browser" in commands[1]


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
    assert set(record) == {"sha", "host", "started", "jobs", "not_requested", "result"}
    assert "password-secret" not in json.dumps(record)
    assert set(record["jobs"][0]) == {"job", "result", "duration_s", "exit"}


@pytest.mark.parametrize("name", ["pytest-postgres", "rust", "rust-worker", "rust-spike"])
def test_service_cleanup(tmp_path, monkeypatch, name):
    fake = Fake()
    invoke(tmp_path, fake, jobs=name)
    command = next(c for c in fake.calls if c.startswith("timeout "))
    assert "trap cleanup EXIT" in command
    assert 'rm -rf -- "$scratch"' in command
    assert 'docker rm -f "$container_id"' in command
    assert "worktree remove" in fake.calls[-1]
    # Execute only a stub body: cleanup must not invoke Docker for Rust jobs.
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    docker.chmod(0o755)
    (tmp_path / "protoc").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "protoc").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    namespace = ci.service_script.__globals__
    assert 'kill "$server"' in ci.service_script(name, '/scratch/job')
    if 'server=$!' in command:
        assert 'kill "$server"' in command
    if name == "pytest-postgres":
        original_postgres = namespace["POSTGRES"]
        monkeypatch.setitem(namespace, "POSTGRES", "container_id=owned-id")
    else:
        monkeypatch.setitem(namespace["RUST"], name, ":")
    scratch = tmp_path / name
    result = subprocess.run(["sh", "-c", ci.service_script(name, str(scratch))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not scratch.exists()
    if name == "pytest-postgres":
        assert calls.read_text() == "rm -f owned-id\n"
        # Execute the actual outer lifecycle used by the driver, not just the
        # legacy service helper. Stub setup only; retain its cleanup verbatim.
        monkeypatch.setitem(namespace, "POSTGRES", original_postgres)
        isolated = ci.isolated_script(':', tmp_path, tmp_path / 'isolated', postgres=True)
        prefix = isolated.split('mkdir "$scratch/checkout"', 1)[0]
        calls.unlink()
        result = subprocess.run(['sh', '-c', prefix + '\ncontainer_id=isolated-owned-id\n'],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert calls.read_text() == 'rm -f isolated-owned-id\n'
        assert not (tmp_path / 'isolated').exists()
    else:
        assert not calls.exists()
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

    code, record = invoke(tmp_path, runner, jobs="rust")
    assert code == df_exit
    if df_exit:
        assert record["jobs"][0]["reason"] == "df command failed"
    assert any(c.startswith("timeout ") for c in fake.calls) == (df_exit == 0)
    assert all("--tmpfs /tmp" in c for c in fake.calls if c.startswith("timeout "))


@pytest.mark.parametrize("output", ["", "Filesystem Blocks Used Available\n",
                                    "disk 90000000 0 invalid 0% /home\n"])
def test_malformed_disk_space_output(tmp_path, output):
    fake = Fake()

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith("df -Pk "):
            result.stdout = output
        return result

    code, record = invoke(tmp_path, runner, jobs="rust")
    assert code == 1
    assert record["jobs"][0]["reason"] == "cannot parse df output"
    assert not any(c.startswith("timeout ") for c in fake.calls)
@pytest.mark.parametrize("exit_code", [0, 7, 143])
def test_remote_home_expansion_and_scratch_cleanup(tmp_path, monkeypatch, exit_code):
    protoc = tmp_path / "protoc"
    protoc.write_text("#!/bin/sh\nexit 0\n")
    protoc.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
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
