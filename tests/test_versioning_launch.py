"""Launch identity and status regressions; no Temporal server required."""
import importlib.util
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/devstack.sh"


def test_devstack_worker_launch_identity(tmp_path):
    worker = tmp_path / "worker"
    worker.write_text('#!/bin/bash\nprintf "%s\\n" "$PG_DEPLOYMENT" "$PG_BUILD_ID" "$PG_TEMPORAL_ADDR"\n')
    worker.chmod(0o755)
    env = dict(os.environ, DEVSTACK_STATE=str(tmp_path / "state"),
               DEVSTACK_DEPLOYMENT="launch-d", DEVSTACK_BUILD_ID="launch-b",
               TEMPORAL_PORT="7233", PG_WORKER_BIN=str(worker))
    result = subprocess.run(["bash", str(SCRIPT), "worker"], env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["launch-d", "launch-b", "http://127.0.0.1:7233"]


def test_start_propagates_promotion_failure_in_conditional(tmp_path):
    # Calling in a conditional disables errexit inside the function: the caller
    # must still return failure after bootstrapping both services for promotion.
    source = SCRIPT.read_text().split('case "${1:-}" in')[0]
    harness = source + '''
resolve_pg_mode() { return 0; }
start_temporal() { echo temporal-started; }
start_set_current() { echo promotion-failed; return 1; }
start_postgres() { echo postgres-started; }
if cmd_start; then exit 0; else exit 1; fi
'''
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env=dict(os.environ, DEVSTACK_STATE=str(tmp_path)))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "temporal-started", "postgres-started", "promotion-failed",
    ]


def test_ci_worker_launch_supplies_deployment(tmp_path):
    spec = importlib.util.spec_from_file_location("launch_util", ROOT / "tests/e2e/rtp1_local_util.py")
    util = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(util)
    binary = tmp_path / "worker"
    binary.write_text('#!/bin/bash\nprintf "%s\\n" "$PG_DEPLOYMENT" "$PG_BUILD_ID"\n')
    binary.chmod(0o755)
    for deployment in (None, "custom"):
        env = {"PG_BUILD_ID": "ci-build", "PG_JAIL": "off"}
        if deployment:
            env["PG_DEPLOYMENT"] = deployment
        worker = util.WorkerProc(str(binary), env, tmp_path / "worker.log")
        try:
            assert worker.proc.wait(timeout=5) == 0
            assert worker.text().splitlines() == [deployment or "pg-worker", "ci-build"]
        finally:
            worker.stop()
