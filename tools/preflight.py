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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_select import MAX_RUST_TIMEOUT, RUST_TIMEOUT, host_load, planned_test_binaries, running_test_binary, rust_bound
from pgtest import test_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from plangraph.spawner.host_ready import (cargo_child_env, cargo_lease, cargo_target_cap_gb,
                                          disk_free_gb, reclaim_cargo_target,
                                          running_disk_refusal, shared_cargo_target)
from plangraph.spawner.overflow import available_memory_mb

CHECK_TIMEOUT = 60
MIN_BUILD_JOBS = 4
# A normal affected run takes around 50 s, with a larger bound for hundreds of test paths.
TEST_TIMEOUT = 240
# Callers without file-only selection can scale the bound for larger test sets.
TEST_PER_PATH_TIMEOUT = 2
MAX_TEST_TIMEOUT = 1200
# Cap xdist by available memory as well as cores on both Linux and Windows.
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


def build_jobs(env, cores, load):
    """CARGO_BUILD_JOBS: the configured positive value, else the free cores, floor MIN_BUILD_JOBS."""
    configured = env.get("CARGO_BUILD_JOBS", "")
    if configured.isascii() and configured.isdigit() and int(configured) > 0:
        return int(configured)
    if configured:
        return MIN_BUILD_JOBS
    return max(MIN_BUILD_JOBS, min(cores, int(cores - load)))


def check_timeout(command, cwd=None, env=None):
    if command[:2] in (["cargo", "clippy"], ["cargo", "test"], ["cargo", "build"]):
        load, cores = host_load()
        return rust_bound(planned_test_binaries([command], cwd or Path.cwd()), load, cores, floor=RUST_TIMEOUT)
    if is_affected_step(command):
        if "--files-only" in command:
            return TEST_TIMEOUT
        return scaled_test_timeout(planned_paths(command, cwd, env))
    return CHECK_TIMEOUT


class SkipFamily(ValueError):
    """A check has no usable revision baseline."""


class FailFamily(ValueError):
    """A check cannot run because a required tool is missing."""


def run_test_process(command, **kwargs):
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
                               capture_output=True, timeout=CHECK_TIMEOUT)
                process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def guarded_cargo_test(args):
    """Build the checkout's runner into the inherited target and use it for cargo test."""
    manifest = Path(__file__).resolve().parents[1] / 'rust/Cargo.toml'
    build = subprocess.run(
        ['cargo', 'build', '--manifest-path', str(manifest), '-p', 'pg-worker-core',
         '--bin', 'pg-test-deadline', '--message-format=json-render-diagnostics'],
        stdout=subprocess.PIPE, text=True,
    )
    runner, diagnostics = policy_artifact(build.stdout, binary='pg-test-deadline')
    sys.stderr.write(diagnostics)
    if build.returncode:
        return build.returncode
    if runner is None:
        raise RuntimeError('cargo did not report the pg-test-deadline executable')
    command = ['cargo', '--config', 'target."cfg(all())".runner=' + json.dumps([runner]),
               'test', *args]
    if os.name == 'posix':
        os.execvp(command[0], command)
    return subprocess.call(command)


def run(command, cwd, env):
    timeout = check_timeout(command, cwd, env)
    try:
        rust_test = command[:2] == ['cargo', 'test']
        runner = run_test_process if rust_test or is_affected_step(command) else subprocess.run
        argv = [sys.executable, __file__, '--cargo-test', *command[2:]] if rust_test else command
        result = runner(argv, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired as exc:
        if is_affected_step(command):
            return 1, (f"tests: affected-tests step exceeded its {timeout} second bound "
                       f"({selected_count(exc.stdout)} selected test paths)\n")
        binary = running_test_binary(exc.stdout)
        named = f" (running test binary: {binary})" if binary else ""
        return 1, f"check exceeded {timeout} second timeout{named}\n"
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


def lane_rust_commands(commands):
    """The lane keeps only the seconds-fast Rust check: formatting. Clippy and cargo test run in CI."""
    return [command for command in commands if command[:2] == ["cargo", "fmt"]]


def with_no_fail_fast(command):
    """cargo test stops at the first failing binary unless told not to: every binary runs in one pass."""
    if command[:2] == ["cargo", "test"] and "--no-fail-fast" not in command:
        return command[:2] + ["--no-fail-fast"] + command[2:]
    return command


def failing_tests(output):
    """Ids of every test cargo reported as FAILED, in order, without repeats."""
    ids = re.findall(r"^test (\S+) \.\.\. FAILED$", output, re.M)
    return list(dict.fromkeys(ids))


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


def private_rust_target(parent, cleanup):
    parent.mkdir(parents=True, exist_ok=True)
    private = tempfile.mkdtemp(prefix=PRIVATE_TARGET_PREFIX, dir=parent)
    cleanup.append(private)
    return private


def rust_compile_failure(command, output):
    if command[0] != "cargo":
        return False
    if command[1] == "test":
        return bool(re.search(r"^error: could not compile\b", output, re.M))
    return command[1] in {"build", "check", "clippy"} and bool(
        re.search(r"^error(?:\[E\d+\])?: ", output, re.M))


def rust_target_env(env, cleanup):
    """Return env for the Rust family: private target when sccache is present and disk allows.

    cleanup is a list that receives the private directory so the caller removes it.
    Never raises: any problem falls back to the shared target. An inherited
    RUSTC_WRAPPER is never replaced.
    """
    out = dict(env)
    if not env.get("SCCACHE_DIR"):
        if wrapper := env.get("RUSTC_WRAPPER"):
            print(f"rust: shared CARGO_TARGET_DIR (RUSTC_WRAPPER={wrapper} is not sccache)", flush=True)
        elif env.get("PLANGRAPH_SCCACHE"):
            print("rust: shared CARGO_TARGET_DIR (configured PLANGRAPH_SCCACHE not found, no sccache)", flush=True)
        else:
            print("rust: shared CARGO_TARGET_DIR (no sccache; a private target would be cold)", flush=True)
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
            prune_stale_targets(parent)
            out["CARGO_TARGET_DIR"] = private_rust_target(parent, cleanup)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"rust: shared CARGO_TARGET_DIR ({exc})", flush=True)
    return out


def policy_build_command(root):
    """The one build of pg-policy: the policy step runs this binary, never a cold cargo run."""
    manifest = Path(root) / 'rust/Cargo.toml'
    if not manifest.is_file():
        return None
    return ['cargo', 'build', '--quiet', '--locked', '--message-format=json',
            '--manifest-path', str(manifest), '-p', 'pg-policy']


def policy_artifact(output, binary='pg-policy'):
    """Find the named Cargo-built executable and return the readable build output."""
    executable, text = None, []
    for line in output.splitlines():
        try:
            message = json.loads(line) if line.startswith('{') else None
        except ValueError:
            message = None
        if message is None:
            text.append(line)
        elif message.get('reason') == 'compiler-message':
            text.append(message.get('message', {}).get('rendered', ''))
        elif (message.get('reason') == 'compiler-artifact' and message.get('executable')
              and message.get('target', {}).get('name') == binary
              and 'bin' in message['target'].get('kind', [])):
            executable = message['executable']
    return executable, ''.join(line + '\n' for line in text if line)


def policy_target(root):
    """A Cargo target of its own for pg-policy, inside the shared target and keyed by what the binary is built from.

    target/debug/pg-policy of the shared target is one file that any checkout's build may replace, so a
    checkout's own artifact cannot be told from another's there. Equal sources share one directory
    (a warm build for every later clone); different sources never do."""
    import hashlib
    rust, digest = Path(root) / 'rust', hashlib.sha256()
    crate = rust / 'pg-policy'
    # Build output under the crate is not source; the exclusion is relative to the crate, so a
    # checkout that lives under a directory named target still hashes its sources.
    names = sorted(p for p in crate.rglob('*')
                   if p.is_file() and 'target' not in p.relative_to(crate).parts)
    names += [p for p in (rust / 'Cargo.toml', rust / 'Cargo.lock', Path(root) / 'rust-toolchain.toml',
                          rust / 'rust-toolchain.toml') if p.is_file()]
    for path in names:
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return shared_cargo_target() / 'policy-bin' / digest.hexdigest()[:16]


def stage_policy_binary(executable, cleanup):
    """Private copy of the built pg-policy, taken under the build lease."""
    private = tempfile.mkdtemp(prefix='preflight-policy-')
    cleanup.append(private)
    return shutil.copy2(executable, Path(private) / Path(executable).name)


def build_policy(root, env, cleanup):
    """Build pg-policy and stage this checkout's own artifact while the shared lease is held.

    The build target is keyed by the sources (policy_target), so no other checkout's build can
    replace this artifact, and cleanup needs the exclusive lease, so the copy happens before release.
    Returns (code, text, staged binary or None); an unusable lease is a failed build named in the
    text, so the independent policy checks still run."""
    try:
        lease = cargo_lease()
    except OSError as exc:
        return 1, f'cargo lease unavailable: {type(exc).__name__}: {exc}\n', None
    with lease:
        refusal = running_disk_refusal(root)
        if refusal:
            return 1, refusal + '\n', None
        keydir = policy_target(root)
        code, output = run(policy_build_command(root), root, dict(env, CARGO_TARGET_DIR=str(keydir)))
        executable, text = policy_artifact(output)
        binary = None
        if not code:
            try:
                binary = stage_policy_binary(executable, cleanup) if executable else None
            except OSError:
                binary = None
            try:
                os.utime(keydir)  # a reused key stays newer than the prune window
            except OSError:
                pass
            if binary is None:
                code, text = 1, text + 'pg-policy build reported no usable executable\n'
        refusal = running_disk_refusal(root)
        if refusal:
            return 1, text + refusal + '\n', None
        return code, text, binary


# A live build holds a shared lease on the target; trimming waits this long, then skips.
TRIM_WAIT_SECONDS = 5.0


def trim_shared_target(root):
    """Before any build: cap the shared target and prune its old cli-tests builds (LE-PUSH-WARM-TARGET-UNCAPPED)."""
    target = shared_cargo_target()
    try:
        if target.exists():
            reclaim_cargo_target(target, cargo_target_cap_gb(), root,
                                 wait=TRIM_WAIT_SECONDS, identified=True)
    except (OSError, ValueError) as exc:
        print(f"cargo target trim skipped: {exc}", flush=True)


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
    parser.add_argument("--lane", action="store_true",
                        help="push-gate mode: seconds-fast checks (scrub, policy, skill lint, cargo fmt) and no test")
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
    load, cores = host_load()
    env = dict(os.environ, CARGO_BUILD_JOBS=str(build_jobs(os.environ, cores, load)))
    cargo_bin = Path.home() / '.cargo/bin'
    if cargo_bin.is_dir():
        env['PATH'] = str(cargo_bin) + os.pathsep + env.get('PATH', '')
    env = cargo_child_env(env)
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
            refusal = running_disk_refusal(root)
            if refusal:
                return 1, refusal + '\n'
            code, output = run(command, cwd, command_env)
            refusal = running_disk_refusal(root)
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
                   "--root", str(root), "--files-only", "--local", "--parts-union", "--run")
    if importlib.util.find_spec("xdist") is not None:
        tests += ["--workers", str(affected_workers())]
    families = [("scrub", scrubs),
                ("policy", [python("check_file_size.py", "--head", args.head), python("check_new_code_lang.py", "--head", args.head),
                            python("check_changelog_docs.py", "--head", args.head),
                            python("ci_commit_gate.py", "check", "--base", policy_base,
                                   "--head", args.head),
                            *([python("skill-rules.py", "all")] if args.lane else [])]),
                ("rust", None), ("tests", [] if args.fast or args.lane else [tests])]
    summary, exit_code, stopped, scrub_bad = [], 0, False, False
    trim_shared_target(root)
    for family, commands in families:
        start = time.monotonic()
        status = "SKIP"
        if family == "tests" and scrub_bad and not stopped:
            print("tests: SKIP: scrub failed, push is refused anyway", flush=True)
        elif not stopped:
            try:
                if commands is None:
                    commands = rust_commands(root, args.base, args.head, env)
                    if args.lane:
                        commands = lane_rust_commands(commands)
                family_env = policy_env if family == "policy" else env
                if family == 'rust':
                    rust_env = rust_target_env(env, cleanup)
                    print(f"rust CARGO_TARGET_DIR: {rust_env.get('CARGO_TARGET_DIR', root / 'rust/target')}", flush=True)
                    family_env = {key: value for key, value in rust_env.items()
                                  if not key.upper().startswith('GIT_')}
                    require_protoc(root, commands, family_env)
                if family == "tests" and commands:
                    family_env = test_environment(root, env, timeout=CHECK_TIMEOUT)
                failed = []  # policy runs every check and reports all failures
                if family == "policy" and policy_build_command(root):
                    # Outside the per-check bound; a cold compile must not fail a green diff.
                    code, output, binary = build_policy(root, env, cleanup)
                    print(output, end="", flush=True)
                    if binary:
                        family_env = dict(family_env, PG_POLICY_BIN=binary)
                    else:
                        failed.append(("FAIL", "pg-policy build", first_error(output)))
                        commands = [c for c in commands if Path(c[1]).name != "check_new_code_lang.py"]
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
                    command = with_no_fail_fast(command)
                    code, output = invoke(command, cwd, command_env)
                    if (code and family == "rust"
                            and command_env["CARGO_TARGET_DIR"] == env["CARGO_TARGET_DIR"]
                            and rust_compile_failure(command, output)):
                        try:
                            private = private_rust_target(Path(env["CARGO_TARGET_DIR"]).parent, cleanup)
                        except OSError as exc:
                            print(f"rust: could not allocate a verification target: {exc}", flush=True)
                        else:
                            print(f"rust: {command[1]} failed in the shared target; "
                                  f"verifying in CARGO_TARGET_DIR={private}", flush=True)
                            family_env = dict(family_env, CARGO_TARGET_DIR=private)
                            command_env = dict(command_env, CARGO_TARGET_DIR=private)
                            code, output = invoke(command, cwd, command_env)
                    print(output, end="", flush=True)
                    infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    if infra:
                        print(f"{family}: INFRA HTTP 5xx; rerun once", flush=True)
                        code, output = invoke(command, cwd, command_env)
                        print(output, end="", flush=True)
                        infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    status = "PASS" if not code else ("INFRA" if infra else "FAIL")
                    if code and family in ("policy", "rust"):
                        # Every check of these families runs, and every failure is reported in one pass.
                        name = Path(command[1]).name if family == "policy" else " ".join(command[:2])
                        failed.append((status, name, first_error(output)))
                        failed.extend(("FAIL", "failing test", test_id) for test_id in failing_tests(output)
                                      if family == "rust")
                        continue
                    if code:
                        break
                if failed:
                    status = "FAIL" if any(f[0] == "FAIL" for f in failed) else "INFRA"
                    print(f"{family}: {len(failed)} check(s) failed:", flush=True)
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
    sys.exit(guarded_cargo_test(sys.argv[2:]) if sys.argv[1:2] == ["--cargo-test"] else main())
