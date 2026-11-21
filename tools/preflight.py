#!/usr/bin/env python3
"""Blocking local CI policy, Rust and affected-test preflight."""
import argparse
import asyncio
import importlib.util
import json
import math
import os
import re
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from plangraph.spawner.host_ready import (cargo_child_env, cargo_lease, disk_free_gb,
                                          cargo_cap_refusal, running_disk_refusal, shared_cargo_target)
from plangraph.spawner.overflow import available_memory_mb

CHECK_TIMEOUT = 60
# Measured on a build host: pg-spawnd clippy 17.8 s, shim/windows_pipe tests 48.4 s.
# Leave ample room for slower hosts, multiple affected crates and test suites.
RUST_TIMEOUT = 600
# The affected-test step is bounded well under the 5 minute push target; the
# measured normal run is about 50 s.
TEST_TIMEOUT = 240
# A large selection (spawner core selects hundreds of test paths) scales the bound:
# paths x PER_PATH seconds, never below TEST_TIMEOUT and capped at MAX_TEST_TIMEOUT.
TEST_PER_PATH_TIMEOUT = 2
MAX_TEST_TIMEOUT = 1200
# pytest-xdist workers of the affected-test step: half the cores, at most one per
# TEST_WORKER_GIB of available memory (the spawner's probe, Linux and Windows), between 2 and
# MAX_TEST_WORKERS; cores only when memory cannot be read; PREFLIGHT_TEST_WORKERS overrides.
TEST_WORKER_GIB = 2
MAX_TEST_WORKERS = 16


def affected_workers(env=None, cores=None, memory_mb=None):
    configured = (env if env is not None else os.environ).get("PREFLIGHT_TEST_WORKERS", "")
    try:
        if int(configured) > 0:
            return int(configured)
    except ValueError:
        pass
    cores = cores if cores is not None else (os.cpu_count() or 2)
    try:
        by_mem = int((memory_mb or available_memory_mb)() // (TEST_WORKER_GIB * 1024))
    except (OSError, ValueError):
        by_mem = cores
    return max(2, min(cores // 2, by_mem, MAX_TEST_WORKERS))


def is_affected_step(command):
    return len(command) > 1 and Path(command[1]).name == "affected_tests.py"


def planned_paths(command, cwd, env):
    """Count of test paths the selector plans for this real (--run) command, else None."""
    if "--run" not in command:
        return None
    try:
        done = subprocess.run([part for part in command if part != "--run"], cwd=cwd, env=env, text=True,
                              capture_output=True, timeout=CHECK_TIMEOUT)
        count = selected_count(done.stdout)
    except (OSError, subprocess.SubprocessError):
        return None
    return count if isinstance(count, int) else None


def scaled_test_timeout(count):
    if count is None:
        return TEST_TIMEOUT
    return min(MAX_TEST_TIMEOUT, max(TEST_TIMEOUT, count * TEST_PER_PATH_TIMEOUT))


def check_timeout(command, cwd=None, env=None):
    if command[:2] in (["cargo", "clippy"], ["cargo", "test"]):
        return RUST_TIMEOUT
    if is_affected_step(command):
        return scaled_test_timeout(planned_paths(command, cwd, env))
    return CHECK_TIMEOUT


class SkipFamily(ValueError):
    """A check has no usable revision baseline."""


class FailFamily(ValueError):
    """A check cannot run because a required tool is missing."""


def run_test_process(command, **kwargs):
    """Kill the command's tree on timeout/interruption, including nested Linux sessions."""
    timeout = kwargs.pop('timeout')
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
            stdout, stderr = process.communicate(timeout=timeout)
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
                               capture_output=True, timeout=CHECK_TIMEOUT)
                process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run(command, cwd, env):
    timeout = check_timeout(command, cwd, env)
    try:
        runner = run_test_process if is_affected_step(command) else subprocess.run
        result = runner(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired as exc:
        if is_affected_step(command):
            return 1, (f"tests: affected-tests step exceeded its {timeout} second bound "
                       f"({selected_count(exc.stdout)} selected test paths)\n")
        return 1, f"check exceeded {timeout} second timeout\n"
    except OSError as exc:
        return 1, f"unable to start check: {type(exc).__name__}\n"


def selected_count(partial):
    """Selected test count from the selector's first output line (JSON plan), else "unknown"."""
    if isinstance(partial, bytes):
        partial = partial.decode("utf-8", "replace")
    try:
        return len(json.loads((partial or "").splitlines()[0])["tests"])
    except (ValueError, KeyError, IndexError, TypeError):
        return "unknown"


def first_error(output):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    marked = [line for line in lines if re.search(r"FAIL|error|exceeded", line, re.I)]
    return (marked or lines or ["no output"])[0]


def rust_path_dependencies(manifest, data, workspace_dir, workspace):
    """Resolve normal, dev, build and target-specific Cargo path dependencies."""
    dependencies = set()
    sections = [data, *data.get('target', {}).values()]
    for section in sections:
        for kind in ('dependencies', 'dev-dependencies', 'build-dependencies'):
            for name, dependency in section.get(kind, {}).items():
                if not isinstance(dependency, dict):
                    continue
                directory = manifest.parent
                if dependency.get('workspace'):
                    dependency = (workspace or {}).get('dependencies', {}).get(name, {})
                    directory = workspace_dir
                if isinstance(dependency, dict) and 'path' in dependency:
                    dependencies.add((directory / dependency['path']).resolve())
    return dependencies


def rust_commands(root, base, head, env=None):
    root = root.resolve()
    try:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
            cwd=root, text=True, stderr=subprocess.DEVNULL,
            timeout=CHECK_TIMEOUT).splitlines()
    except subprocess.CalledProcessError as exc:
        raise SkipFamily("revision baseline unavailable or no merge-base") from exc
    changed = [p for p in changed if p.startswith("rust/") or p == "rust-toolchain.toml"]
    if not changed:
        raise SkipFamily("no rust changes")
    available = shutil.which("cargo") if env is None else shutil.which("cargo", path=env.get('PATH', ''))
    if not available:
        raise FailFamily("CARGO FAIL: cargo is required for Rust checks")
    workspace_file = root / 'rust/Cargo.toml'
    workspace = tomllib.loads(workspace_file.read_text()).get('workspace') if workspace_file.is_file() else None
    members = None
    if workspace is not None:
        members = {path.resolve() for pattern in workspace.get('members', [])
                   for path in (root / 'rust').glob(pattern)
                   if not any(path.relative_to(root / 'rust').match(excluded)
                              for excluded in workspace.get('exclude', []))}
        if 'package' in tomllib.loads(workspace_file.read_text()):
            members.add(workspace_file.parent.resolve())
    manifests, standalone, crates = {}, {}, {}
    for manifest in (root / "rust").rglob("Cargo.toml"):
        if "target" in manifest.parts:
            continue
        data = tomllib.loads(manifest.read_text())
        owner = manifest.parent.relative_to(root).as_posix()
        if members is not None and manifest.parent not in members:
            # Excluded packages can stand alone without a [workspace] table.
            # Address their owning manifest, never a package in rust/.
            if "package" not in data:
                continue
            standalone[owner] = manifest
        elif "package" in data:
            manifests[owner] = data["package"]["name"]
        else:
            continue
        crates[owner] = rust_path_dependencies(
            manifest, data, manifest.parent if owner in standalone else workspace_file.parent,
            data.get('workspace') if owner in standalone else workspace)
    selected = set()
    for path in changed:
        owners = [p for p in crates if path.startswith(p + "/")]
        selected.update([max(owners, key=len)] if owners else manifests)
    # A changed dependency requires checking every consumer, across workspaces.
    while True:
        directories = {root / owner for owner in selected}
        dependents = {owner for owner, deps in crates.items() if deps & directories}
        if dependents <= selected:
            break
        selected.update(dependents)
    packages = [arg for p in sorted(selected & manifests.keys()) for arg in ("-p", manifests[p])]
    commands = ([["cargo", "fmt", "--check"],
                 ["cargo", "clippy", *packages, "--all-targets", "--", "-D", "warnings"],
                 ["cargo", "test", *packages]] if packages else [])
    for owner in sorted(selected & standalone.keys()):
        target = ["--manifest-path", str(standalone[owner])]
        if owner == 'rust/spikes/temporal-spike':
            # This experiment's CI contract is replay only (tests.yml rust-spike).
            commands.append(["cargo", "test", *target, "--test", "replay"])
        else:
            # Worker integration tests require a real Temporal server/CLI. CI
            # supplies those through ci-temporal; local checks stay offline.
            tests = ["--lib", "--bins"] if owner == 'rust/worker' else []
            commands += [["cargo", "fmt", *target, "--check"],
                         ["cargo", "clippy", *target, "--all-targets", "--", "-D", "warnings"],
                         ["cargo", "test", *target, *tests]]
    return commands


PROST_LOCK_MARKERS = ('name = "prost-build"', 'name = "prost-wkt-types"')


def needs_protoc(root, commands):
    """True when a cargo build or test targets a manifest whose lock pulls a protobuf build script."""
    for command in commands:
        if command[0] != 'cargo' or command[1:2] == ['fmt']:
            continue
        manifest = (Path(command[command.index('--manifest-path') + 1]) if '--manifest-path' in command
                    else Path(root) / 'rust/Cargo.toml')
        lock = manifest.parent / 'Cargo.lock'
        if lock.is_file() and any(marker in lock.read_text() for marker in PROST_LOCK_MARKERS):
            return True
    return False


def require_protoc(root, commands, env):
    """Resolve protoc (PROTOC, ~/.local/bin, PATH) into env, or fail in a second: a detached unit's PATH
    lacks ~/.local/bin and the build script would fail only after minutes of compiling."""
    if not needs_protoc(root, commands):
        return
    given = env.get('PROTOC')
    if given:
        # Cargo runs in rust/: a relative path is taken from there, a bare name from the child's PATH.
        if '/' in given:
            resolved = Path(given) if Path(given).is_absolute() else Path(root) / 'rust' / given
            found = str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
        else:
            # Relative and empty PATH entries mean the Cargo working directory, not ours; an
            # explicitly empty PATH is one such entry.
            cargo_path = os.pathsep.join(
                entry if entry and os.path.isabs(entry) else str(Path(root) / 'rust' / entry)
                for entry in env['PATH'].split(os.pathsep)) if 'PATH' in env else None
            found = shutil.which(given, path=cargo_path)
        if not found:
            raise FailFamily(f"PROTOC={given} is not an executable file: fix or unset PROTOC")
        env['PROTOC'] = found
        return
    local = Path.home() / '.local/bin/protoc'
    found = str(local) if local.is_file() and os.access(local, os.X_OK) else shutil.which('protoc', path=env.get('PATH', ''))
    if not found:
        raise FailFamily("protoc not found: set PROTOC, or install it in ~/.local/bin or on PATH "
                         "(a detached systemd-run unit needs the login PATH)")
    env['PROTOC'] = found


def test_environment(root, env):
    """A pre-push caller's Git context must not select a fixture's repository."""
    local = set(subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], cwd=root, env=env,
        text=True, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT).upper().splitlines())
    local.update({"GIT_PREFIX", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                  "GIT_DISCOVERY_ACROSS_FILESYSTEM"})
    return {key: value for key, value in env.items()
            if key.upper() not in local
            and not key.upper().startswith("GIT_CONFIG")}


def python_interpreter(root):
    """Prefer this checkout's venv, then PREFLIGHT_PYTHON, then PATH python3."""
    root = Path(root)
    for relative in ('.venv/bin/python', '.venv/Scripts/python.exe'):
        candidate = root / relative
        if candidate.is_file():
            return str(candidate.absolute())
    configured = os.environ.get('PREFLIGHT_PYTHON')
    return str(Path(configured).expanduser().absolute()) if configured else shutil.which('python3') or 'python3'

def test_baseline(root, fallback):
    """Local tests cover this branch's changes, excluding inherited upstream commits."""
    try:
        subprocess.check_output(['git', 'rev-parse', '--verify', 'origin/main^{commit}'],
                                cwd=root, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return fallback
    return 'origin/main'


def policy_baseline(root, fallback):
    """Policy checks include inherited stack changes against origin/main."""
    return test_baseline(root, fallback)


# Per-run Rust target: no shared cargo lock between concurrent pushes.
# The private target is used only when the effective RUSTC_WRAPPER is sccache (a fresh target without it is always cold
# and exceeds RUST_TIMEOUT); otherwise the shared target (PLANGRAPH_CARGO_TARGET_DIR) is kept.
# PLANGRAPH_PREFLIGHT_MIN_FREE_GB (default 20): free GB needed for a private target.
# PLANGRAPH_PREFLIGHT_RUN_DIR: parent of the private target (default: parent of the shared target).
# PLANGRAPH_SCCACHE: sccache binary (default: sccache on PATH); PLANGRAPH_SCCACHE_CACHE_SIZE (default 20G).
MIN_FREE_GB_DEFAULT = "20"
SCCACHE_SIZE_DEFAULT = "20G"
PRIVATE_TARGET_PREFIX = "preflight-cargo-"
STALE_TARGET_SECONDS = 12 * 3600


def prune_stale_targets(parent, now=None):
    """Remove preflight-cargo-* directories older than STALE_TARGET_SECONDS (orphans of killed runs)."""
    now = time.time() if now is None else now
    try:
        entries = list(parent.glob(PRIVATE_TARGET_PREFIX + "*"))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink() and now - entry.stat().st_mtime > STALE_TARGET_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def find_sccache(env):
    search = env.get("PATH") or os.defpath
    named = env.get("PLANGRAPH_SCCACHE")
    found = shutil.which(named, path=search) if named else shutil.which("sccache", path=search)
    if named and not found:
        print(f"rust: PLANGRAPH_SCCACHE={named} not found, no sccache", flush=True)
    return found


def rust_target_env(env, cleanup):
    """Return env for the Rust family: private target when sccache is present and disk allows.

    cleanup is a list that receives the private directory so the caller removes it.
    Never raises: any problem falls back to the shared target. An inherited
    RUSTC_WRAPPER is never replaced.
    """
    out = dict(env)
    sccache = find_sccache(env)
    if not sccache:
        print("rust: shared CARGO_TARGET_DIR (no sccache; a private target would be cold)", flush=True)
        return out
    wrapper = env.get("RUSTC_WRAPPER")
    if not wrapper:
        out["RUSTC_WRAPPER"] = wrapper = sccache
    out["SCCACHE_CACHE_SIZE"] = env.get("PLANGRAPH_SCCACHE_CACHE_SIZE") or env.get(
        "SCCACHE_CACHE_SIZE") or SCCACHE_SIZE_DEFAULT
    if Path(wrapper).name != Path(sccache).name and Path(wrapper).name != "sccache":
        print(f"rust: shared CARGO_TARGET_DIR (RUSTC_WRAPPER={wrapper} is not sccache)", flush=True)
        return out
    raw = env.get("PLANGRAPH_PREFLIGHT_MIN_FREE_GB", MIN_FREE_GB_DEFAULT)
    try:
        try:
            need = float(raw)
        except ValueError:
            need = float("nan")
        if not math.isfinite(need) or need < 0:
            raise ValueError(f"invalid PLANGRAPH_PREFLIGHT_MIN_FREE_GB={raw!r}")
        parent = Path(env.get("PLANGRAPH_PREFLIGHT_RUN_DIR") or shared_cargo_target().parent).expanduser()
        probe = parent
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        free = disk_free_gb(probe)
        if free is None:
            print("rust: shared CARGO_TARGET_DIR (free disk unknown)", flush=True)
        elif free < need:
            print(f"rust: shared CARGO_TARGET_DIR (free {free:.1f} GB < {need:g} GB)", flush=True)
        else:
            parent.mkdir(parents=True, exist_ok=True)
            prune_stale_targets(parent)
            private = tempfile.mkdtemp(prefix=PRIVATE_TARGET_PREFIX, dir=parent)
            cleanup.append(private)
            out["CARGO_TARGET_DIR"] = private
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"rust: shared CARGO_TARGET_DIR ({exc})", flush=True)
    return out


def main(argv=None, root=None):
    cleanup = []
    try:
        return _main(argv, root, cleanup)
    finally:
        for path in cleanup:
            shutil.rmtree(path, ignore_errors=True)


def _main(argv, root, cleanup):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--scrub-base", help="scrub range start when affected tests use a nearer base")
    parser.add_argument("--fast", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--keep-going", action="store_true", help="default behaviour")
    mode.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parent.parent)
    interpreter = python_interpreter(root)
    print(f"preflight Python: {interpreter}", flush=True)
    tools = Path(os.environ.get("PREFLIGHT_TOOLS_DIR", Path(__file__).resolve().parent))
    policy_base = policy_baseline(root, args.base)
    policy_bases = dict(FILESIZE_REQUIRE_BASE=policy_base, NEWCODE_BASE=policy_base,
                        CHANGELOG_BASE=policy_base)
    env = cargo_child_env(dict(os.environ, CARGO_BUILD_JOBS="4"))
    cargo_bin = Path.home() / '.cargo/bin'
    if cargo_bin.is_dir():
        env['PATH'] = str(cargo_bin) + os.pathsep + env.get('PATH', '')
    # Nested test repositories own their revision history, not this checkout's base.
    for key in policy_bases:
        env.pop(key, None)
    policy_env = dict(env, **policy_bases)

    def python(name, *extra):
        return [interpreter, str(tools / name), *extra]

    def invoke(command, cwd, command_env):
        if command[0] != 'cargo':
            return run(command, cwd, command_env)
        # Share the spawner's target, cap checks and cleanup lease.
        with cargo_lease():
            refusal = running_disk_refusal(root) or cargo_cap_refusal()
            if refusal:
                return 1, refusal + '\n'
            code, output = run(command, cwd, command_env)
            refusal = running_disk_refusal(root) or cargo_cap_refusal()
            return (1, output + refusal + '\n') if refusal else (code, output)

    scrub = python("split/scrub_check.py", ".", "--base", args.scrub_base or args.base, "--head", args.head,
                   "--rules", str(tools / "split/manifest.toml"),
                   "--rules", str(tools / "split/scrub_ci.toml"))
    words = os.environ.get("PLANGRAPH_SCRUB_WORDS")
    default_words = Path.home() / ".config/plangraph/scrub-words.txt"
    scrubs = [scrub]
    if "PLANGRAPH_SCRUB_WORDS" in os.environ:
        words = words or str(default_words)
    elif default_words.exists():
        words = str(default_words)
    if words:
        scrub.extend(["--words", words])
    tests = python("affected_tests.py", "--base", test_baseline(root, args.base), "--head", args.head,
                   "--root", str(root), "--files-only", "--local", "--run")
    if importlib.util.find_spec("xdist") is not None:
        tests += ["--workers", str(affected_workers())]
    families = [("scrub", scrubs),
                ("policy", [python("check_file_size.py", "--head", args.head), python("check_new_code_lang.py", "--head", args.head),
                            python("check_changelog_docs.py", "--head", args.head),
                            python("ci_commit_gate.py", "check", "--base", args.base,
                                   "--head", args.head, "--exempt", str(tools / "ci_commit_exempt.txt"))]),
                ("rust", None), ("tests", [] if args.fast else [tests])]
    summary, exit_code, stopped, scrub_bad = [], 0, False, False
    for family, commands in families:
        start = time.monotonic()
        status = "SKIP"
        if family == "tests" and scrub_bad and not stopped:
            print("tests: SKIP: scrub failed, push is refused anyway", flush=True)
        elif not stopped:
            try:
                if commands is None:
                    commands = rust_commands(root, args.base, args.head, env)
                family_env = policy_env if family == "policy" else env
                if family == 'rust':
                    rust_env = rust_target_env(env, cleanup)
                    print(f"rust CARGO_TARGET_DIR: {rust_env.get('CARGO_TARGET_DIR', root / 'rust/target')}", flush=True)
                    family_env = {key: value for key, value in rust_env.items()
                                  if not key.upper().startswith('GIT_')}
                    require_protoc(root, commands, family_env)
                if family == "tests" and commands:
                    family_env = test_environment(root, env)
                failed = []  # policy runs every check and reports all failures
                for command in commands:
                    cwd = root / "rust" if family == "rust" else root
                    command_env = family_env
                    if (family == "rust" and command[:2] == ["cargo", "test"]
                            and "--manifest-path" in command
                            and command[command.index("--manifest-path") + 1]
                            == str((root / "rust/worker/Cargo.toml").resolve())
                            and "PG_JAIL_BIN" not in family_env
                            and family_env.get("PG_JAIL", "off") == "off"):
                        # Match rust-worker CI for this test child only.
                        command_env = dict(family_env)
                        command_env.setdefault("PG_JAIL", "off")
                        command_env.setdefault("PG_TEST_ALLOW_UNJAILED", "1")
                    code, output = invoke(command, cwd, command_env)
                    print(output, end="", flush=True)
                    infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    if infra:
                        print(f"{family}: INFRA HTTP 5xx; rerun once", flush=True)
                        code, output = invoke(command, cwd, command_env)
                        print(output, end="", flush=True)
                        infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    status = "PASS" if not code else ("INFRA" if infra else "FAIL")
                    if code and family == "policy":
                        failed.append((status, Path(command[1]).name, first_error(output)))
                        continue
                    if code:
                        break
                if failed:
                    status = "FAIL" if any(f[0] == "FAIL" for f in failed) else "INFRA"
                    print(f"policy: {len(failed)} check(s) failed:", flush=True)
                    for _, name, line in failed:
                        print(f"  {name}: {line}", flush=True)
                if family == "scrub" and not words and status == "PASS":
                    status = "SKIP"
                    print("scrub: private word list missing; public rules passed", flush=True)
            except SkipFamily as exc:
                print(f"{family}: SKIP: {exc}", flush=True)
            except FailFamily as exc:
                status = "FAIL"
                print(str(exc) if str(exc).startswith("CARGO FAIL:") else f"{family}: FAIL: {exc}", flush=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                status = "FAIL"
            if status in {"FAIL", "INFRA"}:
                scrub_bad = scrub_bad or (family == "scrub" and status == "FAIL")
                if status == "FAIL" or exit_code == 1:
                    exit_code = 1
                else:
                    exit_code = 3
                stopped = args.fail_fast
        summary.append(f"{status} {family} {time.monotonic() - start:.3f}")
        print(summary[-1], flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
