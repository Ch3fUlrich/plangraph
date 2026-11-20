"""A pre-push Rust step names a missing protoc at once instead of failing in a build script."""
import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "tools/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

LOCK_PROST = '[[package]]\nname = "prost-wkt-types"\nversion = "0.6.0"\n'
LOCK_PLAIN = '[[package]]\nname = "serde"\nversion = "1.0.0"\n'


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "rust/worker").mkdir(parents=True)
    (tmp_path / "rust/Cargo.lock").write_text(LOCK_PLAIN)
    (tmp_path / "rust/worker/Cargo.lock").write_text(LOCK_PROST)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PROTOC", raising=False)
    return tmp_path


def worker_test(tree):
    return ["cargo", "test", "--manifest-path", str(tree / "rust/worker/Cargo.toml")]


def fake_protoc(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "protoc"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_only_a_build_of_a_prost_manifest_needs_protoc(tree):
    assert preflight.needs_protoc(tree, [worker_test(tree)])
    assert not preflight.needs_protoc(tree, [["cargo", "fmt", *worker_test(tree)[2:], "--check"]])
    assert not preflight.needs_protoc(tree, [["cargo", "test", "-p", "plain"]])
    assert not preflight.needs_protoc(tree, [])


def test_protoc_is_found_in_local_bin_when_the_unit_path_lacks_it(tree):
    found = fake_protoc(tree / "home/.local/bin")
    env = {"PATH": "/usr/bin-none"}
    preflight.require_protoc(tree, [worker_test(tree)], env)
    assert env["PROTOC"] == str(found)


def test_protoc_variable_wins_and_must_be_executable(tree):
    given = fake_protoc(tree / "elsewhere")
    fake_protoc(tree / "home/.local/bin")
    env = {"PATH": "", "PROTOC": str(given)}
    preflight.require_protoc(tree, [worker_test(tree)], env)
    assert env["PROTOC"] == str(given)
    given.chmod(0o644)
    with pytest.raises(preflight.FailFamily, match=r"PROTOC=.*not an executable"):
        preflight.require_protoc(tree, [worker_test(tree)], {"PATH": "", "PROTOC": str(given)})


def test_missing_protoc_fails_fast_with_a_named_message(tree):
    start = time.monotonic()
    with pytest.raises(preflight.FailFamily, match=r"protoc not found.*PROTOC.*\.local/bin"):
        preflight.require_protoc(tree, [worker_test(tree)], {"PATH": ""})
    assert time.monotonic() - start < 5
    # A crate set that needs no protoc is not refused.
    preflight.require_protoc(tree, [["cargo", "test", "-p", "plain"]], {"PATH": ""})


def test_push_without_protoc_refuses_before_any_cargo_command(tree, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    monkeypatch.delenv("PLANGRAPH_SCRUB_WORDS", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-only")
    calls = []
    monkeypatch.setattr(preflight, "run", lambda command, cwd, env: calls.append(command) or (0, ""))
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [worker_test(tree)])
    assert preflight.main(["--base", "HEAD", "--fast"], tree) == 1
    out = capsys.readouterr().out
    assert "rust: FAIL: protoc not found" in out
    assert not any(command[0] == "cargo" for command in calls)
