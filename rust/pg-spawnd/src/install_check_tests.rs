use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{ffi::OsString, fs};

#[test]
fn install_check_reports_matching_stale_and_unreadable_builds() {
    let temp = tempfile::tempdir().unwrap();
    let installed = temp.path().join("pg-spawnd");
    let stamp = temp.path().join("spawnd-built-tree");
    let main = "a".repeat(40);
    let old = "b".repeat(40);
    let bytes = b"reference build";
    let sum = format!("{:x}", Sha256::digest(bytes));
    let check = || {
        let args = [
            "check-install",
            "--host-id",
            "fixture",
            "--binary",
            installed.to_str().unwrap(),
            "--source-stamp",
            stamp.to_str().unwrap(),
            "--main-sha",
            &main,
            "--main-sha256",
            &sum,
        ]
        .into_iter()
        .map(OsString::from)
        .collect();
        let mut out = Vec::new();
        super::execute(args, &mut out, |_| panic!("must not probe capacity")).unwrap();
        let row: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(row["host"], "fixture");
        assert_eq!(row["event"], "pg_spawnd_install");
        row
    };

    fs::write(&installed, bytes).unwrap();
    fs::write(&stamp, format!("{main}\n")).unwrap();
    assert_eq!(check()["status"], "current");
    assert_eq!(fs::read(&installed).unwrap(), bytes);
    assert_eq!(fs::read_to_string(&stamp).unwrap(), format!("{main}\n"));

    fs::write(&stamp, &old).unwrap();
    let stale = check();
    assert_eq!(stale["status"], "stale");
    assert_eq!(stale["reason"], "source_sha_mismatch");

    fs::write(&stamp, &main).unwrap();
    fs::write(&installed, b"different build").unwrap();
    let stale = check();
    assert_eq!(stale["status"], "stale");
    assert_eq!(stale["reason"], "binary_sha256_mismatch");

    fs::remove_file(&installed).unwrap();
    let unknown = check();
    assert_eq!(unknown["status"], "unknown");
    assert_eq!(unknown["reason"], "binary_unreadable");
    assert!(!installed.exists());
}
