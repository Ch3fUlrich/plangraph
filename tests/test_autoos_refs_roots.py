"""Scan roots of tools/autoos_refs_check.py beyond the Claude/OpenCode/systemd
files: the routing launch records, routing ``ops/*.mcp.json`` and ``bin/``,
symlinked scripts followed to their content, and ``git ls-files -z``.

The tests avoid the word the scanner looks for in their own names: pytest
builds ``tmp_path`` from the test name, and a path that carries the word would
match by itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from autoos_refs_support import (  # noqa: F401  (ar is a fixture)
    ar, assert_no_leak, run_cli, windows_skip, write,
)

MENTION = "exec /home/x/code/AutoOS/bin/svc\n"
MARKER = "PAYLOAD" + "_MARKER_7"  # text that must never be printed


def routing_fixture(home: Path, routing: Path | None = None, launch: Path | None = None) -> None:
    """One file naming AutoOS in each of the three routing roots."""
    routing = routing or home / "code" / "routing"
    launch = launch or home / ".local" / "state" / "routing" / "launch"
    write(launch / "sess.json", '{"name": "sess", "cmd": "/home/x/code/AutoOS/bin/svc"}\n')
    write(routing / "ops" / "set.mcp.json", '{"command": "/home/x/code/AutoOS/mcp"}\n')
    write(routing / "bin" / "tool", "#!/bin/sh\n" + MENTION)


def test_routing_roots_are_scanned_by_default(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    routing_fixture(home)
    hits = ar.scan_all(home)
    assert sorted(h[0] for h in hits) == ["routing-bin", "routing-launch", "routing-mcp"]
    proc = run_cli("--home", str(home))
    assert proc.returncode == 1  # a fixture naming it in all three used to exit 0
    assert len(proc.stdout.strip().splitlines()) == 3


def test_routing_roots_are_configurable(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    routing_fixture(home, routing=tmp_path / "elsewhere", launch=tmp_path / "records")
    assert ar.scan_all(home) == []  # nothing at the default places
    proc = run_cli("--home", str(home), "--routing-root", str(tmp_path / "elsewhere"),
                   "--launch-dir", str(tmp_path / "records"))
    assert proc.returncode == 1
    assert len(proc.stdout.strip().splitlines()) == 3


def test_routing_roots_read_only_live_files(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    launch = home / ".local" / "state" / "routing" / "launch"
    routing = home / "code" / "routing"
    for name in ("old.json.bak", "restart.md", "sub/nested.json"):
        write(launch / name, MENTION)
    for name in ("other.json", "notes.md", "set.mcp.json.bak", "sub/x.mcp.json"):
        write(routing / "ops" / name, MENTION)
    write(routing / "bin" / "sub" / "nested", MENTION)
    assert ar.scan_all(home) == []


def test_routing_secret_named_and_binary_files_are_not_read(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    launch = home / ".local" / "state" / "routing" / "launch"
    routing = home / "code" / "routing"
    write(launch / "session_token.json", MARKER + " /home/x/code/AutoOS\n")
    write(routing / "bin" / "api_key.sh", MARKER + " /home/x/code/AutoOS\n")
    (routing / "bin").joinpath("blob").write_bytes(b"\0\1\2 /home/x/code/AutoOS\n")
    hits = ar.scan_all(home)
    assert_no_leak((MARKER,), repr(hits), run_cli("--home", str(home)).stdout)
    assert sorted(h[2] for h in hits) == ["SECRET-PATH", "SECRET-PATH"]


def test_explicit_roots_must_exist(ar, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    home.mkdir()
    for flag in ("--launch-dir", "--routing-root"):
        assert ar.main(["--home", str(home), flag, str(tmp_path / "absent")]) == 2
        assert flag in capsys.readouterr().err
    assert ar.main(["--home", str(home)]) == 0  # absent defaults are just absent
    (tmp_path / "file").write_text("x")
    for root in (tmp_path / "absent", tmp_path / "file"):   # the API, not only main(), refuses a bad explicit root
        with pytest.raises(ar.RootScanError):
            ar.scan_all(home, routing_root=root)
        with pytest.raises(ar.RootScanError):
            ar.scan_all(home, launch_dir=root)


@windows_skip
def test_symlinked_script_is_read_through_the_link(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    store = tmp_path / "store"
    write(store / "svc.sh", "#!/bin/sh\n" + MENTION)
    write(store / "clean.sh", "#!/bin/sh\necho clean\n")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "svc").symlink_to(store / "svc.sh")
    (home / ".local" / "bin" / "clean").symlink_to(store / "clean.sh")
    routing = home / "code" / "routing"
    (routing / "bin").mkdir(parents=True)
    (routing / "bin" / "svc").symlink_to(store / "svc.sh")
    hits = ar.scan_all(home)
    assert sorted((h[0], h[1]) for h in hits) == [
        ("router-bin", str(home / ".local" / "bin" / "svc")),
        ("routing-bin", str(routing / "bin" / "svc"))]
    assert {h[2] for h in hits} == {MENTION.strip()}


@windows_skip
def test_symlink_chains_and_relative_links_are_followed(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    bin_dir = home / ".local" / "bin"
    write(home / "lib" / "real.sh", MENTION)
    bin_dir.mkdir(parents=True)
    (bin_dir / "rel").symlink_to(Path("..") / ".." / "lib" / "real.sh")
    (bin_dir / "hop").symlink_to("rel")
    hits = ar.scan_all(home)
    assert sorted(Path(h[1]).name for h in hits) == ["hop", "rel"]


@windows_skip
def test_symlink_resolving_into_the_removed_tree_is_reported(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    bin_dir = home / ".local" / "bin"
    write(tmp_path / "AutoOS" / "lib" / "x.sh", "echo clean\n")
    bin_dir.mkdir(parents=True)
    (bin_dir / "b").symlink_to(tmp_path / "AutoOS" / "lib" / "x.sh")
    (bin_dir / "a").symlink_to("b")  # its own text says nothing; the chain ends in the tree
    hits = ar.scan_all(home)
    assert sorted(Path(h[1]).name for h in hits) == ["a", "b"]
    assert all(h[2].endswith("lib/x.sh") for h in hits)


@windows_skip
def test_symlink_targets_that_must_not_be_read(ar, tmp_path: Path) -> None:
    home = tmp_path / "h"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    write(tmp_path / "store" / "token.sh", MARKER + " /home/x/code/AutoOS\n")
    (tmp_path / "store" / "blob").write_bytes(b"\0\1 /home/x/code/AutoOS\n")
    (bin_dir / "secretish").symlink_to(tmp_path / "store" / "token.sh")
    (bin_dir / "binary").symlink_to(tmp_path / "store" / "blob")
    (bin_dir / "dangling").symlink_to(tmp_path / "store" / "missing")
    (bin_dir / "loop_a").symlink_to("loop_b")
    (bin_dir / "loop_b").symlink_to("loop_a")
    (bin_dir / "dir").symlink_to(tmp_path / "store", target_is_directory=True)
    hits = ar.scan_all(home)
    assert_no_leak((MARKER,), repr(hits), run_cli("--home", str(home)).stdout)
    assert [(Path(h[1]).name, h[2]) for h in hits] == [("secretish", "SECRET-PATH")]


def init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)


@windows_skip
@pytest.mark.parametrize("name", ["lïnk-é", "has space", "tab\there", "bad\udcffname"])
def test_odd_tracked_symlink_names_are_read_with_z(
    ar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Force git's default quoting (a global core.quotepath=false would hide the bug).
    for key, value in (("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", "core.quotepath"),
                       ("GIT_CONFIG_VALUE_0", "true")):
        monkeypatch.setenv(key, value)
    home, repo = tmp_path / "h", tmp_path / "repo"
    home.mkdir()
    init_repo(repo)
    try:
        os.symlink(os.fsencode("/home/x/code/AutoOS/tools/h.sh"),
                   os.path.join(os.fsencode(repo), os.fsencode(name)))
    except (OSError, ValueError):
        pytest.skip("this file system refuses the name")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    hits = ar.scan_all(home, repo=repo)
    assert [(h[0], h[1], h[2]) for h in hits] == [
        ("repo-symlink", str(repo / name), "/home/x/code/AutoOS/tools/h.sh")]
    proc = run_cli("--home", str(home), "--repo", str(repo))
    assert proc.returncode == 1 and proc.stderr == ""  # reported, no crash, not 'broken-link'
    assert "broken-link" not in proc.stdout


@windows_skip
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads every directory")
def test_unreadable_explicit_roots_are_a_scan_error(ar, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    home.mkdir()
    for flag, name in (("--launch-dir", "records"), ("--routing-root", "tree")):
        root = tmp_path / name
        (root / "ops").mkdir(parents=True)
        (root / "bin").mkdir()
        root.chmod(0)
        try:
            assert ar.main(["--home", str(home), flag, str(root)]) == 2
        finally:
            root.chmod(0o755)
        assert "cannot read" in capsys.readouterr().err
    tree = tmp_path / "tree2"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin").chmod(0)
    try:
        assert ar.main(["--home", str(home), "--routing-root", str(tree)]) == 2
    finally:
        (tree / "bin").chmod(0o755)
