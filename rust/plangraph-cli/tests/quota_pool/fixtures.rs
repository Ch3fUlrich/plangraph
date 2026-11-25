//! Fixtures of the `plangraph quota pool` integration tests: a snapshot store and the CLI door.
#![allow(dead_code)]
#[path = "../../../test_support/python.rs"]
mod test_python;

use serde_json::Value;
use std::path::Path;
use std::process::Command;

pub const WEEK: f64 = 604_800.0;

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as f64
}

/// Rows are (id, provider, connection_id, window, remaining, observed_at, reset_at).
pub type Row = (
    i64,
    &'static str,
    Option<&'static str>,
    &'static str,
    Option<f64>,
    f64,
    f64,
);

pub fn database(directory: &Path, rows: &[Row]) -> std::path::PathBuf {
    let sql = |value: Option<&str>| value.map_or("NULL".into(), |v| format!("'{v}'"));
    let inserts: String = rows
        .iter()
        .map(|(id, provider, connection, window, left, at, reset)| {
            let left = left.map_or("NULL".into(), |v| v.to_string());
            insert(
                *id,
                provider,
                &sql(*connection),
                window,
                &left,
                &at.to_string(),
                *reset,
            )
        })
        .collect();
    database_sql(directory, &inserts)
}

/// One INSERT whose connection, remaining and observation are raw SQL literals.
pub fn insert(
    id: i64,
    provider: &str,
    connection: &str,
    window: &str,
    left: &str,
    at: &str,
    reset: f64,
) -> String {
    format!("INSERT INTO quota_snapshots VALUES ({id}, '{provider}', {connection}, '{window}', {left}, 0, {reset}, {at});")
}

pub fn database_sql(directory: &Path, inserts: &str) -> std::path::PathBuf {
    let db = directory.join("quota.sqlite");
    let script = format!(
        "CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id,
          window_key, remaining_percentage, is_exhausted, next_reset_at, created_at);{inserts}"
    );
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

/// A zone-less ISO text of whole `secs` plus `nanos`, `sep` between date and time and `digits`
/// fractional digits; integers keep differences f64 seconds cannot hold.
pub fn iso(secs: f64, nanos: u32, sep: char, digits: usize) -> String {
    let t = chrono::DateTime::from_timestamp(secs as i64, nanos).unwrap();
    let format = format!("%Y-%m-%d{sep}%H:%M:%S%.{digits}f");
    format!("'{}'", t.naive_utc().format(&format))
}

pub fn pool(db: Option<&Path>) -> Value {
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

pub fn window<'a>(report: &'a Value, key: &str) -> &'a Value {
    report["windows"]
        .as_array()
        .unwrap()
        .iter()
        .find(|w| w["window"] == key)
        .unwrap_or_else(|| panic!("window {key} missing in {report}"))
}

pub fn account<'a>(window: &'a Value, name: &str) -> &'a Value {
    window["accounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["account"] == name)
        .unwrap_or_else(|| panic!("account {name} missing in {window}"))
}

pub fn near(value: &Value, expected: f64) {
    let got = value
        .as_f64()
        .unwrap_or_else(|| panic!("{value} is not a number"));
    assert!((got - expected).abs() < 1e-3, "{got} != {expected}");
}
