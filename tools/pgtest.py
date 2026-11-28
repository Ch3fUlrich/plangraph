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


def affected_summary(batches):
    """(exit code, final line) of a run: the first failing batch's code, one line a machine can read."""
    failed = [code for _, code in batches if code]
    code = failed[0] if failed else 0
    return code, f"AFFECTED rc={code} batches={len(batches)} failed={len(failed)}"


_REPORTED = []


def report_batches(batches):
    """Print the result line last (readers never infer it from the tail of pytest output); return the exit code."""
    code, line = affected_summary(batches)
    _REPORTED.append(True)
    print(line, flush=True)
    return code


def run_flag_in(options):
    """Whether argv tokens spell `--run` (or its accepted abbreviation `--ru`); nothing after `--` is a flag."""
    if "--" in options:
        options = options[: options.index("--")]
    return any(len(arg) >= 4 and "--run".startswith(arg) for arg in options)


def with_result_line(run, wants_run):
    """Run `run()`; a `--run` that ends without any batch reported (a refusal, an argparse exit, a
    collection-only run, an operational error) still prints the one AFFECTED line, nonzero on any
    failure. `wants_run()` is the one run-mode decision, taken from the parser itself."""
    _REPORTED.clear()

    def finish(code):
        if not _REPORTED and wants_run():
            print(f"AFFECTED rc={code} batches=0 failed={1 if code else 0}", flush=True)

    try:
        code = run()
    except SystemExit as exit_:
        finish(exit_.code if isinstance(exit_.code, int) else int(exit_.code is not None))
        raise
    except Exception:
        finish(1)
        raise
    finish(code)
    return code


def build_fleet_spawnd(root, env):
    """Build pg-spawnd from the selected checkout (a push preflight runs in a detached one); a failing exit code, else None."""
    print('fleet tests: building pg-spawnd from the selected checkout', flush=True)
    try:
        build = subprocess.run(
            ['cargo', 'build', '--locked', '--manifest-path', 'rust/Cargo.toml', '-p', 'pg-spawnd'],
            cwd=root, env=env)
    except OSError as error:
        print(f'CARGO FAIL: could not prepare pg-spawnd ({error})', file=sys.stderr)
        return 1
    return build.returncode or None


def affected_parser(description):
    """The argument parser of tools/affected_tests.py (here so the run-mode decision and `_main` share it)."""
    import argparse

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", help="committed comparison endpoint; exclude local changes")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--fleet-spawnd-prebuilt", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--collect-only", action="store_true", help="validate the unsharded selection without running tests")
    parser.add_argument("--local", action="store_true", help="follow shared-asset consumers; refuse full-suite execution")
    parser.add_argument("--files-only", action="store_true", help="local hook: explicit affected files and bounded suite-control checks; never collect the whole tree")
    parser.add_argument("--loud", action="store_true", help="pytest's default report, no quiet flags")
    parser.add_argument("--keep-local-bound", action="store_true", help="keep the local test bound for spawner/planner core changes (the pre-push hook has a fixed deadline)")
    parser.add_argument("--parts-union", action="store_true", help="a pushed batch or train selects the union of every part's changed files, not only the files parts share")
    parser.add_argument("--maxfail", type=int, default=0)
    parser.add_argument("--changed", action="append", help="explicit sample path (repeatable); bypass git diff")
    parser.add_argument("-n", "--workers", default="auto")
    parser.add_argument("--within", nargs="+", action="extend", help="restrict selection to given paths")
    parser.add_argument("--root", default=None, help="repository root")
    return parser


def flake_labels(failed, first_rerun, second_rerun, hashes, parallel=True):
    """FLAKY needs an explicit pass on both reruns and one tree hash around every run.

    FAILED-IN-PARALLEL needs workers that actually ran (parallel); a serial sequence has none."""
    stable = None not in hashes and len(set(hashes)) == 1
    lines = []
    for nodeid in failed:
        low, high = first_rerun.get(nodeid), second_rerun.get(nodeid)
        if "passed" in (low, high) and not stable:
            lines.append(f"FAILED-THEN-PASSED: {nodeid}")
        elif parallel and low == "passed" and high == "failed":
            lines.append(f"FAILED-IN-PARALLEL: {nodeid}")
        elif low == high == "passed":
            lines.append(f"FLAKY (rerun -n0; a parallel race is not excluded): {nodeid}")
    return lines


def flaky_only(failed, timed_out, labels, maxfail):
    """True when every failure is FLAKY, none timed out and --maxfail did not cut the run short."""
    flaky = [line for line in labels if line.startswith("FLAKY ")]
    stopped = bool(maxfail) and len(failed) + len(timed_out) >= maxfail
    return bool(failed) and not timed_out and not stopped and len(flaky) == len(failed)
