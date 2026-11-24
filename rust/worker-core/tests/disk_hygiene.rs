#![cfg(target_os = "linux")]
use pg_worker_core::{
    disk_hygiene::{sweep, sweep_checked, Guards, Thresholds},
    host_command,
};
use std::{
    fs,
    path::Path,
    process::Command,
    time::{Duration, SystemTime},
};
fn git(repo: &Path, args: &[&str]) {
    host_command::output(
        pg_worker_core::git_test_support::command(repo)
            .args([
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args),
        Duration::from_secs(10),
    )
    .unwrap();
}
fn fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("main");
    let root = tmp.path().join("trees");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&root).unwrap();
    git(&repo, &["init", "-qb", "main"]);
    fs::write(repo.join(".gitignore"), "target/\nnode_modules/\n").unwrap();
    git(&repo, &["add", "."]);
    git(&repo, &["commit", "-qm", "one"]);
    git(&repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    for name in ["pinned", "dirty", "live", "clean"] {
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                name,
                root.join(name).to_str().unwrap(),
            ],
        );
        fs::create_dir(root.join(name).join("target")).unwrap();
        fs::write(root.join(name).join("target/cache"), [0u8; 64]).unwrap();
    }
    fs::write(root.join("dirty/untracked"), "valuable").unwrap();
    (tmp, repo, root)
}
#[test]
fn disk_hygiene_pinned_dirty_and_live_survive_real_cleanup() {
    let (_tmp, repo, root) = fixture();
    let guards = Guards {
        pinned: vec![root.join("pinned")],
        live: vec![root.join("live")],
    };
    let report = sweep(
        &repo,
        &root,
        &guards,
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    for name in ["pinned", "dirty", "live"] {
        assert!(root.join(name).join("target/cache").exists());
    }
    assert!(!root.join("clean/target").exists());
    assert!(report.alarm && report.freed_bytes >= 64);
    println!("cleanup freed_bytes={}", report.freed_bytes);
}

#[test]
fn final_guard_can_keep_a_cache_in_an_idle_worktree() {
    let (_tmp, repo, root) = fixture();
    let tree = root.join("clean");
    let now = SystemTime::now() + Duration::from_secs(24 * 3600 + 1);
    let guards = Guards {
        pinned: vec![root.join("pinned"), root.join("live")],
        live: vec![],
    };
    let run = |allow| {
        sweep_checked(
            &repo,
            &root,
            &guards,
            now,
            0,
            Thresholds::default(),
            true,
            |candidate| {
                assert_eq!(candidate, tree);
                Ok(allow)
            },
        )
        .unwrap()
    };
    let kept = run(false);
    assert_eq!(kept.freed_bytes, 0);
    assert!(kept
        .skipped
        .iter()
        .any(|line| line.contains("active work or build")));
    assert!(tree.join("target/cache").exists());
    assert_eq!(run(true).freed_bytes, 64);
    assert!(!tree.join("target").exists());
}
#[test]
fn disk_hygiene_ignored_non_cache_files_preserve_the_entire_tree() {
    let (_tmp, repo, root) = fixture();
    fs::write(
        repo.join(".git/info/exclude"),
        ".env\nscratch/\ntarget-backup/\n",
    )
    .unwrap();
    let files = [
        "clean/.env",
        "pinned/scratch/notes private",
        "live/target-backup/cache",
    ];
    for file in files {
        let path = root.join(file);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "valuable").unwrap();
    }
    for apply in [false, true] {
        let report = sweep(
            &repo,
            &root,
            &Guards::default(),
            SystemTime::now() + Duration::from_secs(90000),
            4_000_000_000,
            Thresholds::default(),
            apply,
        )
        .unwrap();
        assert!(report.actions.is_empty());
        assert_eq!(report.freed_bytes, 0);
        for file in files {
            assert_eq!(fs::read_to_string(root.join(file)).unwrap(), "valuable");
        }
        for tree in ["clean", "pinned", "live"] {
            assert!(root.join(tree).join("target/cache").exists());
            assert!(report
                .skipped
                .iter()
                .any(|reason| { reason.contains(tree) && reason.contains("ignored non-cache") }));
        }
    }
}
#[test]
fn disk_hygiene_vanished_worktree_is_reported_and_other_trees_are_cleaned() {
    let (_tmp, repo, root) = fixture();
    // Leave Git's registration behind to model a tree disappearing after listing.
    fs::remove_dir_all(root.join("pinned")).unwrap();
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(report
        .skipped
        .iter()
        .any(|reason| { reason.contains("pinned") && reason.contains("filesystem unavailable") }));
    assert!(!root.join("clean/target").exists());
    assert!(root.join("dirty/untracked").exists());
}
#[test]
fn disk_hygiene_dry_run_and_recent_worktrees_preserve_everything() {
    let (_tmp, repo, root) = fixture();
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        false,
    )
    .unwrap();
    assert!(!report.actions.is_empty() && report.freed_bytes == 0);
    assert!(root.join("clean/target/cache").exists());
    let recent = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(23 * 3600),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(recent.actions.is_empty() && root.join("clean/target/cache").exists());
    let low = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        10_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(!low.alarm && low.actions.is_empty());
}

#[test]
fn disk_hygiene_unmerged_local_branch_keeps_worktree_but_reclaims_ignored_cache() {
    let (_tmp, repo, root) = fixture();
    let clean = root.join("clean");
    fs::write(clean.join("valuable"), "unmerged").unwrap();
    git(&clean, &["add", "valuable"]);
    git(&clean, &["commit", "-qm", "local"]);
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(clean.join("valuable").exists() && !clean.join("target").exists());
    assert!(report.freed_bytes >= 64);
}

#[test]
fn disk_hygiene_missing_or_malformed_pin_and_live_state_abort() {
    let tmp = tempfile::tempdir().unwrap();
    let pins = tmp.path().join("pins.json");
    assert!(Guards::load(&pins, tmp.path()).is_err());
    fs::write(&pins, "[]").unwrap();
    fs::create_dir_all(tmp.path().join("runs/run-active")).unwrap();
    fs::write(
        tmp.path().join("runs/run-active/record.json"),
        "{\"status\":\"running\"}",
    )
    .unwrap();
    assert!(Guards::load(&pins, tmp.path()).is_err());
}
#[test]
#[cfg(unix)]
fn disk_hygiene_symlinked_caches_never_touch_external_data() {
    let (_tmp, repo, root) = fixture();
    let outside = tempfile::tempdir().unwrap();
    fs::write(outside.path().join("keep"), "keep").unwrap();
    fs::remove_file(root.join("clean/target/cache")).unwrap();
    fs::remove_dir(root.join("clean/target")).unwrap();
    std::os::unix::fs::symlink(outside.path(), root.join("clean/target")).unwrap();
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(report
        .skipped
        .iter()
        .any(|s| s.contains("unsafe cache path")));
    assert!(outside.path().join("keep").exists());
    assert!(root.join("clean").exists());
}

#[test]
fn disk_hygiene_missing_origin_ref_never_deletes_unmerged_branch() {
    let (_tmp, repo, root) = fixture();
    let clean = root.join("clean");
    fs::write(clean.join("valuable"), "unmerged").unwrap();
    git(&clean, &["add", "valuable"]);
    git(&clean, &["commit", "-qm", "local"]);
    git(&repo, &["config", "branch.clean.remote", "origin"]);
    git(&repo, &["config", "branch.clean.merge", "refs/heads/clean"]);
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(clean.join("valuable").exists());
    assert_eq!(report.freed_bytes, 192);
}
#[test]
fn disk_hygiene_cache_bytes_exclude_tracked_source() {
    let (_tmp, repo, root) = fixture();
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert_eq!(report.freed_bytes, 192);
    assert!(root.join("clean/.gitignore").exists());
}
#[test]
fn disk_hygiene_git_probe_failure_skips_one_tree() {
    let (_tmp, repo, root) = fixture();
    fs::write(root.join("pinned/.git"), "invalid").unwrap();
    let result = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    );
    assert!(result.is_ok());
    let report = result.unwrap();
    assert!(!root.join("clean/target").exists());
    assert!(report.skipped.iter().any(|s| s.contains("pinned")));
}
#[test]
fn disk_hygiene_cli_errors_replace_stale_report() {
    let tmp = tempfile::tempdir().unwrap();
    let state = tmp.path();
    fs::write(state.join("disk-hygiene.json"), r#"{"alarm":false}"#).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args([
            "--repo", "/missing", "--root", "/missing", "--pins", "/missing", "--state",
        ])
        .arg(state)
        .output()
        .unwrap();
    assert!(!output.status.success());
    let row: serde_json::Value =
        serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
    assert_eq!(row["alarm"], true);
    assert!(row["error"].is_string());
}
#[test]
fn disk_hygiene_cli_refuses_conflicting_modes() {
    let tmp = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["--apply", "--dry-run", "--state"])
        .arg(tmp.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("conflicting"));
}

#[test]
fn disk_hygiene_guard_failure_replaces_stale_healthy_report() {
    let tmp = tempfile::tempdir().unwrap();
    fs::write(tmp.path().join("disk-hygiene.json"), r#"{"alarm":false}"#).unwrap();
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    fs::create_dir_all(tmp.path().join("runs/stray")).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["--repo", "/missing", "--root"])
        .arg(tmp.path())
        .arg("--pins")
        .arg(pins)
        .arg("--state")
        .arg(tmp.path())
        .output()
        .unwrap();
    assert!(!output.status.success());
    let row: serde_json::Value =
        serde_json::from_slice(&fs::read(tmp.path().join("disk-hygiene.json")).unwrap()).unwrap();
    assert_eq!(row["alarm"], true);
    assert!(row["error"]
        .as_str()
        .unwrap()
        .contains("record unavailable"));
}

#[test]
fn disk_hygiene_large_ignored_cache_does_not_overflow_git_probe() {
    let (_tmp, repo, root) = fixture();
    for i in 0..16000 {
        fs::write(
            root.join("clean/target")
                .join(format!("{i:05}{}", "x".repeat(80))),
            "x",
        )
        .unwrap();
    }
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        4_000_000_000,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(!root.join("clean/target").exists(), "{:?}", report.skipped);
    assert_eq!(report.freed_bytes, 16192);
}

#[test]
fn legacy_and_nested_python_caches_are_reclaimed() {
    let (_tmp, repo, root) = fixture();
    fs::write(
        repo.join(".git/info/exclude"),
        "__pycache__/\n.pytest_cache/\n",
    )
    .unwrap();
    let caches = [
        "rust/target",
        "src/pkg/__pycache__",
        ".pytest_cache",
        "tests/.pytest_cache",
    ];
    for cache in caches {
        let path = root.join("clean").join(cache);
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("cache"), [0u8; 42]).unwrap();
    }
    for apply in [false, true] {
        let report = sweep(
            &repo,
            &root,
            &Guards::default(),
            SystemTime::now() + Duration::from_secs(90000),
            0,
            Thresholds::default(),
            apply,
        )
        .unwrap();
        assert!(
            report.skipped.iter().all(|s| !s.contains("clean")),
            "{:?}",
            report.skipped
        );
        for cache in caches {
            assert_eq!(root.join("clean").join(cache).exists(), !apply);
            assert!(report.actions.iter().any(|s| s.contains(cache)));
        }
    }
}

#[test]
fn alarm_uses_the_spawner_five_gib_floor() {
    let (_tmp, repo, root) = fixture();
    let floor = 5 * 1024 * 1024 * 1024;
    assert_eq!(Thresholds::default().alarm_free_bytes, floor);
    for (free, alarm) in [(5_100_000_000, true), (floor - 1, true), (floor, false)] {
        let report = sweep(
            &repo,
            &root,
            &Guards::default(),
            SystemTime::now(),
            free,
            Thresholds::default(),
            false,
        )
        .unwrap();
        assert_eq!(report.alarm, alarm);
    }
}

#[test]
fn tracked_python_cache_is_protected_even_with_pathspec_characters() {
    let (_tmp, repo, root) = fixture();
    let tree = root.join("clean");
    let cache = tree.join("src/[pkg]/__pycache__");
    fs::create_dir_all(&cache).unwrap();
    fs::write(cache.join("tracked"), "keep").unwrap();
    git(&tree, &["add", "--", "."]);
    git(&tree, &["commit", "-qm", "tracked fixture"]);
    let report = sweep(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(90000),
        0,
        Thresholds::default(),
        true,
    )
    .unwrap();
    assert!(cache.join("tracked").exists());
    assert!(tree.join("target/cache").exists());
    assert!(report
        .skipped
        .iter()
        .any(|s| s.contains("clean") && s.contains("tracked cache")));
}

#[test]
fn disk_hygiene_cli_trims_over_cap_idle_pool_slots_beside_the_shared_target() {
    let (tmp, repo, root) = fixture();
    let primary = tmp.path().join("cache/cargo-target");
    let slot = tmp.path().join("cache/cargo-target-pool/slot-1");
    fs::create_dir_all(slot.join("debug")).unwrap();
    fs::write(slot.join("debug/blob"), vec![0u8; 16384]).unwrap();
    fs::create_dir_all(&primary).unwrap();
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::create_dir_all(tmp.path().join("home")).unwrap();
    fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
    // Hosts without readable /proc entries fall back to this reader: no process cwds.
    let reader = tmp.path().join("process-cwds");
    pg_worker_core::executable_test_support::write_executable(
        &reader,
        "#!/bin/sh\necho '[[],false]'\n",
    );
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["--apply", "--shared-target-cap-bytes", "2048", "--repo"])
        .arg(&repo)
        .arg("--root")
        .arg(&root)
        .arg("--pins")
        .arg(&pins)
        .arg("--tmp-root")
        .arg(tmp.path().join("tmp-sweep"))
        .arg("--state")
        .arg(&state)
        .env("HOME", tmp.path().join("home"))
        .env("PG_PROCESS_CWD_READER", &reader)
        .env("PLANGRAPH_CARGO_TARGET_DIR", &primary)
        .output()
        .unwrap();
    let row: serde_json::Value =
        serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
    assert!(output.status.success(), "{row}");
    assert!(
        !slot.join("debug/blob").exists(),
        "idle over-cap slot must be trimmed: {row}"
    );
    assert!(row["actions"]
        .as_array()
        .unwrap()
        .iter()
        .any(|a| a.as_str().is_some_and(|a| a.contains("pool_"))));
}

#[test]
fn old_fast_worktrees_are_swept_only_when_idle_and_disposable() {
    let (_tmp, repo, root) = fixture();
    fs::write(repo.join(".git/info/exclude"), ".env\n").unwrap();
    for name in ["idle-fast", "live-fast", "private-fast"] {
        let tree = root.join(name);
        git(
            &repo,
            &[
                "worktree",
                "add",
                "--detach",
                tree.to_str().unwrap(),
                "HEAD",
            ],
        );
        fs::create_dir(tree.join("target")).unwrap();
        fs::write(tree.join("target/build"), [0; 128]).unwrap();
    }
    fs::write(root.join("private-fast/.env"), "valuable").unwrap();
    let live = root.join("live-fast");
    let mut child = Command::new("sleep")
        .arg("60")
        .current_dir(&live)
        .spawn()
        .unwrap();
    let process_cwd = format!("/proc/{}/cwd", child.id());
    let result = sweep_checked(
        &repo,
        &root,
        &Guards::default(),
        SystemTime::now() + Duration::from_secs(3 * 3600),
        u64::MAX,
        Thresholds::default(),
        true,
        |tree| Ok(!fs::read_link(&process_cwd)?.starts_with(tree)),
    );
    child.kill().unwrap();
    child.wait().unwrap();
    let report = result.unwrap();
    assert!(!root.join("idle-fast").exists(), "{report:?}");
    assert!(live.join("target/build").is_file());
    assert!(root.join("private-fast/.env").is_file());
}

/// Hosts run `pg-disk-hygiene` from a user unit whose `WorkingDirectory` is the main checkout:
/// the unit pins the checkout, yet its idle `rust/target` must still be removed (and one the
/// unit names itself must not).
#[test]
fn a_unit_pinning_the_checkout_root_does_not_stop_the_idle_main_target_sweep() {
    for pin_target_itself in [false, true] {
        let (tmp, repo, root) = fixture();
        let target = repo.join("rust/target");
        fs::write(repo.join(".git/info/exclude"), "rust/target/\n").unwrap();
        fs::create_dir_all(target.join("debug")).unwrap();
        fs::write(target.join("debug/blob"), [0u8; 64]).unwrap();
        fs::write(
            target.join("CACHEDIR.TAG"),
            b"Signature: 8a477f597d28d172789f06886806bc55",
        )
        .unwrap();
        let old = SystemTime::now() - Duration::from_secs(7200);
        for p in [
            target.join("debug/blob"),
            target.join("CACHEDIR.TAG"),
            target.join("debug"),
            target.clone(),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
        let home = tmp.path().join("home");
        let units = home.join(".config/systemd/user");
        fs::create_dir_all(&units).unwrap();
        let named = if pin_target_itself { &target } else { &repo };
        fs::write(
            units.join("plangraph-fixture.service"),
            format!(
                "[Service]\nWorkingDirectory={}\nExecStart=/bin/true\n",
                named.display()
            ),
        )
        .unwrap();
        fs::create_dir_all(tmp.path().join("cache/cargo-target")).unwrap();
        let pins = tmp.path().join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let state = tmp.path().join("state");
        fs::create_dir_all(&state).unwrap();
        fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
        let reader = tmp.path().join("process-cwds");
        pg_worker_core::executable_test_support::write_executable(
            &reader,
            "#!/bin/sh\necho '[[],false]'\n",
        );
        let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
            .args(["--apply", "--repo"])
            .arg(&repo)
            .arg("--root")
            .arg(&root)
            .arg("--pins")
            .arg(&pins)
            .arg("--tmp-root")
            .arg(tmp.path().join("tmp-sweep"))
            .arg("--state")
            .arg(&state)
            .env("HOME", &home)
            .env_remove("XDG_CONFIG_HOME")
            .env("PG_PROCESS_CWD_READER", &reader)
            .env(
                "PLANGRAPH_CARGO_TARGET_DIR",
                tmp.path().join("cache/cargo-target"),
            )
            .output()
            .unwrap();
        let row: serde_json::Value =
            serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
        assert!(output.status.success(), "{row}");
        assert_eq!(
            target.exists(),
            pin_target_itself,
            "pin on the target itself: {pin_target_itself}: {row} {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

/// A shared target configured under a legacy container (`~/.cache/cargo-target/…`) is the
/// host's live target: the legacy sweep must keep it however long it sat idle, while a
/// neighbouring idle legacy target is removed.
#[test]
fn the_configured_shared_target_under_a_legacy_container_is_never_swept() {
    let (tmp, repo, root) = fixture();
    let home = tmp.path().join("home");
    let container = home.join(".cache/cargo-target");
    let old = SystemTime::now() - Duration::from_secs(3 * 86400);
    for name in ["shared", "batch23"] {
        let dir = container.join(name);
        fs::create_dir_all(dir.join("debug")).unwrap();
        fs::write(dir.join(".rustc_info.json"), b"{}").unwrap();
        fs::write(dir.join("debug/blob"), [0u8; 64]).unwrap();
        for p in [
            dir.join("debug/blob"),
            dir.join(".rustc_info.json"),
            dir.join("debug"),
            dir.clone(),
        ] {
            fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
    }
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    fs::create_dir_all(tmp.path().join("tmp-sweep")).unwrap();
    let reader = tmp.path().join("process-cwds");
    pg_worker_core::executable_test_support::write_executable(
        &reader,
        "#!/bin/sh\necho '[[],false]'\n",
    );
    let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
        .args(["--apply", "--repo"])
        .arg(&repo)
        .arg("--root")
        .arg(&root)
        .arg("--pins")
        .arg(&pins)
        .arg("--tmp-root")
        .arg(tmp.path().join("tmp-sweep"))
        .arg("--state")
        .arg(&state)
        .env("HOME", &home)
        .env_remove("XDG_CONFIG_HOME")
        .env("PG_PROCESS_CWD_READER", &reader)
        .env("PLANGRAPH_CARGO_TARGET_DIR", container.join("shared"))
        .output()
        .unwrap();
    let row: serde_json::Value =
        serde_json::from_slice(&fs::read(state.join("disk-hygiene.json")).unwrap()).unwrap();
    assert!(output.status.success(), "{row}");
    assert!(container.join("shared/debug/blob").exists(), "{row}");
    assert!(!container.join("batch23").exists(), "{row}");
}
