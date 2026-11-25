#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use serde_json::Value;
use std::io::{Read, Write};
use std::path::Path;
use std::process::Command;

const WEEK: f64 = 604_800.0;

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as f64
}

/// Rows are (id, provider, connection_id, window, remaining, observed_at, reset_at).
type Row = (
    i64,
    &'static str,
    Option<&'static str>,
    &'static str,
    Option<f64>,
    f64,
    f64,
);

fn database(directory: &Path, rows: &[Row]) -> std::path::PathBuf {
    let db = directory.join("quota.sqlite");
    let mut script = String::from(
        "CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id,
          window_key, remaining_percentage, is_exhausted, next_reset_at, created_at);",
    );
    let sql = |value: Option<&str>| value.map_or("NULL".into(), |v| format!("'{v}'"));
    for (id, provider, connection, window, left, at, reset) in rows {
        let left = left.map_or("NULL".into(), |v| v.to_string());
        script.push_str(&format!(
            "INSERT INTO quota_snapshots VALUES ({id}, '{provider}', {}, '{window}', {left}, 0, {reset}, {at});",
            sql(*connection)
        ));
    }
    let fixture = test_python::command()
        .args([
            "-c",
            "import sqlite3, sys\nwith sqlite3.connect(sys.argv[1]) as db: db.executescript(sys.argv[2])",
        ])
        .arg(&db)
        .arg(script)
        .status()
        .unwrap();
    assert!(fixture.success());
    db
}

fn pool(db: Option<&Path>) -> Value {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_plangraph"));
    cmd.current_dir(repo)
        .env_remove("QUOTA_SNAPSHOT_DB")
        .env("PLANGRAPH_ENV_FILE", "/nonexistent/plangraph.env")
        .args(["quota", "pool"]);
    if let Some(db) = db {
        cmd.arg("--db").arg(db);
    }
    let result = cmd.output().unwrap();
    assert!(result.status.success(), "{result:?}");
    serde_json::from_slice(&result.stdout).unwrap()
}

fn window<'a>(report: &'a Value, key: &str) -> &'a Value {
    report["windows"]
        .as_array()
        .unwrap()
        .iter()
        .find(|w| w["window"] == key)
        .unwrap_or_else(|| panic!("window {key} missing in {report}"))
}

fn account<'a>(window: &'a Value, name: &str) -> &'a Value {
    window["accounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["account"] == name)
        .unwrap_or_else(|| panic!("account {name} missing in {window}"))
}

fn near(value: &Value, expected: f64) {
    let got = value
        .as_f64()
        .unwrap_or_else(|| panic!("{value} is not a number"));
    assert!((got - expected).abs() < 1e-3, "{got} != {expected}");
}

#[test]
fn quota_pool_reports_latest_active_windows_from_read_only_db() {
    let t = now() - 100.0;
    let reset = now() + 3.0 * 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let db = database(
        directory.path(),
        &[
            // Account a: the winner has the lowest id and the newest fractional time; a later
            // id of its own raw key and a later id of the other identity form are older.
            (1, "codex:a", None, "weekly", Some(40.0), t + 0.9, reset),
            (2, "codex:a", None, "weekly", Some(5.0), t - 60.0, reset),
            (3, "codex", Some("a"), "weekly", Some(90.0), t + 0.1, reset),
            (4, "codex", Some("b"), "weekly", Some(80.0), t, reset + 60.0),
            (5, "codex", Some("b"), "5h", Some(100.0), t, now() + 3_600.0),
            (6, "claude", Some("x"), "weekly", Some(1.0), t, reset),
        ],
    );
    let report = pool(Some(&db));
    assert_eq!(report["status"], "ok", "{report}");
    let weekly = window(&report, "weekly");
    let a = account(weekly, "a");
    near(&a["used_pct"], 60.0);
    near(&a["observed_at"], t + 0.9);
    near(&weekly["used_pct"], 40.0); // mean of a (60) and b (20)
    near(&weekly["observed_at"], t + 0.9);
    assert_eq!(weekly["reset_at"], reset as i64);
    assert_eq!(weekly["coverage"]["observed_accounts"], 2);
    assert_eq!(weekly["coverage"]["usable_accounts"], 2);
    assert_eq!(weekly["coverage"]["full_pool"], "unknown");
    near(&window(&report, "5h")["used_pct"], 0.0);
    assert_eq!(report["windows"].as_array().unwrap().len(), 2, "{report}");
}

#[test]
fn quota_pool_does_not_resurrect_invalid_newest_observation() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let db = database(
        directory.path(),
        &[
            // Both identity forms: the older valid row must not stand in for the invalid newest.
            (1, "codex:a", None, "weekly", Some(50.0), t - 60.0, reset),
            (2, "codex:a", None, "weekly", None, t, reset),
            (3, "codex", Some("b"), "weekly", Some(50.0), t - 60.0, reset),
            (4, "codex", Some("b"), "weekly", None, t, reset),
            (5, "codex:c", None, "weekly", Some(50.0), t - 60.0, reset),
            (6, "codex", Some("c"), "weekly", None, t, reset),
        ],
    );
    let report = pool(Some(&db));
    let weekly = window(&report, "weekly");
    assert!(weekly["used_pct"].is_null(), "{weekly}");
    assert!(weekly["pace_pct"].is_null(), "{weekly}");
    assert_eq!(weekly["accounts"].as_array().unwrap().len(), 0, "{weekly}");
    assert_eq!(weekly["coverage"]["usable_accounts"], 0);
    assert_eq!(weekly["coverage"]["observed_accounts"], 3);
    assert_eq!(report["status"], "unknown");
}

#[test]
fn quota_pool_keeps_staggered_account_pace_aligned() {
    let t = now();
    // Account a is a day into its week at its own observation; b is five days in at its own.
    let (at_a, reset_a) = (t - 600.0, t + 6.0 * 86_400.0 - 600.0);
    let (at_b, reset_b) = (t - 60.0, t + 2.0 * 86_400.0);
    let directory = tempfile::tempdir().unwrap();
    let db = database(
        directory.path(),
        &[
            (1, "codex", Some("a"), "weekly", Some(90.0), at_a, reset_a),
            (2, "codex", Some("b"), "weekly", Some(30.0), at_b, reset_b),
            // Stale: outside the cohort for usage and pace alike.
            (
                3,
                "codex",
                Some("c"),
                "weekly",
                Some(0.0),
                t - 86_400.0,
                reset_b,
            ),
        ],
    );
    let weekly = pool(Some(&db))["windows"][0].clone();
    let elapsed = |at: f64, reset: f64| (at - (reset - WEEK)) / WEEK * 100.0;
    near(
        &account(&weekly, "a")["elapsed_pct"],
        elapsed(at_a, reset_a),
    );
    near(
        &account(&weekly, "b")["elapsed_pct"],
        elapsed(at_b, reset_b),
    );
    near(
        &weekly["pace_pct"],
        (elapsed(at_a, reset_a) + elapsed(at_b, reset_b)) / 2.0,
    );
    near(&weekly["used_pct"], 40.0); // the same two accounts: (10 + 70) / 2
    assert_eq!(weekly["coverage"]["observed_accounts"], 3);
    assert_eq!(weekly["coverage"]["usable_accounts"], 2);
    assert_eq!(weekly["reset_at"], reset_b as i64);
}

#[test]
fn quota_pool_without_a_database_is_an_explicit_unknown() {
    // The management route lists accounts without window identity or observation time.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = [0u8; 4096];
        let _ = stream.read(&mut request).unwrap();
        let body = r#"{"data": [{"provider": "codex:a1", "remainingPct": 0}]}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
    });
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(repo)
        .env_remove("QUOTA_SNAPSHOT_DB")
        .env("PLANGRAPH_ENV_FILE", "/nonexistent/plangraph.env")
        .env("OMNIROUTE_BASE_URL", format!("http://127.0.0.1:{port}/v1"))
        .env("PLANGRAPH_GATEWAY_LOG_TOKEN", "fixture-not-a-secret")
        .args(["quota", "pool"])
        .output()
        .unwrap();
    server.join().unwrap();
    assert!(result.status.success(), "{result:?}");
    let report: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(report["status"], "unknown", "{report}");
    assert_eq!(report["reason"], "no identifiable windows");
    assert_eq!(report["windows"].as_array().unwrap().len(), 0);
}

#[test]
fn quota_pool_with_an_unreadable_store_is_an_explicit_unknown() {
    let directory = tempfile::tempdir().unwrap();
    let db = directory.path().join("absent.sqlite");
    let report = pool(Some(&db));
    assert_eq!(report["status"], "unknown");
    assert_eq!(report["reason"], "snapshots unreadable");
    assert!(!db.exists());
}
