"""The shared cargo target is capped and its cli-tests builds pruned before a push builds."""
import os
from pathlib import Path

import pytest

from plangraph.spawner import host_ready as h
from test_preflight import preflight, repo  # noqa: F401
from test_preflight_policy_prebuild import rig  # noqa: F401

NOW = 1_800_000_000


def build(target, name, age_s, now=NOW):
    path = target / "cli-tests" / name
    (path / "debug").mkdir(parents=True)
    (path / "debug/pg-spawnd").write_text("x" * 100)
    for entry in (path / "debug/pg-spawnd", path / "debug", path):
        os.utime(entry, (now - age_s, now - age_s))
    return path


def test_cli_tests_keeps_the_newest_n_checkout_builds(tmp_path):
    for index in range(6):
        build(tmp_path, f"h{index}", age_s=index * 60)
    removed = h.prune_cli_tests(tmp_path, keep=2, now=NOW)
    assert sorted(removed) == ["h2", "h3", "h4", "h5"]
    assert sorted(p.name for p in (tmp_path / "cli-tests").iterdir()) == ["h0", "h1"]


def test_cli_tests_drops_builds_untouched_for_a_day_even_within_n(tmp_path):
    build(tmp_path, "fresh", 3600)
    build(tmp_path, "stale", 25 * 3600)
    assert h.prune_cli_tests(tmp_path, keep=5, now=NOW) == ["stale"]


def test_a_recently_built_inner_file_keeps_the_build_alive(tmp_path):
    path = build(tmp_path, "busy", 30 * 3600)
    os.utime(path / "debug", (NOW - 60, NOW - 60))
    assert h.prune_cli_tests(tmp_path, keep=5, now=NOW) == []


def test_keep_comes_from_the_environment_and_a_missing_dir_is_fine(tmp_path, monkeypatch):
    assert h.prune_cli_tests(tmp_path) == []
    for index in range(4):
        build(tmp_path, f"h{index}", index * 60, now=__import__("time").time())
    monkeypatch.setenv(h.CLI_TESTS_KEEP_ENV, "1")
    assert len(h.prune_cli_tests(tmp_path)) == 3


def test_reclaim_removes_an_over_cap_target_and_keeps_an_under_cap_one(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    (target / "debug").mkdir(parents=True)
    (target / "debug/blob").write_text("x" * 50_000)
    h.reclaim_cargo_target(target, cap=1.0, wait=0)
    assert (target / "debug/blob").exists()
    h.reclaim_cargo_target(target, cap=1e-6, wait=0)   # 50 KB over a ~1 KB cap
    assert not target.exists()


def test_reclaim_refuses_while_a_build_holds_the_lease(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    target.mkdir()
    (target / "blob").write_text("x" * 50_000)
    with h.cargo_lease():
        with pytest.raises(OSError):
            h.reclaim_cargo_target(target, cap=1e-6, wait=0)
    assert (target / "blob").exists()


def test_preflight_trims_an_over_cap_target_before_the_first_build(rig, monkeypatch, capsys):
    repo, log, seen = rig
    target = repo / "target"
    (target / "debug").mkdir(parents=True)
    (target / "debug/old-artifact").write_text("x" * 100_000)
    build(target, "old", 40 * 3600, now=__import__("time").time())
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "0.00005")   # about 50 KB
    # The stub cargo records what it finds in the target when the build starts.
    cargo = repo / "bin/cargo"
    cargo.write_text(cargo.read_text().replace(
        "open(", "open(os.environ['CARGO_TARGET_DIR'] + '.seen', 'w').write(str(sorted(p.name for p in Path(os.environ['CARGO_TARGET_DIR']).rglob('*'))))\nopen(", 1))
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert "old-artifact" not in (repo / "target.seen").read_text()
    assert "old" not in (repo / "target.seen").read_text().replace("old-artifact", "")


def test_preflight_prunes_cli_tests_of_an_under_cap_target(rig, monkeypatch):
    repo, log, seen = rig
    target = repo / "target"
    for index in range(5):
        build(target, f"h{index}", index * 60, now=__import__("time").time())
    monkeypatch.setenv(h.CLI_TESTS_KEEP_ENV, "2")
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert sorted(p.name for p in (target / "cli-tests").iterdir()) == ["h0", "h1"]


def test_trim_is_never_fatal(repo, monkeypatch, capsys):
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo / "t"))
    (repo / "t").mkdir()
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "nonsense")
    preflight.trim_shared_target()
    assert "cargo target trim skipped" in capsys.readouterr().out
