use super::*;
#[test]
fn health_json_and_report_flags_help() {
    for args in [vec!["--help"], vec!["report", "--help"]] {
        let mut out = Vec::new();
        execute(args.into_iter().map(String::from).collect(), &mut out).unwrap();
        assert!(String::from_utf8(out).unwrap().contains("--baseline"));
    }
    assert_eq!(timestamp("1970-01-01T01:00:00+01:00").unwrap(), 0);
    assert_eq!(
        timestamp("2026-11-02T10:30:00Z").unwrap() - timestamp("2026-10-30T10:30:00Z").unwrap(),
        259200
    );
}
#[test]
fn invalid_windows_and_duplicates() {
    for s in [
        "2026-02-29T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:00:00",
        "bad",
    ] {
        assert!(timestamp(s).is_err());
    }
    for args in [
        vec!["--json", "--json"],
        vec!["report", "--since"],
        vec!["--unknown"],
    ] {
        assert!(execute(
            args.into_iter().map(String::from).collect(),
            &mut Vec::new()
        )
        .is_err());
    }
}
#[test]
fn baseline_stored_private_not_symlink() {
    let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&dir).unwrap();
    baseline(&dir, 0, 1, &serde_json::json!({"version":1})).unwrap();
    let path = dir.join("reports/baseline-0-1.json");
    assert!(path.is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::{symlink, PermissionsExt};
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        symlink(&path, dir.join("reports/baseline-1-2.json")).unwrap();
        assert!(baseline(&dir, 1, 2, &Value::Null).is_err());
    }
    std::fs::remove_dir_all(dir).unwrap();
}
