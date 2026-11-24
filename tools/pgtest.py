#!/usr/bin/env python3
"""Run tests in a bounded Linux slice or a Windows kill-on-close job."""
import asyncio
from contextlib import contextmanager
import importlib.util
from itertools import count
import json
import os
import re
from pathlib import Path
import shutil
import signal
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


def run_test_process(command, *, taskkill_timeout, **kwargs):
    """Kill the command's tree on timeout/interruption, including nested Linux sessions."""
    timeout = kwargs.pop('timeout')
    input_text = kwargs.pop('input', None)
    if input_text is not None:
        kwargs['stdin'] = subprocess.PIPE
    tree = None
    if sys.platform.startswith('linux'):
        # Reuse checkout-local helpers without requiring an installed package.
        spec = importlib.util.spec_from_file_location(
            'preflight_proc_kill', Path(__file__).resolve().parents[1]
            / 'src/plangraph/spawner/proc_kill.py')
        tree = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tree)
    with subprocess.Popen(command, **kwargs, start_new_session=os.name == 'posix') as process:
        ident = tree.root_identity(process.pid) if tree else None
        try:
            if input_text is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except BaseException:
            if tree:
                members = {}
                try:
                    os.killpg(process.pid, signal.SIGSTOP)
                    # Freeze discovered sessions before rescanning: their children
                    # must remain attached until every potential parent is stopped.
                    while True:
                        added = tree.snapshot(process.pid, ident).items() - members.items()
                        if not added:
                            break
                        for pid, start in added:
                            if tree.alive(pid, start):
                                try:
                                    os.kill(pid, signal.SIGSTOP)
                                except ProcessLookupError:
                                    pass
                        members.update(added)
                    asyncio.run(tree.finish(process.pid, ident, members))
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            elif os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                               capture_output=True, timeout=taskkill_timeout)
                process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def selected_count(partial):
    """Selected test count from the selector's first output line (JSON plan), else "unknown"."""
    if isinstance(partial, bytes):
        partial = partial.decode("utf-8", "replace")
    try:
        return len(json.loads((partial or "").splitlines()[0])["tests"])
    except (ValueError, KeyError, IndexError, TypeError):
        return "unknown"


def test_environment(root, env, *, timeout=30):
    """A pre-push caller's Git context must not select a fixture's repository."""
    local = set(subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], cwd=root, env=env,
        text=True, stderr=subprocess.DEVNULL, timeout=timeout).upper().splitlines())
    local.update({"GIT_PREFIX", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                  "GIT_DISCOVERY_ACROSS_FILESYSTEM"})
    return {key: value for key, value in env.items()
            if key.upper() not in local
            and not key.upper().startswith("GIT_CONFIG")}


@contextmanager
def affected_pytest():
    """Allocate a scratch root and a distinct basetemp for each pytest process."""
    temporary = tempfile.TemporaryDirectory(prefix="pg-affected-")
    with temporary as scratch:
        with open(Path(scratch) / ".lease", "xb") as lease:
            inherited = {}
            if os.name == "posix":
                import fcntl
                fcntl.flock(lease, fcntl.LOCK_EX)
                # A surviving pytest controller still protects its scratch if the selector dies.
                inherited["pass_fds"] = (lease.fileno(),)
            lease.write(b"plangraph-affected-pytest-v1\n")
            lease.flush()
            attempts = count()

            def run(command, **kwargs):
                basetemp = str(Path(scratch) / f"pytest-{next(attempts)}")
                return subprocess.run([*command[:3], "--basetemp", basetemp, *command[3:]],
                                      **inherited, **kwargs)

            try:
                yield scratch, run
            finally:
                # Keep the lease held until the scratch files have been removed.
                if inherited:
                    temporary.cleanup()


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

    An existing PG_DSN is used as is. The optional throwaway cluster needs both
    the Postgres binaries and the psycopg driver in this Python environment."""
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
    if importlib.util.find_spec("psycopg") is None:
        print("keeper-postgres: SKIPPED: Python cannot find psycopg (install 'psycopg[binary]'); "
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


def deliver_landing_stage(stage, started_ms, ended_ms, result, detail=""):
    """Report one push-gate stage to the keeper's landing timeline when a lander named its batch.

    The lander sets PG_LANDING_REPO, PG_LANDING_BATCH and PG_LANDING_STAGE_HEAD for its push. A failed
    delivery only prints: timing never blocks a push. ``detail`` names a sub-stage (a preflight family)."""
    import socket

    repo, batch, head = (os.environ.get(name) for name in
                         ("PG_LANDING_REPO", "PG_LANDING_BATCH", "PG_LANDING_STAGE_HEAD"))
    if not (repo and batch and head):
        return
    payload = {"action": "stage", "repo": repo, "batch_key": batch, "head": head, "stage": stage,
               "attempt": started_ms, "started_at_ms": started_ms, "ended_at_ms": max(started_ms, ended_ms),
               "host": socket.gethostname()[:64], "result": result, "detail": detail}
    try:
        receipt = subprocess.run(["plangraph", "landing", "journal"], input=json.dumps(payload),
                                 text=True, stdout=subprocess.DEVNULL, timeout=35)
        failed = receipt.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:
        print("landing timing pending keeper delivery", file=sys.stderr)
