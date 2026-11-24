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


def run_keeper_postgres(root, changed, env, *, runner=subprocess.run, search=POSTGRES_BIN_GLOBS,
                        nodes=None):
    """Run the changed keeper test files with PG_KEEPER_BACKEND=postgres, as the CI job does.

    An existing PG_DSN is used as is. The optional throwaway cluster needs both
    the Postgres binaries and the psycopg driver in this Python environment."""
    files = keeper_postgres_files(root, changed)
    if not files:
        return 0
    command = [sys.executable, "-m", "pytest", *(files if nodes is None else nodes)]
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


# Controller-only outcome recorder (xdist workers carry workerinput and stay silent),
# written to a temp dir and loaded with -p; .pytest_cache is never read.
OUTCOME_PLUGIN = """import json, os
_outcomes = {}
_phases = {}
_config = None


def pytest_configure(config):
    global _config
    _config = config


def pytest_runtest_logreport(report):
    if hasattr(_config, "workerinput"):
        return
    if os.environ.get("AFFECTED_EXACT_REPLAY"):
        _phases.setdefault(report.nodeid, {})[report.when] = report.outcome
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif _outcomes.get(report.nodeid) != "failed":
        if report.when == "call" and report.passed:
            _outcomes[report.nodeid] = "passed"
        elif report.skipped:
            _outcomes[report.nodeid] = "skipped"


def pytest_sessionfinish(session):
    if not hasattr(session.config, "workerinput"):
        # xdist's scheduler exists only when workers were really started.
        workers = session.config.pluginmanager.get_plugin("dsession") is not None
        with open(os.environ["AFFECTED_OUTCOMES"], "w") as out:
            data = {"outcomes": _outcomes, "workers": workers}
            if os.environ.get("AFFECTED_EXACT_REPLAY"):
                data.update(phases=_phases, collected=[item.nodeid for item in session.items])
            json.dump(data, out)
"""


def build_fleet(root, env, *, capture_output=False):
    return subprocess.run(
        ['cargo', 'build', '--locked', '--manifest-path', 'rust/Cargo.toml', '-p', 'pg-spawnd'],
        cwd=root, env=env, capture_output=capture_output)


def stage_fleet(root, scratch, env):
    staged = str(Path(scratch) / ('pg-spawnd.exe' if os.name == 'nt' else 'pg-spawnd'))
    result = subprocess.run([sys.executable, 'tests/_spawnd.py', staged], cwd=root, env=env)
    if not result.returncode:
        env["PLANGRAPH_TEST_FLEET_SPAWND"] = staged
    return result.returncode


def _replay_functions(path):
    import ast

    def is_fixture(decorators):
        for decorator in decorators:
            if isinstance(decorator, ast.Call):
                decorator = decorator.func
            if isinstance(decorator, ast.Attribute) and decorator.attr == "fixture":
                return True
            if isinstance(decorator, ast.Name) and decorator.id == "fixture":
                return True
        return False

    def tests(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                if not is_fixture(node.decorator_list):
                    yield prefix + node.name, ast.dump(node)
            elif isinstance(node, ast.ClassDef):
                yield from tests(node.body, prefix + node.name + "::")

    return dict(tests(ast.parse(path.read_bytes()).body)) if path.is_file() else {}


def _replay_observation(data, code, summary, node):
    phases = data.get("phases", {}).get(node, {})
    call = phases.get("call")
    if (code in (0, 1) and data.get("collected", []).count(node) == 1
            and phases.get("setup") == phases.get("teardown") == "passed"
            and call in ("passed", "failed")):
        return dict(outcome=call, summary=summary)
    if "failed" in (phases.get("setup"), phases.get("teardown")):
        reason = "setup or teardown error"
    elif "skipped" in phases.values():
        reason = "skipped"
    else:
        reason = "collection error or zero exact matches"
    return dict(outcome="inconclusive", summary=f"{reason}; {summary}",
                inactive=(reason == "skipped"))


def _replay_pytest(root, options, file, backend, nodes=None, *, fleet=None):
    env = test_environment(root, os.environ)
    for key in ("PYTEST_SHARD", "PYTEST_QUARANTINE_FILE", "PYTEST_ADDOPTS",
                "PLANGRAPH_TEST_FLEET_SPAWND"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PG_KEEPER_BACKEND"] = backend
    with affected_pytest() as (scratch, run):
        if file == "tests/test_fleet_snapshot.py":
            if fleet is None or not Path(fleet).is_file():
                return {}, 2, "fleet binary build or staging failed"
            env["PLANGRAPH_TEST_FLEET_SPAWND"] = fleet
        outcomes = Path(scratch, "outcomes.json")
        Path(scratch, "affected_outcomes.py").write_text(OUTCOME_PLUGIN)
        env.update(AFFECTED_OUTCOMES=str(outcomes), AFFECTED_EXACT_REPLAY="1",
                   PYTHONPATH=os.pathsep.join((scratch, env["PYTHONPATH"])))
        result = None

        def invoke(command, **kwargs):
            nonlocal result
            result = run([*command[:3], "-p", "affected_outcomes", "-q", "--color=no",
                          *options, "--", *command[3:]],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
            return result

        if backend == "postgres":
            code = run_keeper_postgres(root, [file], env, runner=invoke, nodes=nodes or [file])
        else:
            code = invoke([sys.executable, "-m", "pytest", *(nodes or [file])], cwd=root, env=env).returncode
        if result is None:
            return {}, code, "Postgres replay unavailable"
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        summary = lines[-1] if lines else f"pytest exited {code}"
        record = json.loads(outcomes.read_text()) if outcomes.is_file() else {}
        return record, code, summary


def exact_replay(base, head, output, *paths):
    """Replay each changed pytest node on both commits using the required backend(s)."""
    base, head = Path(base), Path(head)
    rows = []
    base_fleet = os.environ.get("PLANGRAPH_REPLAY_BASE_FLEET")
    head_fleet = os.environ.get("PLANGRAPH_REPLAY_HEAD_FLEET")
    for file in paths:
        source, destination = head / file, base / file
        if source.resolve() != source or destination.resolve() != destination:
            raise ValueError(f"test replay refuses a symlink: {file}")
        before = _replay_functions(destination)
        changed = {file + "::" + name for name, body in _replay_functions(source).items()
                   if before.get(name) != body}
        if not changed:
            continue
        original = destination.read_bytes() if destination.is_file() else None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        try:
            backends = ("sqlite", "postgres") if keeper_postgres_files(head, [file]) else ("sqlite",)
            for backend in backends:
                collected, collect_code, collect_summary = _replay_pytest(
                    head, ["--collect-only"], file, backend, fleet=head_fleet)
                by_function = {prefix: [] for prefix in changed}
                for node in collected.get("collected", []):
                    if node.split("[", 1)[0] in by_function and collect_code == 0:
                        by_function[node.split("[", 1)[0]].append(node)
                nodes = sorted({node for ids in by_function.values() for node in ids})
                if nodes:
                    # The exact node arguments select even parametrized items independently.
                    head_result = _replay_pytest(head, [], file, backend, nodes, fleet=head_fleet)
                    base_result = _replay_pytest(base, [], file, backend, nodes, fleet=base_fleet)
                for prefix, ids in sorted(by_function.items()):
                    for node in ids or [prefix]:
                        absent = dict(outcome="inconclusive", summary=f"zero exact matches; {collect_summary}",
                                  inactive=bool(collected) and collect_code in (0, 5))
                        rows.append(dict(id=node, path=file, backend=backend,
                                         base=_replay_observation(*base_result, node) if ids else absent,
                                         head=_replay_observation(*head_result, node) if ids else absent))
        finally:
            if original is None:
                destination.unlink()
            else:
                destination.write_bytes(original)
    Path(output).write_text(json.dumps(rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--exact-replay"]:
        sys.exit(exact_replay(*sys.argv[2:]))
    if sys.argv[1:2] == ["--replay-fleet"]:
        root = Path.cwd()
        env = test_environment(root, os.environ)
        code = build_fleet(root, env, capture_output=True).returncode
        sys.exit(code or stage_fleet(root, sys.argv[2], env))
    sys.exit(main())
