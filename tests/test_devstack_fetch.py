"""tools/devstack.sh ensure_temporal_cli: hermetic fetch coverage via a file:// release tree.

Everything runs against a fake PATH holding only the tools the fetch path needs (curl, tar,
sha256sum, ...) so `temporal` is absent by construction; the tarball is laid out exactly as
GitHub releases are (<base>/v<V>/temporal_cli_<V>_<os>_<arch>.tar.gz) with a tiny shell stub
inside, and TEMPORAL_CLI_SHA256 is overridden together with the version (the supported
hermetic combination).
"""
import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "devstack.sh"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="devstack.sh is a posix bash script")

VERSION = "9.9.9"
DOWNLOAD_TMP = "temporal_cli_download.tar.gz"

# external commands devstack.sh touches from the set-current code path (plus a couple of
# safety nets); symlinking only these keeps the fake PATH hermetic while `temporal` is absent.
# gzip is a hard requirement: tar -xzf execs it as a child ("tar (child): gzip: Cannot exec"
# when it is missing from PATH).
TOOLS = ["bash", "mkdir", "dirname", "uname", "gzip", "curl", "tar", "sha256sum", "cut",
         "mktemp", "tr", "head", "chmod", "rm", "cat", "sleep"]
# platform-dependent fallbacks the script probes at runtime: linked when present, no skip
OPTIONAL_TOOLS = ["shasum", "sha256", "wget"]

TEMPORAL_STUB = """\
#!/bin/bash
printf 'temporal %s\\n' "$*" >>"{log}"
exit 0
"""

UNAME_FAKE = """\
#!/bin/bash
case "${{1:-}}" in
  -s) printf '%s\\n' "{os}" ;;
  -m) printf '%s\\n' "{arch}" ;;
esac
"""


def host_os_arch():
    """Derive (os, arch) exactly like ensure_temporal_cli maps uname -s / uname -m."""
    out = subprocess.run(["uname", "-s"], capture_output=True, text=True).stdout.strip()
    mach = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    os_map = {"Linux": "linux", "Darwin": "darwin"}
    arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if out not in os_map or mach not in arch_map:
        pytest.skip(f"uname reports unsupported host {out!r}/{mach!r}")
    return os_map[out], arch_map[mach]


def make_path_bin(tmp_path, name, skip_tools=()):
    d = tmp_path / name
    d.mkdir()
    for tool in TOOLS + OPTIONAL_TOOLS:
        if tool in skip_tools:
            continue
        src = shutil.which(tool)
        if src is None:
            if tool in OPTIONAL_TOOLS:
                continue
            pytest.skip(f"{tool} not installed")
        os.symlink(src, d / tool)
    return d


def make_release(tmp_path, os_name, arch):
    """Create <tmp>/rel/v<V>/temporal_cli_<V>_<os>_<arch>.tar.gz holding an executable
    `temporal` stub; return the tarball's real sha256."""
    rel = tmp_path / "rel" / f"v{VERSION}"
    rel.mkdir(parents=True)
    tarball = rel / f"temporal_cli_{VERSION}_{os_name}_{arch}.tar.gz"
    payload = tmp_path / "temporal"
    payload.write_text(TEMPORAL_STUB.format(log=str(tmp_path / "calls.log")))
    os.chmod(payload, 0o755)
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(payload, arcname="temporal")
    return hashlib.sha256(tarball.read_bytes()).hexdigest()


def base_env(tmp_path, path_bin, **overrides):
    env = {
        "PATH": str(path_bin),
        "HOME": str(tmp_path),
        "DEVSTACK_STATE": str(tmp_path / "state"),
        "DEVSTACK_TEMPORAL_URL_BASE": f"file://{tmp_path}/rel",
        "TEMPORAL_CLI_VERSION": VERSION,
    }
    env.update(overrides)
    return env


def run_devstack(env, *cmds, timeout=60):
    return subprocess.run([BASH, str(SCRIPT), *cmds], env=env,
                          capture_output=True, text=True, timeout=timeout)


def calls(tmp_path):
    log = tmp_path / "calls.log"
    return log.read_text() if log.exists() else ""


def test_missing_cli_with_correct_sha256_installs_and_proceeds(tmp_path):
    os_name, arch = host_os_arch()
    sha = make_release(tmp_path, os_name, arch)
    env = base_env(tmp_path, make_path_bin(tmp_path, "pathbin"), TEMPORAL_CLI_SHA256=sha)

    r = run_devstack(env, "set-current")

    assert r.returncode == 0, r.stdout + r.stderr
    cli = tmp_path / "state" / "bin" / "temporal"
    assert cli.is_file() and os.access(cli, os.X_OK)
    # the install notice goes to stdout; the script then proceeds past the check and runs the
    # installed stub for worker deployment set-current-version
    assert f"temporal: installed CLI v{VERSION}" in r.stdout
    assert "worker deployment set-current-version" in calls(tmp_path)
    assert not (tmp_path / "state" / DOWNLOAD_TMP).exists()


def test_wrong_sha256_exits_1_and_removes_download(tmp_path):
    os_name, arch = host_os_arch()
    make_release(tmp_path, os_name, arch)
    env = base_env(tmp_path, make_path_bin(tmp_path, "pathbin"),
                   TEMPORAL_CLI_SHA256="0" * 64)

    r = run_devstack(env, "set-current")

    assert r.returncode == 1
    assert "temporal: sha256 mismatch" in r.stderr
    assert not (tmp_path / "state" / "bin" / "temporal").exists()
    assert not (tmp_path / "state" / DOWNLOAD_TMP).exists()
    assert calls(tmp_path) == ""


def test_cli_already_on_path_never_downloads(tmp_path):
    path_bin = make_path_bin(tmp_path, "pathbin")
    stub = path_bin / "temporal"
    stub.write_text(TEMPORAL_STUB.format(log=str(tmp_path / "calls.log")))
    os.chmod(stub, 0o755)
    # URL base points at a nonexistent tree and the sha override is garbage: either would
    # fail loudly if the resolution path touched the network at all
    env = base_env(tmp_path, path_bin,
                   DEVSTACK_TEMPORAL_URL_BASE=f"file://{tmp_path}/no_such_dir",
                   TEMPORAL_CLI_SHA256="1" * 64)

    r = run_devstack(env, "set-current")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "fetching CLI" not in r.stdout + r.stderr
    assert not (tmp_path / "state" / "bin" / "temporal").exists()
    assert "worker deployment set-current-version" in calls(tmp_path)


def test_unsupported_os_exits_1_before_any_download(tmp_path):
    path_bin = make_path_bin(tmp_path, "pathbin", skip_tools=("uname",))
    uname = path_bin / "uname"
    uname.write_text(UNAME_FAKE.format(os="Plan9", arch="x86_64"))
    os.chmod(uname, 0o755)
    env = base_env(tmp_path, path_bin,
                   DEVSTACK_TEMPORAL_URL_BASE=f"file://{tmp_path}/no_such_dir",
                   TEMPORAL_CLI_SHA256="0" * 64)

    r = run_devstack(env, "set-current")

    assert r.returncode != 0
    assert "temporal: unsupported OS 'Plan9'" in r.stderr
    assert "TEMPORAL_CLI" in r.stderr  # the hint telling the user what to set instead
    assert not (tmp_path / "state" / "bin" / "temporal").exists()
