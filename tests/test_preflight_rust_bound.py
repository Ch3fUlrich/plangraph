"""The rust stage bound and build jobs follow the plan and the host (LE-PREFLIGHT-RUST-STAGE-FIXED-600S-FOUR-JOBS)."""
import subprocess
import sys

import pytest

from test_preflight import preflight, repo  # noqa: F401


def crate(root, name, tests):
    (root / "rust" / name / "tests").mkdir(parents=True)
    for test in tests:
        (root / "rust" / name / "tests" / f"{test}.rs").write_text("")
    (root / "rust" / name / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n')


@pytest.mark.parametrize("test_command", [["cargo", "test"], ["cargo", "nextest", "run"]])
def test_planned_binaries_count_the_test_files_of_every_tested_crate(tmp_path, test_command):
    crate(tmp_path, "pg-spawnd", ["a", "b", "c"])
    crate(tmp_path, "pg-policy", ["x"])
    commands = [["cargo", "fmt", "--check"],
                ["cargo", "clippy", "-p", "pg-spawnd", "--all-targets"],
                [*test_command, "-p", "pg-spawnd", "-p", "pg-policy"]]
    # one lib/bin binary per crate plus every tests/*.rs file
    assert preflight.planned_test_binaries(commands, tmp_path) == 3 + 1 + 1 + 1


def test_a_standalone_manifest_counts_its_own_tests(tmp_path):
    crate(tmp_path, "worker", ["t1", "t2"])
    manifest = tmp_path / "rust/worker/Cargo.toml"
    assert preflight.planned_test_binaries(
        [["cargo", "test", "--manifest-path", str(manifest)]], tmp_path) == 3


def test_check_timeout_scales_for_nextest_without_stubbing_the_binary_count(tmp_path, monkeypatch):
    crate(tmp_path, "worker", [f"test_{i}" for i in range(40)])
    manifest = tmp_path / "rust/worker/Cargo.toml"
    monkeypatch.setattr(preflight, "host_load", lambda: (0.0, 32))
    command = ["cargo", "nextest", "run", "--manifest-path", str(manifest)]
    assert preflight.check_timeout(command, tmp_path, {}) == 615


@pytest.mark.parametrize("binaries,load,cores,expected", [
    (3, 0.0, 32, 600),            # small plan, idle host: the old floor
    (60, 0.0, 32, 900),           # 60 binaries x 15 s
    (60, 16.0, 32, 1350),         # factor 1 + load/cores
    (60, 64.0, 32, 1800),         # load above the core count counts as saturated
    (400, 0.0, 32, 3600),         # capped
])
def test_rust_bound_scales_with_binaries_and_load(binaries, load, cores, expected):
    assert preflight.rust_bound(binaries, load, cores) == pytest.approx(expected)
    assert preflight.MAX_RUST_TIMEOUT == 3600


def test_a_loaded_host_gets_a_bound_above_the_failed_655_seconds():
    """Train 1 ran 414 s idle and 655 s at load ~27 with five writers: the bound must cover it."""
    assert preflight.rust_bound(40, 27.0, 32) > 655


def test_check_timeout_uses_the_scaled_bound_for_cargo(tmp_path, monkeypatch):
    crate(tmp_path, "pg-spawnd", ["a"] * 1)
    monkeypatch.setattr(preflight, "host_load", lambda: (0.0, 32))
    assert preflight.check_timeout(["cargo", "test", "-p", "pg-spawnd"], tmp_path, {}) == 600
    monkeypatch.setattr(preflight, "planned_test_binaries", lambda commands, root: 100)
    monkeypatch.setattr(preflight, "host_load", lambda: (0.0, 32))
    assert preflight.check_timeout(["cargo", "test", "-p", "pg-spawnd"], tmp_path, {}) == 1500


@pytest.mark.parametrize("cores,load,expected", [
    (32, 0.0, 32), (32, 27.0, 5), (32, 30.0, 4), (32, 40.0, 4), (8, 1.0, 7), (2, 0.0, 4)])
def test_build_jobs_follow_the_free_cores_with_a_floor_of_four(cores, load, expected):
    assert preflight.build_jobs({}, cores, load) == expected


def test_a_configured_job_count_is_kept():
    assert preflight.build_jobs({"CARGO_BUILD_JOBS": "12"}, 32, 30.0) == 12
    assert preflight.build_jobs({"CARGO_BUILD_JOBS": "x"}, 32, 30.0) == 4


def test_a_timeout_names_the_test_binary_that_was_running(tmp_path, monkeypatch):
    script = tmp_path / "fake_cargo.py"
    script.write_text("import sys, time\n"
                      "print('     Running tests/codex_effort.rs (target/debug/deps/codex_effort-1a2b)', flush=True)\n"
                      "time.sleep(10)\n")
    monkeypatch.setattr(preflight, "check_timeout", lambda *a, **k: 0.5)
    code, text = preflight.run([sys.executable, str(script)], tmp_path, dict(__import__("os").environ))
    assert code == 1
    assert "check exceeded 0.5 second timeout" in text
    assert "running test binary: tests/codex_effort.rs" in text


def test_a_timeout_without_a_binary_line_says_nothing_about_one(tmp_path, monkeypatch):
    script = tmp_path / "quiet.py"
    script.write_text("import time\ntime.sleep(10)\n")
    monkeypatch.setattr(preflight, "check_timeout", lambda *a, **k: 0.5)
    code, text = preflight.run([sys.executable, str(script)], tmp_path, dict(__import__("os").environ))
    assert (code, text) == (1, "check exceeded 0.5 second timeout\n")


def test_running_binary_is_the_last_running_line():
    out = ("   Running unittests src/lib.rs (target/debug/deps/a-1)\n"
           "     Running tests/b.rs (target/debug/deps/b-2)\n")
    assert preflight.running_test_binary(out) == "tests/b.rs"
    assert preflight.running_test_binary(b"nothing") is None
    assert preflight.running_test_binary(None) is None
