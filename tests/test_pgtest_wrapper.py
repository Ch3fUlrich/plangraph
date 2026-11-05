"""Hermetic wrapper checks, including an injected Windows job API."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "tools/pgtest.py"


@pytest.mark.parametrize("memory", [None, "768M"])
def test_systemd(tmp_path, memory):
    fake = tmp_path / "systemd-run"
    fake.write_text("#!" + sys.executable + "\nimport json, sys\n"
                    "print(json.dumps(sys.argv[1:]))\nsys.exit(37)\n")
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.defpath)
    env.pop("PGTEST_MEMORY_MAX", None)
    if memory:
        env["PGTEST_MEMORY_MAX"] = memory
    result = subprocess.run([sys.executable, str(WRAPPER), "pytest", "-q"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 37
    assert json.loads(result.stdout) == ["--user", "--scope", "-q",
        "--slice=agent-tests.slice", "-p", "MemoryMax=" + (memory or "2G"),
        "--", "pytest", "-q"]
    assert result.stderr == ""


def load_wrapper():
    spec = importlib.util.spec_from_file_location("pgtest", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_systemd(tmp_path):
    nice = tmp_path / "nice"
    nice.symlink_to("/usr/bin/nice")
    result = subprocess.run([sys.executable, str(WRAPPER), sys.executable,
                             "-c", "raise SystemExit(19)"],
                            env=dict(os.environ, PATH=str(tmp_path)),
                            capture_output=True, text=True)
    assert result.returncode == 19
    assert result.stderr.count("warning:") == 1


def test_no_args():
    result = subprocess.run([sys.executable, str(WRAPPER)], capture_output=True)
    assert result.returncode == 2
    assert b"usage:" in result.stderr


def test_windows():
    events = []

    class JobApi:
        def create_job(self):
            events.append("create")
            return 123

        def assign(self, job, pid):
            assert job == 123 and pid > 0
            events.append("assign")

        def close(self, job):
            assert job == 123
            events.append("close")

    assert load_wrapper().main([sys.executable, "-c", "raise SystemExit(23)"],
                              platform="win32", job_api=JobApi()) == 23
    assert events == ["create", "assign", "close"]
