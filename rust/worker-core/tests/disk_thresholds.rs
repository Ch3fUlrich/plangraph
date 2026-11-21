#![cfg(target_os = "linux")]
use pg_worker_core::disk_hygiene::{sweep, Guards, Thresholds};
use std::{process::Command, time::SystemTime};

#[test]
fn percentage_thresholds_trigger_at_the_boundary_on_large_filesystems() {
    let repo = tempfile::tempdir().unwrap();
    assert!(pg_worker_core::git_test_support::command(repo.path())
        .args(["init", "-q"])
        .status()
        .unwrap()
        .success());
    let total = 100_000_000_000;
    let limits = Thresholds::default().for_filesystem(total);
    let check = |free| {
        sweep(
            repo.path(),
            repo.path(),
            &Guards::default(),
            SystemTime::now(),
            free,
            limits,
            false,
        )
        .unwrap()
    };
    assert!(!check(total / 4 + 1).alarm);
    assert!(check(total / 4).alarm);
    let missing_repo = repo.path().join("missing");
    let cleanup = |free| {
        sweep(
            &missing_repo,
            repo.path(),
            &Guards::default(),
            SystemTime::now(),
            free,
            limits,
            false,
        )
    };
    assert!(cleanup(total / 5 + 1).is_ok());
    assert!(cleanup(total / 5).is_err());
}

#[test]
fn absolute_threshold_boundaries_and_custom_configuration() {
    let root = tempfile::tempdir().unwrap();
    // No Git repo: reaching cleanup produces a Git error, making the boundary observable.
    for (free, alarm, cleanup) in [
        (4_999_999_999, true, true),
        (5_000_000_000, false, true),
        (9_999_999_999, false, true),
        (10_000_000_000, false, false),
    ] {
        let result = sweep(
            root.path(),
            root.path(),
            &Guards::default(),
            SystemTime::now(),
            free,
            Thresholds::default(),
            false,
        );
        if cleanup {
            assert!(result.is_err());
        } else {
            let report = result.unwrap();
            assert_eq!(report.alarm, alarm);
            assert_eq!(report.free_bytes, free);
        }
    }
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
    // A valid empty repo makes alarms observable below the cleanup threshold.
    assert!(pg_worker_core::git_test_support::command(root.path())
        .args(["init", "-q"])
        .arg(root.path())
        .status()
        .unwrap()
        .success());
    for (free, alarm) in [(4, true), (5, false), (9, false), (10, false)] {
        let report = sweep(
            root.path(),
            root.path(),
            &Guards::default(),
            SystemTime::now(),
            free,
            config,
            false,
        )
        .unwrap();
        assert_eq!(report.alarm, alarm);
    }
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
