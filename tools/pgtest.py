#!/usr/bin/env python3
"""Run tests in a bounded Linux slice or a Windows kill-on-close job."""
import importlib.util
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


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


KEEPER_TEST_RE = re.compile(r"^tests/test_keeper_[^/]+\.py$")
POSTGRES_BIN_GLOBS = ("/usr/lib/postgresql/*/bin", "/usr/local/pgsql/bin", "/opt/homebrew/opt/postgresql@16/bin")


def keeper_postgres_files(root, changed):
    """Changed or added tests/test_keeper_* files that still exist: these run on Postgres too."""
    return sorted(p for p in dict.fromkeys(Path(c).as_posix() for c in changed)
                  if KEEPER_TEST_RE.match(p) and (Path(root) / p).is_file())


def postgres_bin(env, search=POSTGRES_BIN_GLOBS):
    """The directory with initdb and pg_ctl: PG_BIN, then PATH, then the usual install places."""
    names = ("initdb", "pg_ctl", "createdb")
    candidates = [Path(env["PG_BIN"])] if env.get("PG_BIN") else []
    candidates += [Path(d) for d in env.get("PATH", "").split(os.pathsep) if d]
    for pattern in search:
        candidates += sorted(Path("/").glob(pattern.lstrip("/")), reverse=True)
    for directory in candidates:
        if all(shutil.which(name, path=str(directory)) for name in names):
            return directory
    return None


def run_keeper_postgres(root, changed, env, *, runner=subprocess.run, search=POSTGRES_BIN_GLOBS):
    """Run the changed keeper test files with PG_KEEPER_BACKEND=postgres, as the CI job does.

    An existing PG_DSN is used as is; else a throwaway local cluster is started and stopped. A host
    without the Postgres binaries says so and passes: CI is then the first Postgres run."""
    files = keeper_postgres_files(root, changed)
    if not files:
        return 0
    command = [sys.executable, "-m", "pytest", *files]
    pg_env = dict(env, PG_KEEPER_BACKEND="postgres")
    if env.get("PG_DSN"):
        return runner(command, cwd=root, env=pg_env).returncode
    directory = postgres_bin(env, search)
    if directory is None:
        print("keeper-postgres: SKIPPED: no Postgres binaries on this host (set PG_BIN or PG_DSN); "
              f"{len(files)} keeper test file(s) run on Postgres first in CI", flush=True)
        return 0
    scratch = Path(tempfile.mkdtemp(prefix="pgkeeper-"))
    tool = lambda name: str(Path(shutil.which(name, path=str(directory))))  # noqa: E731
    try:
        subprocess.run([tool("initdb"), "-D", str(scratch / "data"), "-U", "postgres", "--auth=trust"],
                       check=True, capture_output=True)
        subprocess.run([tool("pg_ctl"), "-D", str(scratch / "data"), "-w", "-o",
                        f"-c listen_addresses='' -c unix_socket_directories='{scratch}'", "-l",
                        str(scratch / "log"), "start"], check=True, capture_output=True)
        try:
            subprocess.run([tool("createdb"), "-h", str(scratch), "-U", "postgres", "keeper"],
                           check=True, capture_output=True)
            pg_env["PG_DSN"] = f"postgresql://postgres@/keeper?host={scratch}"
            print(f"keeper-postgres: {len(files)} keeper test file(s) on a throwaway cluster", flush=True)
            return runner(command, cwd=root, env=pg_env).returncode
        finally:
            subprocess.run([tool("pg_ctl"), "-D", str(scratch / "data"), "-m", "immediate", "stop"],
                           capture_output=True)
    except subprocess.CalledProcessError as exc:
        print(f"keeper-postgres: FAIL: cannot start a throwaway cluster: {exc.cmd[0]}", flush=True)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
