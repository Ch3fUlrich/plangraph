"""Ownership and output disclosure regression probes for Dell CI."""
import json
import subprocess

from test_dell_ci_run import Fake, ci, invoke
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
    result = subprocess.run(["sh", "-c", 'docker() { exit 99; }; ' + script])
    assert result.returncode == 1


def test_temporal_ports_and_liveness():
    body = ci.service_script.__globals__["RUST"]["rust-worker"]
    assert 'sock.bind(("127.0.0.1", 0))' in body
    assert "range(3)" in body
    assert '--port "$port" --http-port "$2" --metrics-port "$3"' in body
    assert 'kill -0 "$server" 2>/dev/null || exit 1' in body
    assert 'health --address "127.0.0.1:$port"' in body
    assert "PG_TEMPORAL_ADDR=http://127.0.0.1:$port" in body


def test_existing_scratch_is_not_removed(tmp_path):
    scratch = tmp_path / "owned-by-other-run"
    scratch.mkdir()
    result = subprocess.run(["sh", "-c", ci.service_script("rust-spike", str(scratch))])
    assert result.returncode != 0
    assert scratch.is_dir()
