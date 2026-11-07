"""Ownership and output disclosure regression probes for Dell CI."""
import json
import subprocess
import os
from pathlib import Path
import shutil
import shlex

from test_dell_ci_run import Fake, ci, invoke
from dell_ci_jobs import sandbox_argv, isolated_script
import pytest


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_remote_output_never_persisted(tmp_path, failure):
    fake = Fake()
    canary = "remote-output-canary"

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith("timeout "):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, canary.encode(), canary)
            if failure == "oserror":
                raise OSError(canary)
            return subprocess.CompletedProcess(argv, 7, canary, canary)
        return result

    code, record = invoke(tmp_path, runner, jobs="pytest-postgres")
    assert code == 1
    assert canary not in json.dumps(record)
    assert "output_tail" not in record["jobs"][0]


def test_concurrent_container_ownership(tmp_path, monkeypatch):
    namespace = ci.service_script.__globals__
    monkeypatch.setitem(namespace, "POSTGRES", 'container_id="$container"\nsleep 0.1')
    docker = tmp_path / "docker"
    calls = tmp_path / "calls"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    processes = []
    for run in ["run-one", "run-two"]:
        root = tmp_path / run
        root.mkdir()
        script = ci.service_script("pytest-postgres", str(root / "pytest-postgres"))
        processes.append(subprocess.Popen(["sh", "-c", script]))
    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    assert set(calls.read_text().splitlines()) == {
        "rm -f dell-ci-run-one-pytest-postgres",
        "rm -f dell-ci-run-two-pytest-postgres"}


def test_failed_container_creation_does_not_remove_other_run(tmp_path, monkeypatch):
    namespace = ci.service_script.__globals__
    monkeypatch.setitem(namespace, "POSTGRES", "container_id=$(false)")
    script = ci.service_script("pytest-postgres", str(tmp_path / "scratch"))
    calls = tmp_path / "calls"
    docker_stub = ('calls_log=$1; docker() { '
                   'printf "%s\\n" "$*" >> "$calls_log"; exit 99; }; ')
    result = subprocess.run(
        ["sh", "-c", docker_stub + script, "sh", str(calls)])
    assert result.returncode == 1
    assert not calls.exists() or not any(
        line.startswith("rm -f ") for line in calls.read_text().splitlines())


def test_temporal_ports_and_liveness():
    body = ci.service_script.__globals__["RUST"]["rust-worker"]
    assert 'sock.bind(("127.0.0.1", 0))' in body
    assert "range(3)" in body
    assert '--port "$port" --http-port "$2" --metrics-port "$3"' in body
    assert 'kill -0 "$server" 2>/dev/null || exit 1' in body
    assert 'health --address "127.0.0.1:$port"' in body
    assert "PG_TEMPORAL_ADDR=http://127.0.0.1:$port" in body


def test_postgres_password_is_fresh_and_job_private():
    script = isolated_script('true', '/driver', '/scratch/job', postgres=True)
    assert 'secrets.token_hex(32)' in script
    assert '-e POSTGRES_PASSWORD' in script
    assert ' sh "$PG_DSN"' in script
    argv = sandbox_argv('/checkout', '/scratch', '/rust', '/cargo', 'true')
    assert 'POSTGRES_PASSWORD' not in argv
    assert 'PG_DSN' not in argv


def test_existing_scratch_is_not_removed(tmp_path):
    scratch = tmp_path / "owned-by-other-run"
    scratch.mkdir()
    result = subprocess.run(["sh", "-c", ci.service_script("rust-spike", str(scratch))])
    assert result.returncode != 0
    assert scratch.is_dir()


def test_isolated_script_quotes_paths_and_preserves_command(tmp_path, monkeypatch):
    source = tmp_path / 'source space $(touch injected)'
    job = tmp_path / 'job space $(touch injected)'
    command = 'git diff SCRATCH CHECKOUT TOOLCHAIN CARGOBIN'
    script = isolated_script(command, source, job)
    assignment = next(line for line in script.splitlines() if line.startswith('scratch='))
    result = subprocess.run(['sh', '-c', assignment + '\nprintf "%s" "$scratch"'],
                            capture_output=True, text=True, check=True)
    assert result.stdout == str(job)
    copy = next(line for line in script.splitlines() if line.startswith('cp -a '))
    bundle = next(line for line in script.splitlines() if line.startswith('git -C ') and 'bundle create' in line)
    assert shlex.split(copy)[2] == str(source) + '/.'
    assert shlex.split(bundle)[2] == str(source)
    invocation = script.splitlines()[-1]
    # Evaluate the generated shell quoting without launching a sandbox.
    capture = 'capture() { python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@"; }; '
    monkeypatch.setenv('scratch', str(job))
    monkeypatch.setenv('toolchain', str(tmp_path / 'rust space'))
    result = subprocess.run(['sh', '-c', capture + invocation.replace('/usr/bin/bwrap', 'capture', 1)],
                            capture_output=True, text=True, check=True)
    argv = json.loads(result.stdout)
    assert argv[-1] == command
    assert str(job / 'checkout') in argv
    assert str(job / 'data') in argv
    assert str(tmp_path / 'rust space') in argv
    assert not (tmp_path / 'injected').exists()


def test_sandbox_capabilities(tmp_path):
    commands = [sandbox_argv(str(tmp_path / job / "checkout"),
                            str(tmp_path / job / "scratch"),
                            "/opt/rust", "/opt/cargo/bin", "true")
                for job in ("one", "two")]
    for argv in commands:
        assert "--clearenv" in argv
        assert all(flag in argv for flag in ("--unshare-user", "--unshare-pid",
                                            "--unshare-ipc", "--proc"))
        sources = [argv[i + 1] for i, value in enumerate(argv)
                   if value in {"--bind", "--ro-bind"}]
        for forbidden in ("/var/run", "/run/docker.sock", str(Path.home() / ".ssh"),
                          str(Path.home() / ".cache"), str(Path.home() / ".local/bin"),
                          str(Path.home() / "code/plangraph")):
            for source in sources:
                assert not Path(forbidden).resolve().is_relative_to(
                    Path(source).expanduser().resolve()), (source, forbidden)
        keys = [argv[i + 1] for i, value in enumerate(argv) if value == "--setenv"]
        assert set(keys) == {"HOME", "PATH", "CARGO_HOME", "CARGO_TARGET_DIR",
                             "RUSTUP_HOME", "XDG_CACHE_HOME", "PYTHONPATH",
                             "CARGO_BUILD_JOBS", "LANG"}
        assert ".local/bin" not in " ".join(argv)
    assert commands[0] != commands[1]
    assert set(commands[0]) & {str(tmp_path / "two/scratch")} == set()


def test_every_job_uses_fresh_sandbox(tmp_path):
    fake = Fake()
    code, _ = invoke(tmp_path, fake, jobs=",".join(ci.JOBS))
    assert code == 0
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == len(ci.JOBS)
    for name, command in zip(ci.JOBS, commands):
        assert "/usr/bin/bwrap --unshare-user --unshare-pid" in command
        assert "--clearenv" in command
        assert f"/{name}" in command
        assert 'cp -a ' in command and '"$scratch/checkout/"' in command
        assert "bundle create" in command and "--all HEAD" in command
        assert "$HOME/.local/bin" not in command


@pytest.fixture
def real_sandbox(tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap unavailable")
    checkout, scratch = tmp_path / "checkout", tmp_path / "scratch"
    checkout.mkdir()
    scratch.mkdir()
    def run(command):
        return subprocess.run(sandbox_argv(str(checkout), str(scratch),
                                          "/usr", "/usr/bin", command),
                              capture_output=True, text=True, timeout=20)
    probe = run("true")
    if probe.returncode:
        pytest.fail("bwrap probe failed: " + probe.stderr)
    return run


@pytest.mark.parametrize("attack", ["other-job", "checkout", "ssh", "docker", "env", "proc"])
def test_real_sandbox_denies_host_access(tmp_path, real_sandbox, attack, monkeypatch):
    other = tmp_path / "other-job"
    other.mkdir()
    host = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DELL_CI_SECRET_CANARY", "host-secret")
    commands = {
        "other-job": f"touch {other}/poison",
        "checkout": f"touch {host}/sandbox-escape-probe",
        "ssh": 'cat "$HOME/.ssh/id_ed25519"',
        "docker": "python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(\"/var/run/docker.sock\")'",
        "env": 'test -n "$DELL_CI_SECRET_CANARY"',
        "proc": f"cat /proc/{os.getpid()}/environ",
    }
    result = real_sandbox(commands[attack])
    assert result.returncode != 0, result.stdout
    assert not (other / "poison").exists()
    assert not (host / "sandbox-escape-probe").exists()
