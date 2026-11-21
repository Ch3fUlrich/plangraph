use super::main_gh::*;
use super::main_state::*;
use crate::ci_watch::github::Github;
use serde_json::{json, Value};

const TIP: &str = "1111111111111111111111111111111111111111";

#[test]
fn the_matrix_file_names_every_job_of_the_workflow() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let matrix = matrix_from_toml(MATRIX).unwrap();
    let workflow = std::fs::read_to_string(root.join(".github/workflows/tests.yml")).unwrap();
    let re = regex::Regex::new(r#"(?m)^  [a-z-]+:\n    name: ['"]?([^\n'"$]*?)['"]?$"#).unwrap();
    let names: Vec<String> = re
        .captures_iter(&workflow)
        .map(|c| c[1].to_owned())
        .collect();
    assert!(names.len() >= 10, "{names:?}");
    for tier in ["self", "hosted"] {
        for name in &names {
            assert!(
                matrix.tiers[tier].contains_key(name),
                "{name} missing from tier {tier}"
            );
        }
    }
    assert_eq!(
        matrix.tiers["self"]
            .values()
            .filter(|s| *s == "sqlite")
            .count(),
        4
    );
    assert_eq!(
        matrix.tiers["hosted"]
            .values()
            .filter(|s| *s == "sqlite")
            .count(),
        3
    );
}

/// A recorded listing: one push run with a failed first attempt and a green second one.
struct Fixture {
    protection: Value,
    calls: Vec<String>,
}

impl Github for Fixture {
    fn pages(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        self.calls.push(endpoint.to_owned());
        let failed = endpoint.contains("/attempts/1/");
        let mut jobs = vec![];
        for n in 1..=3 {
            jobs.push(json!({"name": format!("Python tests, SQLite keeper (shard {n} of 3)"), "conclusion": "success"}));
        }
        jobs.push(
            json!({"name": "Rust workspace on Linux: fmt, clippy, tests",
            "conclusion": if failed { "failure" } else { "success" }}),
        );
        Ok(jobs)
    }
    fn page(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        self.calls.push(endpoint.to_owned());
        if endpoint.starts_with("actions/workflows/tests.yml/runs?head_sha") {
            return Ok(vec![
                json!({"id": 7, "head_sha": TIP, "head_branch": "main", "event": "push", "run_attempt": 2}),
                json!({"id": 8, "head_sha": TIP, "head_branch": "feature", "event": "push", "run_attempt": 1}),
            ]);
        }
        if endpoint.starts_with("actions/workflows/tests.yml/runs?branch") {
            return Ok(vec![
                json!({"run_started_at": "2026-11-21T00:00:00Z", "updated_at": "2026-11-21T00:20:00Z"}),
            ]);
        }
        Err(format!("unexpected {endpoint}"))
    }
    fn log(&mut self, _: u64) -> Result<String, String> {
        Err("no logs".into())
    }
    fn rerun(&mut self, _: u64) -> Result<(), String> {
        Err("no rerun".into())
    }
    fn value(&mut self, endpoint: &str) -> Result<Value, String> {
        self.calls.push(endpoint.to_owned());
        if endpoint == "git/ref/heads/main" {
            return Ok(json!({"object": {"sha": TIP}}));
        }
        if endpoint.ends_with("/protection") {
            return Ok(self.protection.clone());
        }
        if endpoint.contains("/attempts/1") {
            return Ok(
                json!({"status": "completed", "conclusion": "failure", "run_started_at": "2026-11-21T00:00:00Z"}),
            );
        }
        if endpoint.contains("/attempts/2") {
            return Ok(
                json!({"status": "completed", "conclusion": "success", "run_started_at": "2026-11-21T00:30:00Z"}),
            );
        }
        Err(format!("unexpected {endpoint}"))
    }
}

fn source(protection: Value) -> GhSource<Fixture> {
    GhSource {
        gh: Fixture {
            protection,
            calls: vec![],
        },
        branch: "main".into(),
        now: || 1_800_000_000,
    }
}

fn protected() -> Value {
    json!({"allow_force_pushes": {"enabled": false}, "allow_deletions": {"enabled": false}})
}

#[test]
fn the_recorded_listing_becomes_runs_with_their_attempts_and_tier() {
    let mut s = source(protected());
    let runs = s.runs(TIP).unwrap();
    // Runs of every ref on the commit are read; the evaluation ignores other refs on a full tip.
    assert_eq!(runs.len(), 2);
    assert_eq!(runs[1].refname, "feature");
    assert_eq!(runs[0].attempts.len(), 2);
    assert_eq!(runs[0].attempts[0].tier, "hosted");
    assert_eq!(
        runs[0].attempts[0].jobs[3].conclusion.as_deref(),
        Some("failure")
    );
    assert_eq!(s.green_push_durations().unwrap(), vec![1200]);
}

#[test]
fn protection_with_force_push_allowed_is_not_forward_only() {
    assert!(source(protected()).forward_only().unwrap());
    let open =
        json!({"allow_force_pushes": {"enabled": true}, "allow_deletions": {"enabled": false}});
    assert!(!source(open).forward_only().unwrap());
}

#[test]
fn the_file_ledger_records_a_tip_once_refuses_stale_and_non_descending_calls() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("l.json");
    let mut ledger = FileLedger {
        path: path.clone(),
        descends: |_: &str, _: &str| true,
    };
    assert_eq!(ledger.observe("a", None, 100).unwrap(), 100);
    assert_eq!(
        ledger.observe("a", None, 500).unwrap(),
        100,
        "first sighting stays"
    );
    assert!(
        ledger.observe("b", None, 200).is_err(),
        "caller does not know the recorded tip"
    );
    assert_eq!(ledger.observe("b", Some("a"), 200).unwrap(), 200);
    assert_eq!(ledger.last().as_deref(), Some("b"));
    let mut sideways = FileLedger {
        path,
        descends: |_: &str, _: &str| false,
    };
    assert!(sideways.observe("c", Some("b"), 300).is_err());
}

#[test]
fn the_whole_function_reads_the_recorded_listing_to_green() {
    let dir = tempfile::tempdir().unwrap();
    let matrix = matrix_from_toml(MATRIX).unwrap();
    // The recorded run lists only four of the expected jobs, so it is not complete-green: red.
    let mut ledger = FileLedger {
        path: dir.path().join("l.json"),
        descends: |_: &str, _: &str| true,
    };
    let answer = observe(&mut source(protected()), &mut ledger, &matrix);
    assert!(
        matches!(answer.state, State::Red { .. }),
        "{:?}",
        answer.state
    );
    assert_eq!(answer.tip, TIP);
}
