#!/usr/bin/env python3
"""Fail-closed, value-redacted pilot preparation (stdlib only)."""
import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit


CHECKOUT = Path(__file__).resolve().parents[1]


def emit(check, good, detail):
    print(f'{"OK" if good else "REFUSE"} {check} {detail}')
    return good


def command(argv, cwd=None, timeout=10):
    # Never forward subprocess output: child processes can echo sensitive values.
    return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout,
                          check=False)


def pins(value):
    pairs = []
    for part in value.split(","):
        name, sep, version = part.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            raise ValueError("invalid pin")
        if not version.strip() or "=" in version or any(c in version for c in "\r\n\x00"):
            raise ValueError("invalid version")
        if name in [n for n, _ in pairs]:
            raise ValueError("duplicate pin")
        pairs.append((name, version))
    return pairs


def http_url(value):
    try:
        url = urlsplit(value)
        return (url.scheme in ("http", "https") and bool(url.hostname)
                and url.port != 0 and not any(c.isspace() for c in value))
    except ValueError:
        return False


def absolute_file(value):
    path = Path(value)
    return bool(value) and path.is_absolute() and path.is_file()


def preflight():
    results = []
    for name in ("PLANGRAPH_TEMPORAL_HTTP", "PG_KEEPER_URL", "PG_KEEPER_TOKEN",
                 "PG_JAIL_BIN", "PG_HARNESS_VERSIONS", "PG_BUILD_ID",
                 "PG_CARD_ALLOWLIST", "PG_TEMPORAL_ADDR"):
        value = os.environ.get(name, "")
        valid = bool(value.strip()) and not any(c in value for c in "\x00\r\n")
        if name in ("PLANGRAPH_TEMPORAL_HTTP", "PG_KEEPER_URL"):
            valid = valid and http_url(value)
        elif name in ("PG_JAIL_BIN", "PG_CARD_ALLOWLIST"):
            valid = valid and absolute_file(value)
        elif name == "PG_HARNESS_VERSIONS":
            try:
                pins(value)
            except ValueError:
                valid = False
        elif name == "PG_BUILD_ID":
            valid = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", value))
        elif name == "PG_TEMPORAL_ADDR":
            valid = valid and http_url(value if "://" in value else "http://" + value)
        results.append(emit(name, valid, "present and valid" if valid else "missing, empty or malformed"))
    return all(results)


def setup_repo(directory):
    repo = Path(directory).resolve()
    allow = repo.parent / (repo.name + ".cards.allow")
    if repo.is_relative_to(CHECKOUT):
        return emit("setup-repo", False, "directory is inside checkout")
    if repo.exists() and (not repo.is_dir() or any(repo.iterdir())):
        return emit("setup-repo", False, "directory must be absent or empty")
    if allow.exists() or allow.is_symlink():
        return emit("setup-repo", False, "allowlist already exists")
    repo.mkdir(parents=True, exist_ok=True)
    # Isolate Git from global/system configuration, templates, hooks and identity.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        result = subprocess.run(["git", "-c", "user.name=Pilot", "-c",
                                 "user.email=pilot@localhost", "-c",
                                 "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                 *args], cwd=repo, env=env, capture_output=True,
                                timeout=30, check=False)
        if result.returncode:
            raise ValueError("git failed")

    git("init", "-b", "main", "--template=")
    script = repo / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    git("add", "--", "check.sh")
    git("commit", "-m", "Initialize isolated pilot")
    with allow.open("x", encoding="utf-8") as stream:
        stream.write("pilot:PILOT-1\npilot:PILOT-2\n")
    # Paths are explicit CLI operands, never environment values.
    emit("setup-repo", True, f"repo={ascii(str(repo))} allowlist={ascii(str(allow))}")
    return True


def persistent(path):
    path = path.resolve()
    if path.is_relative_to(Path("/tmp")) or not path.is_file() or not os.access(path, os.W_OK):
        return False
    # Longest matching Linux mount wins, including bind-mounted tmpfs paths.
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
        if path.is_relative_to(Path(mount)):
            mounts.append((len(mount), right.split()[0]))
    return bool(mounts) and max(mounts)[1] not in ("tmpfs", "ramfs")


def verify(args):
    results = []

    def check(name, operation, detail):
        try:
            good = bool(operation())
        except (OSError, ValueError, subprocess.SubprocessError):
            good = False
        results.append(emit(name, good, detail if good else "verification failed"))

    check("persistence", lambda: persistent(Path(args.temporal_db)), "regular persistent writable database")

    def auth():
        token = os.environ.get("PG_TOKEN_FILE")
        if token is not None:
            p = Path(token)
            if not token or not p.is_file() or stat.S_IMODE(p.stat().st_mode) != 0o600:
                return False
        repo = Path(args.repo).resolve()
        top = command(["git", "rev-parse", "--show-toplevel"], repo)
        if top.returncode or Path(os.fsdecode(top.stdout).strip()).resolve() != repo:
            return False
        helper = command(["git", "config", "--get-regexp", r"^credential(\..*)?\.helper$"], repo)
        remotes = command(["git", "remote"], repo)
        return helper.returncode == 1 and remotes.returncode == 0 and not remotes.stdout.strip()

    check("auth", auth, "token mode checked; no credential helper or remote")
    # rust/pg-jail/src/main.rs:19-33 accepts --rw/--ro/--deny-read/--proc-self/--,
    # not --help. Do not execute a pretend help probe (or a jailed command).
    check("jail", lambda: absolute_file(args.jail_bin) and os.access(args.jail_bin, os.X_OK),
          "regular executable; CLI has no help probe")

    def versions():
        for name, version in pins(os.environ.get("PG_HARNESS_VERSIONS", "")):
            result = command([name, "--version"], timeout=10)
            reported = result.stdout.decode("utf-8").strip()
            if result.returncode or reported not in (version, f"{name} {version}"):
                return False
        return True

    check("pins", versions, "all version pins match")
    check("checks", lambda: command([str(Path(args.repo).resolve() / "check.sh")],
                                    cwd=args.repo, timeout=30).returncode == 0,
          "check.sh exited zero")
    return all(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight")
    setup = sub.add_parser("setup-repo")
    setup.add_argument("directory")
    verifier = sub.add_parser("verify")
    for flag in ("temporal-db", "jail-bin", "repo"):
        verifier.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        good = (preflight() if args.action == "preflight" else
                setup_repo(args.directory) if args.action == "setup-repo" else verify(args))
    except (OSError, ValueError, subprocess.SubprocessError):
        good = emit(args.action, False, "operation failed; details redacted")
    return 0 if good else 2


if __name__ == "__main__":
    raise SystemExit(main())
