#!/usr/bin/env python3
"""Blocking local CI policy, Rust and affected-test preflight."""
import argparse
import importlib.util
import json
import os
import re
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

CHECK_TIMEOUT = 60
# Measured on a build host: pg-spawnd clippy 17.8 s, shim/windows_pipe tests 48.4 s.
# Leave ample room for slower hosts, multiple affected crates and test suites.
RUST_TIMEOUT = 600
# The affected-test step is bounded well under the 5 minute push target; the
# measured normal run is about 50 s.
TEST_TIMEOUT = 240


def check_timeout(command):
    if command[:2] in (["cargo", "clippy"], ["cargo", "test"]):
        return RUST_TIMEOUT
    if len(command) > 1 and Path(command[1]).name == "affected_tests.py":
        return TEST_TIMEOUT
    return CHECK_TIMEOUT


class SkipFamily(ValueError):
    """A check has no usable revision baseline."""


class FailFamily(ValueError):
    """A check cannot run because a required tool is missing."""


def run_test_process(command, **kwargs):
    """Bound the selector and its pytest workers as one process tree."""
    timeout = kwargs.pop('timeout')
    with subprocess.Popen(command, **kwargs, start_new_session=os.name == 'posix') as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                               capture_output=True, timeout=CHECK_TIMEOUT)
                process.kill()
            process.communicate()
            raise exc
        return subprocess.CompletedProcess(command, process.returncode, stdout)


def run(command, cwd, env):
    timeout = check_timeout(command)
    try:
        runner = run_test_process if len(command) > 1 and Path(command[1]).name == "affected_tests.py" else subprocess.run
        result = runner(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired as exc:
        if timeout == TEST_TIMEOUT and len(command) > 1 and Path(command[1]).name == "affected_tests.py":
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


def rust_commands(root, base, head, env=None):
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
    manifests = {}
    for manifest in (root / "rust").rglob("Cargo.toml"):
        if "target" in manifest.parts:
            continue
        if members is not None and manifest.parent.resolve() not in members:
            continue
        data = tomllib.loads(manifest.read_text())
        if "package" in data:
            manifests[manifest.parent.relative_to(root).as_posix()] = data["package"]["name"]
    selected = set()
    for path in changed:
        owners = [p for p in manifests if path.startswith(p + "/")]
        selected.update([max(owners, key=len)] if owners else manifests)
    packages = [arg for p in sorted(selected) for arg in ("-p", manifests[p])]
    return [["cargo", "fmt", "--check"],
            ["cargo", "clippy", *packages, "--all-targets", "--", "-D", "warnings"],
            ["cargo", "test", *packages]]


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


def main(argv=None, root=None):
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
    env = dict(os.environ, CARGO_BUILD_JOBS="4")
    cargo_bin = Path.home() / '.cargo/bin'
    if cargo_bin.is_dir():
        env['PATH'] = str(cargo_bin) + os.pathsep + env.get('PATH', '')
    # Nested test repositories own their revision history, not this checkout's base.
    for key in policy_bases:
        env.pop(key, None)
    policy_env = dict(env, **policy_bases)

    def python(name, *extra):
        return [interpreter, str(tools / name), *extra]

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
        scrubs.append(python("preflight_private_scrub.py", "--base", args.scrub_base or args.base,
                             "--head", args.head, "--words", words))
    tests = python("affected_tests.py", "--base", test_baseline(root, args.base), "--head", args.head,
                   "--root", str(root), "--files-only", "--local", "--run")
    if importlib.util.find_spec("xdist") is not None:
        tests += ["--workers", "2"]
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
                    family_env = {key: value for key, value in env.items()
                                  if not key.upper().startswith('GIT_')}
                if family == "tests" and commands:
                    family_env = test_environment(root, env)
                failed = []  # policy runs every check and reports all failures
                for command in commands:
                    cwd = root / "rust" if family == "rust" else root
                    code, output = run(command, cwd, family_env)
                    print(output, end="", flush=True)
                    infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    if infra:
                        print(f"{family}: INFRA HTTP 5xx; rerun once", flush=True)
                        code, output = run(command, cwd, family_env)
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
