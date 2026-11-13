#!/usr/bin/env python3
"""Fail-closed, value-redacted pilot preparation (stdlib only)."""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit


CHECKOUT = Path(__file__).resolve().parents[1]
# PLANGRAPH_TEMPORAL_HTTP is read by the keeper, not the Rust worker.
REQUIRED_ENV = ("PLANGRAPH_TEMPORAL_HTTP", "PG_KEEPER_URL", "PG_KEEPER_TOKEN",
                "PG_JAIL_BIN", "PG_HARNESS_VERSIONS", "PG_BUILD_ID",
                "PG_CARD_ALLOWLIST", "PG_TEMPORAL_ADDR")


def emit(check, good, detail):
    print(f'{"OK" if good else "REFUSE"} {check} {detail}')
    return good


def command(argv, cwd=None, timeout=10, home=None, extra_env=None):
    # Never forward subprocess output: child processes can echo sensitive values.
    private_home = tempfile.mkdtemp(prefix="pilot-home-") if home is None else None
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout,
                              check=False, env={"PATH": os.environ.get("PATH", os.defpath),
                                                "LANG": "C.UTF-8", "HOME": private_home if home is None else home,
                                                "TERM": "dumb", **(extra_env or {})})
    finally:
        if private_home is not None:
            # The child may remove, chmod or replace HOME; never follow symlinks
            # or let cleanup errors mask its result (including a timeout).
            if os.path.islink(private_home):
                try:
                    os.unlink(private_home)
                except OSError:
                    pass
            else:
                shutil.rmtree(private_home, ignore_errors=True)


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
                and url.port != 0 and not url.username and not url.password
                and not url.fragment and not any(ord(c) < 33 for c in value))
    except ValueError:
        return False


def absolute_file(value):
    path = Path(value)
    try:
        return bool(value) and path.is_absolute() and stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def read_regular_file(value, mode=None):
    fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (mode is not None and stat.S_IMODE(info.st_mode) != mode):
            raise ValueError("invalid file")
        return stream.read()


def card_keys(text):
    keys = [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    # The pilot namespace/form is the boundary, not a hardcoded PILOT-1/2 pair.
    if not 1 <= len(keys) <= 20 or any(
            not re.fullmatch(r"pilot:PILOT-[0-9]+", key) for key in keys):
        raise ValueError("invalid pilot keys")
    return keys


def credential_valid():
    token = os.environ.get("PG_TOKEN_FILE")
    if token is not None:
        try:
            if not absolute_file(token):
                return False
            credential = read_regular_file(token, 0o600).strip()
        except (OSError, ValueError):
            return False
    else:
        credential = os.environ.get("PG_KEEPER_TOKEN", "")
    return bool(credential) and not any(c.isspace() or ord(c) < 32 for c in credential)


def preflight():
    results = []
    for name in REQUIRED_ENV:
        value = os.environ.get(name, "")
        valid = bool(value.strip()) and not any(c in value for c in "\x00\r\n")
        if name in ("PLANGRAPH_TEMPORAL_HTTP", "PG_KEEPER_URL"):
            valid = valid and http_url(value)
        elif name in ("PG_JAIL_BIN", "PG_CARD_ALLOWLIST"):
            valid = valid and absolute_file(value)
            if name == "PG_JAIL_BIN":
                valid = valid and os.access(value, os.X_OK)
            if valid and name == "PG_CARD_ALLOWLIST":
                try:
                    card_keys(read_regular_file(value))
                except (OSError, ValueError):
                    valid = False
        elif name == "PG_HARNESS_VERSIONS":
            try:
                pins(value)
            except ValueError:
                valid = False
        elif name == "PG_BUILD_ID":
            valid = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", value))
        elif name == "PG_KEEPER_TOKEN":
            valid = credential_valid()
        elif name == "PG_TEMPORAL_ADDR":
            valid = valid and http_url(value if "://" in value else "http://" + value)
        results.append(emit(name, valid, "present and valid" if valid else "missing, empty or malformed"))
    return all(results)


def setup_repo(directory):
    if '..' in Path(directory).parts:
        return emit("setup-repo", False, "directory contains parent traversal")
    repo = Path(directory).resolve()
    allow = repo.parent / (repo.name + ".cards.allow")
    if repo.is_relative_to(CHECKOUT):
        return emit("setup-repo", False, "directory is inside checkout")
    if repo.exists() and (not repo.is_dir() or any(repo.iterdir())):
        return emit("setup-repo", False, "directory must be absent or empty")
    if allow.exists() or allow.is_symlink():
        return emit("setup-repo", False, "allowlist already exists")
    repo.parent.mkdir(parents=True, exist_ok=True)
    # Empty HOME and explicit config operands isolate Git without extra env keys.
    def git(*args):
        result = command(["git", "-c", "user.name=Pilot", "-c",
                          "user.email=pilot@localhost", "-c",
                          "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                          *args], cwd=staged, timeout=30)
        if result.returncode:
            raise ValueError("git failed")

    with tempfile.TemporaryDirectory(prefix=".pilot-repo-", dir=repo.parent) as temp:
        staged = Path(temp) / "repo"
        staged.mkdir()
        git("init", "-b", "main", "--template=")
        script = staged / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        git("add", "--", "check.sh")
        git("commit", "-m", "Initialize isolated pilot")
        staged_allow = staged / allow.name
        with staged_allow.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(card_keys("pilot:PILOT-1\npilot:PILOT-2\n")) + "\n")
        # Publish without replacing a concurrent allowlist, before moving the repo.
        os.link(staged_allow, allow)
        try:
            staged_allow.unlink()
            staged.rename(repo)
        except OSError:
            allow.unlink()
            raise
    # Paths are explicit CLI operands, never environment values.
    emit("setup-repo", True, f"repo={ascii(str(repo))} allowlist={ascii(str(allow))}")
    return True


def persistent(path, mountinfo=None):
    path = path.resolve()
    if path.is_relative_to(Path("/tmp").resolve()) or not path.is_file() or not os.access(path, os.W_OK):
        return False
    # Longest matching Linux mount wins, including bind-mounted tmpfs paths.
    best, filesystem = -1, None
    for line in (Path("/proc/self/mountinfo").read_text()
                 if mountinfo is None else mountinfo).splitlines():
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
        try:
            mount = Path(mount).resolve()
        except (OSError, RuntimeError):
            return False
        if path.is_relative_to(mount) and len(mount.parts) >= best:
            best, filesystem = len(mount.parts), right.split()[0]
    return filesystem in {
        "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs"}


def verify(args):
    results = []
    failed = None

    def check(name, operation, detail):
        nonlocal failed
        if failed:
            print(f"SKIP {name} not run: {failed} failed")
            results.append(False)
            return
        try:
            good = bool(operation())
        except Exception:
            good = False
        results.append(emit(name, good, detail if good else "verification failed"))
        if not good:
            failed = name

    check("persistence", lambda: persistent(Path(args.temporal_db)), "regular persistent writable database")

    def auth():
        if not credential_valid():
            return False
        allow = os.environ.get("PG_CARD_ALLOWLIST", "")
        if not absolute_file(allow):
            return False
        repo = Path(args.repo).resolve()
        card_keys(read_regular_file(allow))
        home = os.environ.get("HOME") or str(Path.home())
        git_env = {key: value for key, value in os.environ.items()
                   if key in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                              "GIT_CONFIG_COUNT", "XDG_CONFIG_HOME"}
                   or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        top = command(["git", "rev-parse", "--show-toplevel"], repo, home=home, extra_env=git_env)
        if top.returncode or Path(os.fsdecode(top.stdout).strip()).resolve() != repo:
            return False
        helper = command(["git", "config", "--get-regexp", r"^credential(\..*)?\.helper$"],
                         repo, home=home, extra_env=git_env)
        remotes = command(["git", "remote"], repo, home=home, extra_env=git_env)
        return helper.returncode == 1 and remotes.returncode == 0 and not remotes.stdout.strip()

    check("auth", auth, "token mode checked; no credential helper or remote")
    # rust/pg-jail/src/main.rs:19-33 accepts --rw/--ro/--deny-read/--proc-self/--,
    # not --help. Do not execute a pretend help probe (or a jailed command).
    check("jail", lambda: args.jail_bin == os.environ.get("PG_JAIL_BIN")
          and absolute_file(args.jail_bin) and os.access(args.jail_bin, os.X_OK),
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
    except Exception:
        good = emit(args.action, False, "operation failed; details redacted")
    return 0 if good else 2


if __name__ == "__main__":
    raise SystemExit(main())
