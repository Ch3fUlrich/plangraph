"""The shared cargo target is capped and its cli-tests builds pruned before a push builds."""
import os
from pathlib import Path

import pytest

from plangraph.spawner import host_ready as h
from test_preflight import preflight, repo  # noqa: F401
from test_preflight_policy_prebuild import rig, shared_target  # noqa: F401

NOW = 1_800_000_000


def build(target, name, age_s, now=NOW):
    path = target / "cli-tests" / name
    (path / "debug").mkdir(parents=True)
    (path / "debug/pg-spawnd").write_text("x" * 100)
    (path / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())   # Cargo writes it in every target
    for entry in (path / "debug/pg-spawnd", path / "CACHEDIR.TAG", path / "debug", path):
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
    h.reclaim_cargo_target(target, cap=1.0, checkout=tmp_path / 'checkout', wait=0)
    assert (target / "debug/blob").exists()
    h.reclaim_cargo_target(target, cap=1e-6, checkout=tmp_path / 'checkout', wait=0)   # 50 KB over a ~1 KB cap
    assert not target.exists()


def test_reclaim_refuses_while_a_build_holds_the_lease(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    target.mkdir()
    (target / "blob").write_text("x" * 50_000)
    with h.cargo_lease():
        with pytest.raises(OSError):
            h.reclaim_cargo_target(target, cap=1e-6, checkout=tmp_path / 'checkout', wait=0)
    assert (target / "blob").exists()


def test_preflight_trims_an_over_cap_target_before_the_first_build(rig, monkeypatch, capsys):
    repo, log, seen = rig
    target = shared_target(repo)
    (target / "debug").mkdir(parents=True)
    (target / "debug/old-artifact").write_text("x" * 100_000)
    (target / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    build(target, "old", 40 * 3600, now=__import__("time").time())
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "0.00005")   # about 50 KB
    # The stub cargo records what it finds in the target when the build starts.
    cargo = repo / "bin/cargo"
    cargo.write_text(cargo.read_text().replace(
        "open(", "open(os.environ['PLANGRAPH_CARGO_TARGET_DIR'] + '.seen', 'w').write(str(sorted(p.name for p in Path(os.environ['PLANGRAPH_CARGO_TARGET_DIR']).rglob('*'))))\nopen(", 1))
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    seen_in_target = Path(str(target) + ".seen").read_text()
    assert "old-artifact" not in seen_in_target
    assert "old" not in seen_in_target.replace("old-artifact", "")


def test_preflight_prunes_cli_tests_of_an_under_cap_target(rig, monkeypatch):
    repo, log, seen = rig
    target = shared_target(repo)
    for index in range(5):
        build(target, f"h{index}", index * 60, now=__import__("time").time())
    monkeypatch.setenv(h.CLI_TESTS_KEEP_ENV, "2")
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert sorted(p.name for p in (target / "cli-tests").iterdir()) == ["h0", "h1"]


def test_trim_is_never_fatal(repo, monkeypatch, capsys):
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo / "t"))
    (repo / "t").mkdir()
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "nonsense")
    preflight.trim_shared_target(repo)
    assert "cargo target trim skipped" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["checkout", "inside-checkout", "home", "above-home", "root"])
def test_a_target_that_holds_the_checkout_or_home_is_never_reclaimed(tmp_path, monkeypatch, where):
    home = tmp_path / "users/me"
    checkout = home / "code/repo"
    checkout.mkdir(parents=True)
    (checkout / "src.py").write_text("x" * 50_000)
    monkeypatch.setenv("HOME", str(home))
    target = {"checkout": checkout, "inside-checkout": checkout / "target", "home": home,
              "above-home": tmp_path / "users", "root": Path("/")}[where]
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    with pytest.raises(ValueError, match="unsafe cache directory"):
        h.reclaim_cargo_target(target, cap=1e-9, checkout=checkout, wait=0)
    assert (checkout / "src.py").exists()


def test_a_symlinked_target_is_never_reclaimed(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "debug").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(link))
    with pytest.raises(ValueError, match="unsafe cache directory"):
        h.reclaim_cargo_target(link, cap=1e-9, checkout=tmp_path / "checkout", wait=0)
    assert (real / "debug").exists()


def test_a_directory_without_cargo_markers_is_not_removed_whole(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    target.mkdir()
    (target / "notes.txt").write_text("x" * 50_000)
    with pytest.raises(ValueError, match="not an identified cargo target"):
        h.reclaim_cargo_target(target, cap=1e-9, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert (target / "notes.txt").exists()
    (target / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    h.reclaim_cargo_target(target, cap=1e-9, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert not target.exists()


def test_preflight_never_deletes_a_checkout_configured_as_the_target(repo, monkeypatch, capsys):
    (repo / "src.py").write_text("x" * 50_000)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "1e-9")
    preflight.trim_shared_target(repo)
    assert (repo / "src.py").exists() and (repo / ".git").exists()
    assert "cargo target trim skipped" in capsys.readouterr().out


def test_a_git_checkout_with_a_debug_directory_is_not_a_cargo_target(tmp_path, monkeypatch):
    sibling = tmp_path / "other-checkout"
    (sibling / "debug").mkdir(parents=True)
    (sibling / ".git").mkdir()
    (sibling / "debug/notes").write_text("x" * 50_000)
    (sibling / "cli-tests/h0").mkdir(parents=True)   # even a look-alike build root
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(sibling))
    for cap in (1e-9, 100):
        with pytest.raises(ValueError, match="not an identified cargo target"):
            h.reclaim_cargo_target(sibling, cap=cap, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert (sibling / "debug/notes").exists() and (sibling / "cli-tests/h0").exists()


def test_a_bare_debug_directory_is_not_enough(tmp_path):
    (tmp_path / "debug").mkdir()
    assert not h.identified_cargo_target(tmp_path)
    (tmp_path / ".rustc_info.json").write_text('{"rustc_fingerprint": 1}')
    assert h.identified_cargo_target(tmp_path)


@pytest.mark.parametrize("marker", ["CACHEDIR.TAG", ".rustc_info.json", "cli-tests", "policy-bin"])
def test_marker_names_alone_are_placeholders_not_evidence(tmp_path, marker):
    """Ordinary data with a marker's name as a plain file or empty directory is not a cargo target."""
    (tmp_path / "notes.txt").write_text("x")
    if marker in ("cli-tests", "policy-bin"):
        (tmp_path / marker / "h0").mkdir(parents=True)
    else:
        (tmp_path / marker).write_text("placeholder")
    assert not h.identified_cargo_target(tmp_path)


def test_only_cargos_own_content_identifies_a_target(tmp_path):
    (tmp_path / "CACHEDIR.TAG").write_text("Signature: something-else")
    (tmp_path / ".rustc_info.json").write_text("{}")
    assert not h.identified_cargo_target(tmp_path)
    (tmp_path / ".rustc_info.json").write_text("not json")
    assert not h.identified_cargo_target(tmp_path)
    (tmp_path / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode() + "\n# created by cargo")
    assert h.identified_cargo_target(tmp_path)


def test_a_linked_marker_is_not_evidence(tmp_path):
    real = tmp_path / "real.tag"
    real.write_text(h.CACHEDIR_SIGNATURE.decode())
    target = tmp_path / "t"
    target.mkdir()
    (target / "CACHEDIR.TAG").symlink_to(real)
    assert not h.identified_cargo_target(target)


def test_a_build_root_counts_when_a_build_inside_holds_cargo_output(tmp_path):
    (tmp_path / "policy-bin/k0").mkdir(parents=True)
    assert not h.identified_cargo_target(tmp_path)
    (tmp_path / "policy-bin/k0/CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    assert h.identified_cargo_target(tmp_path)


@pytest.mark.parametrize("subdir", ["cli-tests", "policy-bin"])
def test_a_symlinked_build_root_is_not_pruned(tmp_path, subdir):
    outside = tmp_path / "outside"
    for name in ("h0", "h1", "h2"):
        (outside / name).mkdir(parents=True)
        os.utime(outside / name, (NOW - 100 * 3600, NOW - 100 * 3600))
    target = tmp_path / "t"
    target.mkdir()
    (target / subdir).symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        h.prune_builds(target, subdir, keep=1, now=NOW)
    assert sorted(p.name for p in outside.iterdir()) == ["h0", "h1", "h2"]


def test_reclaim_leaves_everything_alone_behind_a_symlinked_build_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    (outside / "h0").mkdir(parents=True)
    os.utime(outside / "h0", (NOW - 100 * 3600, NOW - 100 * 3600))
    target = tmp_path / "t"
    (target / "debug").mkdir(parents=True)
    (target / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    (target / "cli-tests").symlink_to(outside)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    with pytest.raises(ValueError, match="symbolic link"):
        h.reclaim_cargo_target(target, cap=1e-9, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert (outside / "h0").exists() and (target / "debug").exists()


def test_policy_bin_builds_are_pruned_with_cli_tests(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    now = __import__("time").time()
    for index in range(5):
        path = target / "policy-bin" / f"k{index}"
        (path / "debug").mkdir(parents=True)
        os.utime(path / "debug", (now - index * 60, now - index * 60))
        os.utime(path, (now - index * 60, now - index * 60))
    (target / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    h.reclaim_cargo_target(target, cap=100, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert sorted(p.name for p in (target / "policy-bin").iterdir()) == ["k0", "k1", "k2"]


@pytest.mark.parametrize("payload", ['["rustc_fingerprint"]', '"rustc_fingerprint"', '[{"rustc_fingerprint": 1}]', "null"])
def test_only_a_json_object_with_the_fingerprint_is_evidence(tmp_path, payload):
    (tmp_path / "notes.txt").write_text("x" * 50_000)
    (tmp_path / ".rustc_info.json").write_text(payload)
    assert not h.identified_cargo_target(tmp_path)
    with pytest.raises(ValueError, match="not an identified cargo target"):
        h.reclaim_cargo_target(tmp_path, cap=1e-9, checkout=tmp_path.parent / "checkout", wait=0, identified=True)
    assert (tmp_path / "notes.txt").exists()


def test_both_build_roots_are_checked_before_either_is_pruned(tmp_path, monkeypatch):
    target = tmp_path / "t"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    (target / "CACHEDIR.TAG").parent.mkdir(parents=True)
    (target / "CACHEDIR.TAG").write_text(h.CACHEDIR_SIGNATURE.decode())
    expired = build(target, "expired", 100 * 3600, now=__import__("time").time())
    outside = tmp_path / "outside"
    (outside / "h0").mkdir(parents=True)
    (target / "policy-bin").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        h.reclaim_cargo_target(target, cap=100, checkout=tmp_path / "checkout", wait=0, identified=True)
    assert expired.exists() and (outside / "h0").exists()
