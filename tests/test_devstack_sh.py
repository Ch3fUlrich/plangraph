"""tools/devstack.sh: hermetic start/status/stop/keeper coverage with fake temporal+docker."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "devstack.sh"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="devstack.sh is a posix bash script")

TEMPORAL_FAKE = """\
#!/bin/bash
TMP="{tmp}"
if [ "${{1:-}}" = server ] && [ -n "${{FAKE_SERVER_DELAY:-}}" ]; then sleep "$FAKE_SERVER_DELAY"; fi
l=temporal; for a in "$@"; do l="$l $a"; done; printf '%s\n' "$l" >>"$TMP/calls.log"
case "${{1:-}}" in
  server)
    printf '%s\\n' "$$" >"$TMP/temporal_child.pid"
    if [ "${{FAKE_SERVER_DIE:-}}" = 1 ]; then exit 1; fi
    # -a makes /proc/<pid>/cmdline contain "start-dev" so the script trusts the pid file
    if [ "${{FAKE_IGNORE_TERM:-}}" = 1 ]; then
      exec -a "temporal server start-dev" bash -c 'trap "" TERM; while :; do sleep 1; done'
    fi
    exec -a "temporal server start-dev" sleep 300 ;;
  operator)
    if [ "${{FAKE_HEALTH_FAIL:-}}" = 1 ]; then exit 1; fi
    [ -e "$TMP/temporal_child.pid" ] || exit 1
    exit 0 ;;
  worker)
    if [ "${{FAKE_SETCUR_FAIL:-}}" = 1 ]; then exit 1; fi
    exit 0 ;;
esac
exit 0
"""

DOCKER_FAKE = """\
#!/bin/bash
TMP="{tmp}"
l=docker; for a in "$@"; do l="$l $a"; done; printf '%s\n' "$l" >>"$TMP/calls.log"
case "${{1:-}}" in
  info) exit 0 ;;
  ps)
    case "${{2:-}}" in
      -q) [ -e "$TMP/running" ] && printf 'abc123\\n' ;;
      -aq) [ -e "$TMP/container" ] && printf 'abc123\\n' ;;
    esac
    exit 0 ;;
  run) if [ "${{FAKE_RUN_FAIL:-}}" = 1 ]; then touch "$TMP/container"; exit 1; fi; touch "$TMP/container" "$TMP/running"; exit 0 ;;
  start) touch "$TMP/running"; exit 0 ;;
  stop) rm -f "$TMP/running"; exit 0 ;;
  rm) rm -f "$TMP/container" "$TMP/running"; exit 0 ;;
  exec)
    if [ "${{FAKE_PG_NOTREADY:-}}" = 1 ]; then exit 1; fi
    exit 0 ;;
esac
exit 0
"""


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Stack:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, text in (("temporal", TEMPORAL_FAKE), ("docker", DOCKER_FAKE)):
            f = bin_dir / name
            f.write_text(text.format(tmp=str(tmp_path)))
            os.chmod(f, 0o755)
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DEVSTACK_STATE": str(self.state),
            "TEMPORAL_CLI": str(bin_dir / "temporal"),
            "TEMPORAL_PORT": "17999",
            "TEMPORAL_UI_PORT": "18999",
            "PG_PORT": "55999",
            "DEVSTACK_PG_NAME": "devstack-test-pg",
            "DEVSTACK_PG_MODE": "docker",
            "DEVSTACK_HEALTH_TIMEOUT": "3",
        }

    def run(self, *cmds, timeout=90, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run([BASH, str(SCRIPT), *cmds], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def lines(self):
        log = self.tmp / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def stack(tmp_path):
    st = Stack(tmp_path)
    yield st
    try:
        st.run("stop", timeout=60)
    except Exception:
        pass


def test_start_launches_temporal_set_current_and_one_docker_pg(stack):
    assert stack.run("start").returncode == 0
    lines = stack.lines()
    dev = [l for l in lines if l.startswith("temporal server start-dev")]
    assert dev == [f"temporal server start-dev --ip 127.0.0.1 --port 17999 --ui-port 18999 "
                   f"--db-filename {stack.state}/temporal.db"]
    setcur = [l for l in lines if l.startswith("temporal worker deployment set-current-version")]
    assert len(setcur) == 1
    assert "--address 127.0.0.1:17999 --deployment-name d --build-id v1" in setcur[0]
    runs = [l for l in lines if l.startswith("docker run")]
    assert len(runs) == 1
    for token in ("--name devstack-test-pg", "--memory 1g",
                  "POSTGRES_HOST_AUTH_METHOD=trust", "POSTGRES_DB=keeper",
                  "127.0.0.1:55999:5432", "postgres:16"):
        assert token in runs[0]
    joined = "\n".join(lines)
    assert "compose" not in joined
    assert "0.0.0" + ".0" not in joined  # loopback-only


def test_start_is_idempotent(stack):
    assert stack.run("start").returncode == 0
    first = len(stack.lines())
    assert stack.run("start").returncode == 0
    second = stack.lines()[first:]
    assert not any("start-dev" in l for l in second)
    assert not any(l.startswith("docker run") for l in second)


def test_start_waits_for_server_before_reporting_healthy(stack):
    # Race: start must not report healthy until the fake server has logged start-dev.
    r = stack.run("start", extra_env={"FAKE_SERVER_DELAY": "1"})
    assert r.returncode == 0
    dev = [l for l in stack.lines() if l.startswith("temporal server start-dev")]
    assert len(dev) == 1

    r = stack.run("start", extra_env={"FAKE_SERVER_DELAY": "1"})
    assert r.returncode == 0
    dev = [l for l in stack.lines() if l.startswith("temporal server start-dev")]
    assert len(dev) == 1


def test_status_reports_running_then_stopped_after_stop(stack):
    assert stack.run("start").returncode == 0
    r = stack.run("status")
    assert r.returncode == 0
    assert any(l.startswith("temporal: running") for l in r.stdout.splitlines())
    assert "postgres: running (docker devstack-test-pg)" in r.stdout
    assert "export PG_DSN=postgresql://postgres@127.0.0.1:55999/keeper" in r.stdout
    assert "export PG_KEEPER_BACKEND=postgres" in r.stdout
    assert stack.run("stop").returncode == 0
    r = stack.run("status")
    assert r.returncode == 1
    assert "temporal: stopped" in r.stdout


def test_stop_kills_temporal_and_stops_container_but_keeps_state(stack):
    stack.state.mkdir(parents=True)
    (stack.state / "temporal.db").write_text("SENTINEL\n")
    assert stack.run("start").returncode == 0
    pid = int((stack.state / "temporal.pid").read_text().strip())
    assert alive(pid)
    assert stack.run("stop").returncode == 0
    assert not alive(pid)
    lines = stack.lines()
    assert "docker stop devstack-test-pg" in lines
    assert not any(l.startswith("docker rm") for l in lines)
    assert (stack.tmp / "container").exists()
    assert not (stack.tmp / "running").exists()
    assert (stack.state / "temporal.db").read_text() == "SENTINEL\n"
    assert stack.run("stop").returncode == 0


def test_stop_purge_removes_the_container(stack):
    assert stack.run("start").returncode == 0
    assert stack.run("stop", "--purge").returncode == 0
    assert "docker rm -f -v devstack-test-pg" in stack.lines()
    assert not (stack.tmp / "container").exists()
    assert not (stack.tmp / "running").exists()


def test_start_after_plain_stop_reuses_the_container(stack):
    assert stack.run("start").returncode == 0
    assert stack.run("stop").returncode == 0
    first = len(stack.lines())
    assert stack.run("start").returncode == 0
    second = stack.lines()[first:]
    assert "docker start devstack-test-pg" in second
    assert not any(l.startswith("docker run") for l in second)


def test_temporal_health_failure_kills_process_and_exits_1(stack):
    # DEVSTACK_HEALTH_TIMEOUT=3 bounds the poll; subprocess timeout 30 s is headroom
    r = stack.run("start", timeout=30, extra_env={"FAKE_HEALTH_FAIL": "1"})
    assert r.returncode == 1
    assert "not healthy within 3 s" in r.stderr
    assert not (stack.state / "temporal.pid").exists()
    pid = int((stack.tmp / "temporal_child.pid").read_text().strip())
    assert not alive(pid)


def test_health_timeout_escalates_to_kill_for_term_ignoring_server(stack):
    # the fake ignores TERM; the script must escalate to KILL after its short grace
    r = stack.run("start", timeout=30, extra_env={
        "FAKE_HEALTH_FAIL": "1", "FAKE_IGNORE_TERM": "1"})
    assert r.returncode == 1
    assert "not healthy within 3 s" in r.stderr
    assert not (stack.state / "temporal.pid").exists()
    pid = int((stack.tmp / "temporal_child.pid").read_text().strip())
    deadline = time.monotonic() + 3
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid)


def test_dying_start_dev_child_fails_fast(stack):
    # start_temporal watches the child pid: a crash exits in ~1 poll, not after the 30 s timeout
    started = time.monotonic()
    r = stack.run("start", timeout=30, extra_env={
        "FAKE_SERVER_DIE": "1", "FAKE_HEALTH_FAIL": "1", "DEVSTACK_HEALTH_TIMEOUT": "30"})
    elapsed = time.monotonic() - started
    assert r.returncode == 1
    assert elapsed < 2.5, elapsed
    assert not (stack.state / "temporal.pid").exists()


def test_stale_pid_file_for_unrelated_process_is_not_trusted(stack):
    # temporal.pid holding a live but unrelated pid must not make stop kill that process
    stack.state.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "200"])
    try:
        (stack.state / "temporal.pid").write_text(f"{proc.pid}\n")
        assert stack.run("stop").returncode == 0
        assert proc.poll() is None
        assert not (stack.state / "temporal.pid").exists()
    finally:
        proc.kill()
        proc.wait()


def test_reused_container_that_never_becomes_ready_is_stopped_not_removed(stack):
    assert stack.run("start").returncode == 0
    assert stack.run("stop").returncode == 0
    first = len(stack.lines())
    r = stack.run("start", timeout=30, extra_env={"FAKE_PG_NOTREADY": "1"})
    assert r.returncode == 1
    second = stack.lines()[first:]
    assert "docker start devstack-test-pg" in second
    assert "docker stop devstack-test-pg" in second
    assert not any(l.startswith("docker rm") for l in second)


def test_fresh_container_that_never_becomes_ready_is_removed_with_volume(stack):
    r = stack.run("start", timeout=30, extra_env={"FAKE_PG_NOTREADY": "1"})
    assert r.returncode == 1
    assert "docker rm -f -v devstack-test-pg" in stack.lines()
    assert not any(l.startswith("docker rm") and " -v" not in l for l in stack.lines())
    assert not (stack.tmp / "container").exists()


def test_docker_run_failure_removes_partial_container_with_volume(stack):
    r = stack.run("start", timeout=30, extra_env={"FAKE_RUN_FAIL": "1"})
    assert r.returncode == 1
    assert "docker rm -f -v devstack-test-pg" in stack.lines()
    assert not any(l.startswith("docker rm") and " -v" not in l for l in stack.lines())


def test_set_current_failure_does_not_fail_start(stack):
    # the script retries 5x with 2 s sleeps; allow headroom
    r = stack.run("start", timeout=40, extra_env={"FAKE_SETCUR_FAIL": "1"})
    assert r.returncode == 0
    assert "WARNING: set-current-version failed" in r.stderr


def test_bogus_pg_mode_exits_2(stack):
    r = stack.run("start", extra_env={"DEVSTACK_PG_MODE": "bogus"})
    assert r.returncode == 2
    assert "DEVSTACK_PG_MODE" in r.stderr


def test_bogus_pg_mode_is_rejected_before_temporal_starts(stack):
    r = stack.run("start", extra_env={"DEVSTACK_PG_MODE": "bogus"})
    assert r.returncode == 2
    assert not any("start-dev" in l for l in stack.lines())
    assert not (stack.state / "temporal.pid").exists()


def test_missing_temporal_cli_exits_1_with_hint(stack):
    r = stack.run("start", extra_env={"TEMPORAL_CLI": "/nonexistent"})
    assert r.returncode == 1
    assert "TEMPORAL_CLI" in r.stderr


def test_keeper_without_postgres_exits_3(stack):
    r = stack.run("keeper")
    assert r.returncode == 3
    assert "postgres is not up" in r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    # real PATH for this test only (shutil.which above + inherited os.environ below)
    r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
