#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::Path;
use std::process::{Command, Output};

fn quota_status(db: &Path) -> Output {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(repo)
        .env("PLANGRAPH_ENV_FILE", db.with_extension("env"))
        .args(["quota", "--db"])
        .arg(db)
        .output()
        .unwrap()
}

#[test]
fn quota_status_reports_latest_usage_per_provider_account_and_window() {
    let directory = tempfile::tempdir().unwrap();
    let db = directory.path().join("quota #1.sqlite");
    let fixture = test_python::command()
        .args([
            "-c",
            r#"
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.executescript('''
        CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id,
          window_key, remaining_percentage, is_exhausted, next_reset_at, created_at);
        INSERT INTO quota_snapshots VALUES
          (1, 'codex', 'a', 'weekly', 90, 0, NULL, '2026-11-21T10:00:00Z'),
          (2, 'codex', 'a', 'weekly', 26.5, 0, NULL, '2026-11-21T11:00:00Z'),
          (3, 'agy', 'b', 'daily', 50, 1, NULL, '2026-11-21T11:00:00Z'),
          (4, 'codex', 'a', 'daily', NULL, 0, NULL, '2026-11-21T11:00:00Z'),
          (5, 'codex', 'b', 'daily', 99.9, 0, NULL, '2026-11-21T11:00:00Z');
    ''')
"#,
        ])
        .arg(&db)
        .status()
        .unwrap();
    assert!(fixture.success());
    let result = quota_status(&db);
    assert!(result.status.success(), "{:?}", result);
    let text = String::from_utf8(result.stdout).unwrap();
    assert!(text.contains("agy\tb\tdaily\t100% / 100%"), "{text}");
    assert!(text.contains("codex\ta\tweekly\t73.5% / 100%"), "{text}");
    assert!(text.contains("codex\ta\tdaily\tunknown"), "{text}");
    assert!(text.contains("codex\tb\tdaily\t0.1% / 100%"), "{text}");
    assert!(!text.contains("10% / 100%"), "{text}");
}

#[test]
fn quota_status_leaves_an_unavailable_store_unknown_and_does_not_create_it() {
    let directory = tempfile::tempdir().unwrap();
    let db = directory.path().join("absent.sqlite");
    let result = quota_status(&db);
    assert!(result.status.success(), "{:?}", result);
    assert_eq!(String::from_utf8(result.stdout).unwrap(), "quota unknown\n");
    assert!(!db.exists());
}
