#!/usr/bin/env python3
"""Run tests in a bounded Linux slice or a Windows kill-on-close job."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _job_api():
    try:
        from plangraph.spawner.winjob import WinJobApi
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "src/plangraph/spawner/winjob.py"
        spec = importlib.util.spec_from_file_location("pgtest_winjob", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        WinJobApi = module.WinJobApi
    return WinJobApi()


def main(argv=None, *, platform=None, job_api=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        print("usage: pgtest <command> [args ...]", file=sys.stderr)
        return 2
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        api = job_api if job_api is not None else _job_api()
        job = api.create_job()
        child = None
        try:
            child = subprocess.Popen(command)
            api.assign(job, child.pid)
            return child.wait()
        finally:
            api.close(job)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
    args = ["nice", "-n", "10"]
    if platform.startswith("linux") and shutil.which("systemd-run"):
        args += ["systemd-run", "--user", "--scope", "-q",
                 "--slice=agent-tests.slice", "-p",
                 "MemoryMax=" + os.environ.get("PGTEST_MEMORY_MAX", "2G"), "--"]
    else:
        print("pgtest: warning: systemd-run unavailable; running under nice only", file=sys.stderr)
    os.execvp(args[0], args + command)


if __name__ == "__main__":
    sys.exit(main())
