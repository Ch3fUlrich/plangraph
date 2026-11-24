//! The shipped unit files verify with systemd-analyze and carry no secret.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn unit_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/systemd")
}

#[cfg(unix)]
#[test]
fn units_verify_with_systemd_analyze() {
    let probe = Command::new("systemd-analyze").arg("--version").output();
    if probe.is_err() {
        eprintln!("systemd-analyze not installed: skipped");
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    let bin = pg_worker_core::executable_test_support::write_script(
        tmp.path(),
        "pg-keeper-backup",
        "#!/bin/sh\n",
    );
    let service = fs::read_to_string(unit_dir().join("pg-keeper-backup.service"))
        .unwrap()
        .replace("%h/.local/bin/pg-keeper-backup", bin.to_str().unwrap())
        .replace("EnvironmentFile=", "EnvironmentFile=-");
    fs::write(tmp.path().join("pg-keeper-backup.service"), service).unwrap();
    fs::copy(
        unit_dir().join("pg-keeper-backup.timer"),
        tmp.path().join("pg-keeper-backup.timer"),
    )
    .unwrap();
    let out = Command::new("systemd-analyze")
        .args(["verify", "--user"])
        .arg(tmp.path().join("pg-keeper-backup.timer"))
        .arg(tmp.path().join("pg-keeper-backup.service"))
        .output()
        .unwrap();
    // Other units on the host may warn; only findings about ours count.
    let text: Vec<_> = String::from_utf8_lossy(&out.stderr)
        .lines()
        .filter(|l| l.contains("pg-keeper-backup"))
        .map(str::to_owned)
        .collect();
    assert!(text.is_empty(), "{text:?}");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
fn units_and_env_example_carry_no_secret_value() {
    for name in [
        "pg-keeper-backup.service",
        "pg-keeper-backup.timer",
        "keeper-backup.env.example",
    ] {
        let text = fs::read_to_string(unit_dir().join(name)).unwrap();
        for line in text.lines().filter(|l| !l.trim_start().starts_with('#')) {
            if let Some((key, value)) = line.split_once('=') {
                assert!(
                    !(key.starts_with("PG") && !value.is_empty()),
                    "{name}: {line}"
                );
            }
            assert!(
                name.ends_with(".example") || !line.contains("PGPASSWORD"),
                "{name}: {line}"
            );
        }
    }
    let service = fs::read_to_string(unit_dir().join("pg-keeper-backup.service")).unwrap();
    assert!(service.contains("UMask=0077") && service.contains("--verify"));
}
