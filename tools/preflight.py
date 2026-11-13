#!/usr/bin/env python3
"""Blocking local CI policy, Rust and affected-test preflight."""
import argparse
import importlib.util
import os
import re
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
TEST_TIMEOUT = 900


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


def run(command, cwd, env):
    timeout = check_timeout(command)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        return 1, f"check exceeded {timeout} second timeout\n"
    except OSError as exc:
        return 1, f"unable to start check: {type(exc).__name__}\n"


def rust_commands(root, base, head):
    try:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
            cwd=root, text=True, stderr=subprocess.DEVNULL,
            timeout=CHECK_TIMEOUT).splitlines()
    except subprocess.CalledProcessError as exc:
        raise SkipFamily("revision baseline unavailable or no merge-base") from exc
    changed = [p for p in changed if p.startswith("rust/")]
    if not changed:
        raise SkipFamily("no rust changes")
    if not shutil.which("cargo"):
        raise FailFamily("missing required tool: cargo")
    manifests, workspace_roots = {}, {"rust"}
    for manifest in (root / "rust").rglob("Cargo.toml"):
        if "target" in manifest.parts:
            continue
        data = tomllib.loads(manifest.read_text())
        directory = manifest.parent.relative_to(root).as_posix()
        if "workspace" in data:
            workspace_roots.add(directory)
        if "package" in data:
            manifests[directory] = data["package"]["name"]
    # A nested [workspace] is independently built; shared root files select only
    # root members, never packages that cargo -p cannot resolve from rust/.
    workspace = {path: max((w for w in workspace_roots
                           if path == w or path.startswith(w + "/")), key=len)
                 for path in manifests}
    selected = {}
    for path in changed:
        owners = [p for p in manifests if path.startswith(p + "/")]
        if owners:
            owner = max(owners, key=len)
            selected.setdefault(workspace[owner], set()).add(owner)
        else:
            selected.setdefault("rust", set()).update(p for p in manifests
                                                       if workspace[p] == "rust")
    commands = []
    for owner in sorted(selected):
        packages = [arg for p in sorted(selected[owner])
                    for arg in ("-p", manifests[p])]
        manifest_args = ([] if owner == "rust" else
                         ["--manifest-path", str(Path(owner).relative_to("rust") / "Cargo.toml")])
        commands.extend([["cargo", "fmt", *manifest_args, "--check"],
                         ["cargo", "clippy", *manifest_args, *packages,
                          "--all-targets", "--", "-D", "warnings"],
                         ["cargo", "test", *manifest_args, *packages]])
    return commands


def test_environment(root, env):
    """A pre-push caller's Git context must not select a fixture's repository."""
    local = set(subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], cwd=root, env=env,
        text=True, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT).splitlines())
    return {key: value for key, value in env.items()
            if key not in local
            and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}


def test_baseline(root, fallback):
    """Local tests cover this branch's changes, excluding inherited upstream commits."""
    try:
        subprocess.check_output(['git', 'rev-parse', '--verify', 'origin/main^{commit}'],
                                cwd=root, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return fallback
    return 'origin/main'


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--fast", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--keep-going", action="store_true", help="default behaviour")
    mode.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parent.parent)
    tools = Path(os.environ.get("PREFLIGHT_TOOLS_DIR", Path(__file__).resolve().parent))
    policy_bases = dict(FILESIZE_REQUIRE_BASE=args.base, NEWCODE_BASE=args.base,
                        CHANGELOG_BASE=args.base)
    env = dict(os.environ, CARGO_BUILD_JOBS="4")
    # Nested test repositories own their revision history, not this checkout's base.
    for key in policy_bases:
        env.pop(key, None)
    policy_env = dict(env, **policy_bases)

    def python(name, *extra):
        return [sys.executable, str(tools / name), *extra]

    scrub = python("split/scrub_check.py", ".", "--rules", str(tools / "split/manifest.toml"),
                   "--rules", str(tools / "split/scrub_ci.toml"))
    words = os.environ.get("PLANGRAPH_SCRUB_WORDS")
    default_words = Path.home() / ".config/plangraph/scrub-words.txt"
    scrubs = [scrub]
    if "PLANGRAPH_SCRUB_WORDS" in os.environ:
        words = words or str(default_words)
    elif default_words.exists():
        words = str(default_words)
    if words:
        scrubs.append(python("preflight_private_scrub.py", "--base", args.base,
                             "--head", args.head, "--words", words))
    tests = python("affected_tests.py", "--base", test_baseline(root, args.base), "--head", args.head,
                   "--root", str(root), "--run", "--local")
    if importlib.util.find_spec("xdist") is not None:
        tests += ["--workers", "2"]
    families = [("scrub", scrubs),
                ("policy", [python("check_file_size.py", "--head", args.head), python("check_new_code_lang.py", "--head", args.head),
                            python("check_changelog_docs.py", "--head", args.head),
                            python("ci_commit_gate.py", "check", "--base", args.base,
                                   "--head", args.head, "--exempt", str(tools / "ci_commit_exempt.txt"))]),
                ("rust", None), ("tests", [] if args.fast else [tests])]
    summary, exit_code, stopped = [], 0, False
    for family, commands in families:
        start = time.monotonic()
        status = "SKIP"
        if not stopped:
            try:
                if commands is None:
                    commands = rust_commands(root, args.base, args.head)
                family_env = policy_env if family == "policy" else env
                if family == "tests" and commands:
                    family_env = test_environment(root, env)
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
                    if code:
                        break
                if family == "scrub" and not words and status == "PASS":
                    status = "SKIP"
                    print("scrub: private word list missing; public rules passed", flush=True)
            except SkipFamily as exc:
                print(f"{family}: SKIP: {exc}", flush=True)
            except FailFamily as exc:
                status = "FAIL"
                print(f"{family}: FAIL: {exc}", flush=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
                status = "FAIL"
            if status in {"FAIL", "INFRA"}:
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
