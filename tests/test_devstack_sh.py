"""tools/devstack.sh: hermetic start/status/stop/keeper coverage with fake temporal+docker."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "devstack.sh"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="devstack.sh is a posix bash script")

TEMPORAL_FAKE = """\
#!/bin/bash
TMP="{tmp}"
{{ printf 'temporal'; for a in "$@"; do printf ' %s' "$a"; done; printf '\\n'; }} >>"$TMP/calls.log"
case "${{1:-}}" in
  server) exec sleep 300 ;;
  operator) exit 0 ;;
  worker)
    if [ "${{FAKE_SETCUR_FAIL:-}}" = 1 ]; then exit 1; fi
    exit 0 ;;
esac
exit 0
"""

DOCKER_FAKE = """\
#!/bin/bash
TMP="{tmp}"
{{ printf 'docker'; for a in "$@"; do printf ' %s' "$a"; done; printf '\\n'; }} >>"$TMP/calls.log"
case "${{1:-}}" in
  info) exit 0 ;;
  ps) if [ -e "$TMP/container" ]; then printf 'abc123\\n'; fi; exit 0 ;;
  run) touch "$TMP/container"; exit 0 ;;
  start|exec) exit 0 ;;
  rm) rm -f "$TMP/container"; exit 0 ;;
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
                   f"--headless --db-filename {stack.state}/temporal.db"]
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


def test_stop_kills_temporal_and_removes_container_but_keeps_state(stack):
    stack.state.mkdir(parents=True)
    (stack.state / "temporal.db").write_text("SENTINEL\n")
    assert stack.run("start").returncode == 0
    pid = int((stack.state / "temporal.pid").read_text().strip())
    assert alive(pid)
    assert stack.run("stop").returncode == 0
    assert not alive(pid)
    assert "docker rm -f devstack-test-pg" in stack.lines()
    assert (stack.state / "temporal.db").read_text() == "SENTINEL\n"
    assert stack.run("stop").returncode == 0


def test_set_current_failure_does_not_fail_start(stack):
    # the script retries 5x with 2 s sleeps; allow headroom
    r = stack.run("start", timeout=40, extra_env={"FAKE_SETCUR_FAIL": "1"})
    assert r.returncode == 0
    assert "WARNING: set-current-version failed" in r.stderr


def test_bogus_pg_mode_exits_2(stack):
    r = stack.run("start", extra_env={"DEVSTACK_PG_MODE": "bogus"})
    assert r.returncode == 2
    assert "DEVSTACK_PG_MODE" in r.stderr


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
