//! The `pg-telemetry` command line: usage, refusals and the checks that run before any connection.
use std::process::{Command, Output};

/// An unreachable reader DSN: a run that reaches the connection fails with the fixed connect message.
const UNREACHABLE: &str = "host=127.0.0.1 port=1 user=reader connect_timeout=2";

fn telemetry(args: &[&str], env: &[(&str, &str)]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_pg-telemetry"))
        .env_clear()
        .envs(env.iter().copied())
        .args(args)
        .output()
        .unwrap()
}

fn failed_with(output: Output, message: &str) {
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        format!("baseline: {message}\n")
    );
}

fn dsn_file(dir: &tempfile::TempDir) -> String {
    let path = dir.path().join("reader.dsn");
    std::fs::write(&path, format!("{UNREACHABLE}\n")).unwrap();
    path.to_str().unwrap().to_string()
}

#[test]
fn anything_but_keeper_only_with_an_optional_since_is_a_usage_error() {
    let usage = "usage: plangraph baseline --keeper-only [--since RFC3339]";
    for args in [
        vec![],
        vec!["--since", "2024-01-01T00:00:00Z"],
        vec!["--keeper-only", "--since"],
        vec!["--keeper-only", "--until", "2024-01-01T00:00:00Z"],
        vec!["--keeper-only", "--since", "2024-01-01T00:00:00Z", "extra"],
        vec!["--keeper-only", "--keeper-only"],
    ] {
        failed_with(telemetry(&args, &[]), usage);
    }
}

#[test]
fn a_sqlite_keeper_is_refused_before_anything_is_read() {
    for backend in ["sqlite", "SQLite"] {
        failed_with(
            telemetry(&["--keeper-only"], &[("PG_KEEPER_BACKEND", backend)]),
            "keeper baseline requires PostgreSQL",
        );
    }
}

#[test]
fn the_reader_dsn_file_is_required_and_must_be_readable() {
    failed_with(
        telemetry(&["--keeper-only"], &[]),
        "PG_TELEMETRY_DSN_FILE is required",
    );
    failed_with(
        telemetry(
            &["--keeper-only"],
            &[("PG_TELEMETRY_DSN_FILE", "/nonexistent/reader.dsn")],
        ),
        "cannot read the telemetry reader DSN file",
    );
}

#[test]
fn a_bad_schema_or_window_is_refused_before_connecting() {
    let dir = tempfile::tempdir().unwrap();
    let file = dsn_file(&dir);
    failed_with(
        telemetry(
            &["--keeper-only"],
            &[
                ("PG_TELEMETRY_DSN_FILE", &file),
                ("PG_SCHEMA", "Keeper;drop"),
            ],
        ),
        "invalid keeper schema name",
    );
    failed_with(
        telemetry(
            &["--keeper-only", "--since", "yesterday"],
            &[("PG_TELEMETRY_DSN_FILE", &file)],
        ),
        "invalid --since timestamp",
    );
}

#[test]
fn a_valid_request_reaches_the_connection_with_or_without_since() {
    let dir = tempfile::tempdir().unwrap();
    let file = dsn_file(&dir);
    for args in [
        vec!["--keeper-only"],
        vec!["--keeper-only", "--since", "2024-01-01T00:00:00Z"],
    ] {
        failed_with(
            telemetry(
                &args,
                &[("PG_TELEMETRY_DSN_FILE", &file), ("PG_SCHEMA", "keeper_x")],
            ),
            "cannot connect to keeper PostgreSQL",
        );
    }
}

#[test]
fn version_prints_the_name_and_succeeds_without_any_environment() {
    let output = telemetry(&["--version"], &[]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("pg-telemetry {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert!(output.stderr.is_empty());
}
