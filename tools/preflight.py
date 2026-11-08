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


class SkipFamily(ValueError):
    """A check has no usable revision baseline."""


def run(command, cwd, env):
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=CHECK_TIMEOUT)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        return 1, "check exceeded 60 second timeout\n"
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
    if not changed or not shutil.which("cargo"):
        return []
    manifests = {}
    for manifest in (root / "rust").rglob("Cargo.toml"):
        if "target" in manifest.parts:
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
    env = dict(os.environ, FILESIZE_REQUIRE_BASE=args.base, NEWCODE_BASE=args.base,
               CHANGELOG_BASE=args.base, CARGO_BUILD_JOBS="4")

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
    tests = python("affected_tests.py", "--base", args.base, "--head", args.head,
                   "--root", str(root), "--run")
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
                for command in commands:
                    cwd = root / "rust" if family == "rust" else root
                    code, output = run(command, cwd, env)
                    print(output, end="", flush=True)
                    infra = bool(code and re.search(r"\bHTTP(?:/\d(?:\.\d)?)?\s+5\d\d\b", output, re.I))
                    if infra:
                        print(f"{family}: INFRA HTTP 5xx; rerun once", flush=True)
                        code, output = run(command, cwd, env)
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
