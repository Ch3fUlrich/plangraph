#![cfg(target_os = "linux")]
use pg_worker_core::disk_hygiene::{sweep, Guards, Thresholds};
use std::{
    fs,
    path::Path,
    process::Command,
    time::{Duration, SystemTime},
};

fn cache_fixture() -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    let git = |args: &[&str]| {
        let output = pg_worker_core::git_test_support::command(root.path())
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    };
    git(&["init", "-q"]);
    fs::write(root.path().join(".gitignore"), "/target/\n").unwrap();
    git(&["add", ".gitignore"]);
    git(&["commit", "-qm", "cache fixture"]);
    git(&["worktree", "add", "-qb", "ordinary", "ordinary"]);
    let cache = root.path().join("ordinary/target");
    fs::create_dir(&cache).unwrap();
    fs::write(cache.join("artifact"), b"cache").unwrap();
    root
}

fn assert_thresholds(root: &Path, limits: Thresholds, cases: &[(u64, bool, bool)]) {
    let now = SystemTime::now() + Duration::from_secs(25 * 3600);
    // Worktree discovery runs at every free-space level. The dry-run action
    // for an eligible ordinary cache shows whether pressure triggered cleanup.
    for &(free, alarm, cleanup) in cases {
        let report = sweep(root, root, &Guards::default(), now, free, limits, false).unwrap();
        assert_eq!(report.alarm, alarm, "{report:?}");
        assert_eq!(report.free_bytes, free);
        assert_eq!(report.actions.len(), usize::from(cleanup), "{report:?}");
    }
}

#[test]
fn percentage_thresholds_trigger_at_the_boundary_on_large_filesystems() {
    let root = cache_fixture();
    let total = 100_000_000_000;
    assert_thresholds(
        root.path(),
        Thresholds::default().for_filesystem(total),
        &[
            (total / 4 + 1, false, false),
            (total / 4, true, false),
            (total / 5 + 1, true, false),
            (total / 5, true, true),
        ],
    );
}

#[test]
fn absolute_threshold_boundaries_and_custom_configuration() {
    let root = cache_fixture();
    let alarm_floor = 5 * 1024 * 1024 * 1024;
    assert_thresholds(
        root.path(),
        Thresholds::default(),
        &[
            (alarm_floor - 1, true, true),
            (alarm_floor, false, true),
            (9_999_999_999, false, true),
            (10_000_000_000, false, false),
        ],
    );
    let config = Thresholds {
        alarm_free_bytes: 5,
        cleanup_free_bytes: 10,
    };
    assert!(config.validate().is_ok());
    assert!(Thresholds {
        alarm_free_bytes: 0,
        ..config
    }
    .validate()
    .is_err());
    assert!(Thresholds {
        alarm_free_bytes: 11,
        ..config
    }
    .validate()
    .is_err());
    assert_thresholds(
        root.path(),
        config,
        &[
            (4, true, true),
            (5, false, true),
            (9, false, true),
            (10, false, false),
        ],
    );
}

#[test]
fn cli_rejects_invalid_byte_thresholds() {
    let tmp = tempfile::tempdir().unwrap();
    for options in [
        vec!["--alarm-free-bytes", "-1"],
        vec!["--cleanup-free-bytes", "invalid"],
        vec!["--alarm-free-bytes", "0"],
        vec!["--alarm-free-bytes", "11", "--cleanup-free-bytes", "10"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"))
            .args([
                "--repo", "/missing", "--root", "/missing", "--pins", "/missing", "--state",
            ])
            .arg(tmp.path())
            .args(options)
            .output()
            .unwrap();
        assert!(!output.status.success());
        let report: serde_json::Value =
            serde_json::from_slice(&std::fs::read(tmp.path().join("disk-hygiene.json")).unwrap())
                .unwrap();
        assert_eq!(report["alarm"], true);
        let error = report["error"].as_str().unwrap();
        assert!(
            error.contains("invalid digit found in string")
                || error.contains("require 0 < alarm-free-bytes <= cleanup-free-bytes"),
            "{error}"
        );
        assert!(report.get("disk_pct").is_none());
    }
}
