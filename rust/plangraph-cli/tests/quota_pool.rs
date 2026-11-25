#[path = "quota_pool/fixtures.rs"]
mod fixtures;

use fixtures::*;
use serde_json::Value;
use std::io::{Read, Write};
use std::path::Path;
use std::process::Command;

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

#[test]
fn quota_pool_orders_zone_less_t_timestamps_and_sub_microsecond_ties() {
    let t = now() - 100.0;
    let reset = now() + 3.0 * 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let inserts = [
        // Account a: T-separated, no zone; the winner differs from the older row by 100 ns.
        insert(
            1,
            "codex:a",
            "NULL",
            "weekly",
            "40",
            &iso(t, 123_456_800, 'T', 9),
            reset,
        ),
        insert(
            2,
            "codex:a",
            "NULL",
            "weekly",
            "90",
            &iso(t, 123_456_700, 'T', 9),
            reset,
        ),
        // Account b: the only row is T-separated and must still be usable.
        insert(
            3,
            "codex:b",
            "NULL",
            "weekly",
            "80",
            &iso(t, 0, 'T', 3),
            reset,
        ),
    ]
    .concat();
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    near(&account(&weekly, "a")["used_pct"], 60.0);
    near(&account(&weekly, "b")["used_pct"], 20.0);
    assert_eq!(weekly["coverage"]["usable_accounts"], 2, "{weekly}");
    // The report keeps the selected observation to the nanosecond; a float time could not.
    let at_ns = |name: &str| account(&weekly, name)["observed_at_ns"].as_i64().unwrap();
    assert_eq!(at_ns("a"), t as i64 * 1_000_000_000 + 123_456_800);
    assert_eq!(at_ns("b"), t as i64 * 1_000_000_000);
    assert_eq!(weekly["observed_at_ns"], at_ns("a"));
}

#[test]
fn quota_pool_isolates_malformed_sqlite_values_per_row() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let inserts = [
        insert(1, "codex:a", "NULL", "weekly", "50", &t.to_string(), reset),
        // An infinite percentage and a BLOB on other providers' rows must not hide account a.
        insert(2, "claude", "'x'", "weekly", "9e999", &t.to_string(), reset),
        insert(
            3,
            "claude",
            "'y'",
            "weekly",
            "x'00ff'",
            &t.to_string(),
            reset,
        ),
        insert(
            4,
            "codex:b",
            "NULL",
            "weekly",
            "x'00ff'",
            &t.to_string(),
            reset,
        ),
    ]
    .concat();
    let report = pool(Some(&database_sql(directory.path(), &inserts)));
    let weekly = window(&report, "weekly");
    near(&weekly["used_pct"], 50.0);
    assert_eq!(weekly["coverage"]["observed_accounts"], 2, "{weekly}");
    assert_eq!(weekly["coverage"]["usable_accounts"], 1, "{weekly}");
}

#[test]
fn quota_pool_empty_connection_id_keeps_the_provider_suffix_identity() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let inserts = insert(1, "codex:a", "''", "weekly", "70", &t.to_string(), reset);
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    near(&account(&weekly, "a")["used_pct"], 30.0);
    assert_eq!(weekly["coverage"]["unidentified"], 0, "{weekly}");
}

#[test]
fn quota_pool_leaves_out_an_observation_made_before_its_window_began() {
    let t = now();
    let directory = tempfile::tempdir().unwrap();
    // Account a is fresh but was observed an hour before the week its reset implies began;
    // b is inside its week.
    let inserts = [
        insert(
            1,
            "codex",
            "'a'",
            "weekly",
            "10",
            &(t - 100.0).to_string(),
            t - 100.0 + WEEK + 3_600.0,
        ),
        insert(
            2,
            "codex",
            "'b'",
            "weekly",
            "60",
            &(t - 60.0).to_string(),
            t + 86_400.0,
        ),
    ]
    .concat();
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    near(&weekly["used_pct"], 40.0);
    assert_eq!(weekly["coverage"]["observed_accounts"], 2, "{weekly}");
    assert_eq!(weekly["coverage"]["usable_accounts"], 1, "{weekly}");
    assert_eq!(weekly["accounts"].as_array().unwrap().len(), 1);
}

#[test]
fn quota_pool_rejects_out_of_range_timestamps_without_hiding_healthy_accounts() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let directory = tempfile::tempdir().unwrap();
    let inserts = [
        insert(1, "codex:a", "NULL", "weekly", "50", &t.to_string(), reset),
        // Finite but absurd observations and resets are unusable, never arithmetic input.
        insert(2, "codex:b", "NULL", "weekly", "50", "-1e100", reset),
        insert(3, "codex:c", "NULL", "weekly", "50", "1e300", reset),
        insert(4, "codex:d", "NULL", "weekly", "50", &t.to_string(), -1e100),
        insert(5, "codex:e", "NULL", "weekly", "50", &t.to_string(), 1e300),
    ]
    .concat();
    let report = pool(Some(&database_sql(directory.path(), &inserts)));
    let weekly = window(&report, "weekly");
    near(&weekly["used_pct"], 50.0);
    assert_eq!(weekly["coverage"]["observed_accounts"], 5, "{weekly}");
    assert_eq!(weekly["coverage"]["usable_accounts"], 1, "{weekly}");
    assert_eq!(weekly["accounts"][0]["account"], "a");
}

#[test]
fn quota_pool_ranks_numeric_and_iso_timestamps_of_one_instant_alike() {
    let t = now() - 100.0;
    let reset = now() + 86_400.0;
    let (numeric, text) = (format!("{}", t + 0.25), iso(t, 250_000_000, 'T', 9));
    let directory = tempfile::tempdir().unwrap();
    // One instant written both ways: the later id wins whichever form it is in.
    let inserts = [
        insert(1, "codex:a", "NULL", "weekly", "90", &text, reset),
        insert(2, "codex:a", "NULL", "weekly", "40", &numeric, reset),
        insert(3, "codex:b", "NULL", "weekly", "40", &numeric, reset),
        insert(4, "codex:b", "NULL", "weekly", "90", &text, reset),
    ]
    .concat();
    let weekly = pool(Some(&database_sql(directory.path(), &inserts)))["windows"][0].clone();
    near(&account(&weekly, "a")["used_pct"], 60.0);
    near(&account(&weekly, "b")["used_pct"], 10.0);
    assert_eq!(
        account(&weekly, "a")["observed_at_ns"],
        account(&weekly, "b")["observed_at_ns"]
    );
}
