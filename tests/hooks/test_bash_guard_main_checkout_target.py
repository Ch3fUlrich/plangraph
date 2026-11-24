"""A cargo build inside a main checkout without a shared target is refused (LE-CODING-VM-DISK-EMERGENCY-FIFTH-UNCAPPED-TARGETS)."""
import json
import subprocess

import pytest

from bash_guard_support import HOOK, guard  # noqa: F401
import sys


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv("CARGO_BUILD_TARGET_DIR", raising=False)
    main = tmp_path / "code" / "proj"
    (main / "rust").mkdir(parents=True)
    git(main, "init", "-q")
    (main / "rust" / "Cargo.toml").write_text("[workspace]\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "init")
    work = main / ".claude" / "worktrees" / "lane"
    git(main, "worktree", "add", "-q", str(work), "-b", "lane/x")
    return main, work


def hook(command, cwd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})
    return subprocess.run([sys.executable, "-I", str(HOOK)], input=payload.encode(), capture_output=True,
                          timeout=60)


@pytest.mark.parametrize("command", [
    "cargo build", "cargo test -p pg-worker-core", "cargo +nightly clippy --all-targets",
    "nice -n 10 cargo check", "env FOO=1 cargo run", "bash -c 'cargo build'",
    "CARGO_TARGET_DIR=rust/target cargo build", "cargo build --target-dir rust/target",
    "cargo test --target-dir=./rust/target",
    "cargo -q build", "cargo --locked test", "cargo --offline clippy", "cargo +nightly --color never build",
    "cargo -Z unstable-options check", "cargo --config build.jobs=2 test",
    "nice bash -c 'cargo build'", "timeout 600 bash -c 'cargo test'", "eval 'cargo build'",
    "nice -n 5 eval 'cargo -q build'", "time sh -c 'cargo check'", "env FOO=1 bash -lc 'cargo test'",
    "CARGO_TARGET_DIR=rust/target nice bash -c 'cargo build'",
    "pgtest cargo test", "pgtest cargo build", "pgtest.py cargo test -p x",
    "python3 tools/pgtest.py cargo build", "pgtest bash -c 'cargo test'",
])
def test_cargo_without_a_shared_target_is_refused_in_a_main_checkout(repos, command):
    main, _ = repos
    result = hook(command, main / "rust")
    assert result.returncode == 2, result.stderr
    assert b"shared target" in result.stderr and b"cargo-target" in result.stderr


@pytest.mark.parametrize("command", [
    "CARGO_TARGET_DIR=/var/cache/shared cargo build", "env CARGO_TARGET_DIR=/var/cache/shared cargo test",
    "cargo build --target-dir /var/cache/shared", "cargo fmt --check", "cargo --version",
    "echo cargo build", "bash -c 'CARGO_TARGET_DIR=/var/cache/shared cargo test'",
    "cargo -q fmt", "cargo --locked --version", "nice bash -c 'cargo fmt --check'",
    "CARGO_TARGET_DIR=/var/cache/shared nice bash -c 'cargo build'",
    "env CARGO_TARGET_DIR=/var/cache/shared timeout 9 bash -c 'cargo test'",
    "eval 'CARGO_TARGET_DIR=/var/cache/shared cargo -q build'",
])
def test_cargo_with_an_outside_target_or_no_build_is_allowed(repos, command):
    main, _ = repos
    assert hook(command, main / "rust").returncode == 0


def test_cargo_in_a_worktree_under_claude_worktrees_is_refused_without_a_shared_target(repos):
    """A writer worktree held a 19 GB rust/target on dell: the same refusal applies there."""
    _, work = repos
    for command in ("cargo build", "cargo test -p x", "CARGO_TARGET_DIR=target cargo build"):
        result = hook(command, work / "rust")
        assert result.returncode == 2 and b"cargo-target" in result.stderr, command
    assert hook("CARGO_TARGET_DIR=/var/cache/shared cargo build", work / "rust").returncode == 0
    assert hook("cargo build --target-dir /var/cache/shared", work).returncode == 0


def test_cargo_in_a_linked_worktree_elsewhere_is_not_refused(repos, tmp_path):
    main, _ = repos
    elsewhere = tmp_path / "runs" / "run-1" / "worktree"
    git(main, "worktree", "add", "-q", str(elsewhere), "-b", "other/y")
    assert hook("cargo build", elsewhere / "rust").returncode == 0


def test_cargo_outside_any_repository_is_allowed(tmp_path):
    assert hook("cargo build", tmp_path).returncode == 0


def test_a_target_dir_environment_inside_the_main_checkout_is_refused(repos, monkeypatch):
    main, _ = repos
    monkeypatch.setenv("CARGO_TARGET_DIR", str(main / "rust" / "target"))
    assert hook("cargo build", main / "rust").returncode == 2
    monkeypatch.setenv("CARGO_TARGET_DIR", str(main.parent / "elsewhere"))
    assert hook("cargo build", main / "rust").returncode == 0


@pytest.mark.parametrize("template", [
    "cargo -C {main}/rust build",
    "cargo -q -C {main}/rust test",
    "cargo build --manifest-path {main}/rust/Cargo.toml",
    "cargo test --manifest-path={main}/rust/Cargo.toml",
    "cd {main} && cargo build",
    "cd {main}/rust; cargo test -p x",
    "bash -c 'cd {main} && cargo build'",
    "nice bash -c 'cd {main}/rust && cargo clippy'",
    "cd {main} && CARGO_TARGET_DIR=rust/target cargo build",
    "cargo -C {main}/rust build --target-dir target",
])
def test_cargo_aimed_at_a_main_checkout_from_outside_is_refused(repos, tmp_path, template):
    main, _ = repos
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = hook(template.format(main=main), outside)
    assert result.returncode == 2, (template, result.stderr)
    assert b"cargo-target" in result.stderr


@pytest.mark.parametrize("template", [
    "cargo -C {main}/rust build --target-dir /var/cache/shared",
    "CARGO_TARGET_DIR=/var/cache/shared cargo build --manifest-path {main}/rust/Cargo.toml",
    "cd {main} && CARGO_TARGET_DIR=/var/cache/shared cargo build",
    "cd {main} && cargo fmt --check",
    "cargo build --manifest-path {out}/Cargo.toml",
])
def test_cargo_aimed_at_a_main_checkout_with_an_outside_target_is_allowed(repos, tmp_path, template):
    main, _ = repos
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert hook(template.format(main=main, out=outside), outside).returncode == 0


def test_cd_out_of_a_main_checkout_leaves_the_refusal_behind(repos, tmp_path):
    main, _ = repos
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert hook(f"cd {outside} && cargo build", main / "rust").returncode == 0
    assert hook(f"cd {outside} && cd {main} && cargo build", outside).returncode == 2


@pytest.mark.parametrize("template", [
    "cd /tmp && CARGO_TARGET_DIR={main}/rust/target cargo build",
    "CARGO_TARGET_DIR={main}/rust/target cargo test",
    "cargo build --target-dir {main}/rust/target",
    "cargo build --target-dir={main}/target",
    "cargo -C {main}/rust build --target-dir /tmp/elsewhere/../../{main}/rust/target",
    "pgtest cargo build --target-dir {main}/rust/target",
    "CARGO_BUILD_TARGET_DIR={main}/rust/target cargo check",
])
def test_cargo_whose_target_resolves_into_a_main_checkout_is_refused_from_any_cwd(repos, tmp_path, template):
    main, _ = repos
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = hook(template.format(main=main), outside)
    assert result.returncode == 2, (template, result.stderr)
    assert b"cargo-target" in result.stderr


def test_cargo_whose_target_resolves_into_an_agent_worktree_is_refused_from_any_cwd(repos, tmp_path):
    _, work = repos
    result = hook(f"CARGO_TARGET_DIR={work}/rust/target cargo build", tmp_path)
    assert result.returncode == 2 and b"cargo-target" in result.stderr


@pytest.mark.parametrize("template", [
    "pgtest cargo test --target-dir /var/cache/shared",
    "CARGO_TARGET_DIR=/var/cache/shared pgtest cargo build",
    "python3 tools/pgtest.py cargo test --target-dir /var/cache/shared",
    "pgtest cargo fmt --check",
    "cd /tmp && CARGO_TARGET_DIR=/var/cache/shared cargo build",
    "cargo build --target-dir {out}/target",
])
def test_pgtest_and_unrelated_targets_stay_allowed(repos, tmp_path, template):
    main, _ = repos
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert hook(template.format(main=main, out=outside), main / "rust").returncode == 0, template


def test_a_repository_at_the_home_directory_does_not_make_the_shared_target_a_main_checkout(tmp_path, monkeypatch):
    """The workstation's home directory is itself a Git repository; the shared target lives below it."""
    home = tmp_path / "home"
    (home / ".cache/plangraph/cargo-target").mkdir(parents=True)
    git(home, "init", "-q")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    shared = home / ".cache/plangraph/cargo-target"
    assert hook(f"CARGO_TARGET_DIR={shared} cargo build", tmp_path).returncode == 0
    assert hook(f"cargo build --target-dir {shared}", home).returncode == 0
    project = home / "code" / "proj"
    (project / "rust").mkdir(parents=True)
    git(project, "init", "-q")
    assert hook(f"CARGO_TARGET_DIR={shared} cargo build", project / "rust").returncode == 0
    assert hook("cargo build", project / "rust").returncode == 2


def test_the_refusal_names_the_configured_data_drive_target_not_the_home_cache(repos, monkeypatch):
    main, work = repos
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", "/mnt/data/pg-cache")
    for cwd in (main / "rust", work / "rust"):
        result = hook("cargo build", cwd)
        assert result.returncode == 2
        assert b"/mnt/data/pg-cache" in result.stderr and b".cache/plangraph" not in result.stderr
        assert b"run it in a worktree" not in result.stderr
    monkeypatch.delenv("PLANGRAPH_CARGO_TARGET_DIR")
    assert b"~/.cache/plangraph/cargo-target" in hook("cargo build", main / "rust").stderr


@pytest.mark.parametrize("command", [
    "env -u CARGO_TARGET_DIR cargo build", "env --unset CARGO_TARGET_DIR cargo build",
    "env --unset=CARGO_TARGET_DIR cargo test", "unset CARGO_TARGET_DIR; cargo build",
    "unset CARGO_TARGET_DIR CARGO_BUILD_TARGET_DIR && cargo check", "env -i PATH=/usr/bin cargo build",
    "bash -c 'unset CARGO_TARGET_DIR; cargo build'", "env -u CARGO_TARGET_DIR -u CARGO_BUILD_TARGET_DIR cargo clippy",
])
def test_unsetting_the_target_in_the_command_leaves_a_main_checkout_build_refused(repos, monkeypatch, command):
    """The hook's own CARGO_TARGET_DIR is not what cargo sees after `env -u`, `env -i` or `unset`."""
    main, _ = repos
    monkeypatch.setenv("CARGO_TARGET_DIR", "/var/cache/shared")
    result = hook(command, main / "rust")
    assert result.returncode == 2, (command, result.stderr)


@pytest.mark.parametrize("command", [
    "env -u RUSTFLAGS cargo build", "unset RUSTFLAGS; cargo build",
    "env -i CARGO_TARGET_DIR=/var/cache/shared cargo build", "unset CARGO_TARGET_DIR; CARGO_TARGET_DIR=/var/cache/shared cargo build",
])
def test_unrelated_unsets_and_a_target_given_after_the_unset_stay_allowed(repos, monkeypatch, command):
    main, _ = repos
    monkeypatch.setenv("CARGO_TARGET_DIR", "/var/cache/shared")
    assert hook(command, main / "rust").returncode == 0, command
