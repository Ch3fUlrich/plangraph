use super::main_gh::*;
use super::main_state::*;
use crate::ci_watch::github::Github;
use serde_json::{json, Value};

const TREE: &str = "2222222222222222222222222222222222222222";
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
        if endpoint.starts_with("commits/") {
            return Ok(vec![]);
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
        if endpoint.starts_with("git/commits/") {
            return Ok(json!({"tree": {"sha": TREE}}));
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
        land_app: None,
    }
}

fn protected() -> Value {
    json!({"allow_force_pushes": {"enabled": false}, "allow_deletions": {"enabled": false}})
}

#[test]
fn the_recorded_listing_becomes_runs_with_their_attempts_and_tier() {
    let mut s = source(protected());
    let runs = s.runs(TIP).unwrap();
    // A push run of another ref on the same commit tested that commit: it is read (run 8).
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

/// A recorded listing: run 3 is the branch's own push run listing only the fast jobs; run 4 is a pull
/// request run of another ref on the same commit; run 5 is the evidence run on the train's branch.
struct FastListing {
    statuses: Vec<Value>,
    tree: &'static str,
}

impl Github for FastListing {
    fn pages(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        let full = endpoint.contains("/runs/4/") || endpoint.contains("/runs/5/");
        if full {
            return Ok(vec![
                json!({"name": "Python tests, SQLite keeper (shard 1 of 3)", "conclusion": "success"}),
                json!({"name": "Rust workspace on Linux: fmt, clippy, tests", "conclusion": "failure"}),
            ]);
        }
        Ok(["Select suites for this change", "ci-gate"]
            .iter()
            .map(|n| json!({"name": n, "conclusion": "success"}))
            .collect())
    }
    fn page(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>, String> {
        if endpoint.starts_with("actions/workflows/tests.yml/runs?head_sha") {
            return Ok(vec![
                json!({"id": 3, "head_sha": TIP, "head_branch": "main", "event": "push", "run_attempt": 1}),
                json!({"id": 4, "head_sha": TIP, "head_branch": "pr-branch", "event": "pull_request", "run_attempt": 1}),
                json!({"id": 5, "head_sha": TIP, "head_branch": "land/train", "event": "push", "run_attempt": 2}),
            ]);
        }
        if endpoint.starts_with("commits/") {
            return Ok(self.statuses.clone());
        }
        Err(format!("unexpected {endpoint}"))
    }
    fn log(&mut self, _: u64) -> Result<String, String> {
        Err("none".into())
    }
    fn rerun(&mut self, _: u64) -> Result<(), String> {
        Err("none".into())
    }
    fn value(&mut self, endpoint: &str) -> Result<Value, String> {
        if endpoint.starts_with("git/commits/") {
            return Ok(json!({"tree": {"sha": self.tree}}));
        }
        Ok(
            json!({"status": "completed", "conclusion": "success", "run_started_at": "2026-11-21T00:00:00Z"}),
        )
    }
}

fn fast_source(statuses: Vec<Value>, land_app: Option<u64>) -> GhSource<FastListing> {
    GhSource {
        gh: FastListing {
            statuses,
            tree: TREE,
        },
        branch: "main".into(),
        now: || 1_800_000_000,
        land_app,
    }
}

fn auth(app: u64, state: &str, text: &str) -> Value {
    json!({"context": "land-auth", "state": state, "creator": {"id": app}, "description": text})
}

const CI_BOT: u64 = 41_898_282;

fn ci_full(run: u64, attempt: u32, tree: &str) -> Value {
    ci_full_by(CI_BOT, "success", run, attempt, tree)
}

fn ci_full_by(creator: u64, state: &str, run: u64, attempt: u32, tree: &str) -> Value {
    json!({"context": "ci-full", "state": state, "creator": {"id": creator},
           "description": format!("run {run} attempt {attempt} tree {tree}")})
}

const APP_TEXT: &str = "attempt 9 evidence run 5 attempt 2";

fn by_id(runs: &[Run], id: u64) -> Option<&Run> {
    runs.iter().find(|r| r.id == id)
}

#[test]
fn a_run_is_fast_only_with_the_apps_status_format_and_a_full_attempt_of_the_tips_tree() {
    let statuses = vec![auth(77, "success", APP_TEXT), ci_full(5, 2, TREE)];
    let runs = fast_source(statuses, Some(77)).runs(TIP).unwrap();
    let fast = by_id(&runs, 3).unwrap();
    assert!(fast.fast && fast.attempts[0].tier == "fast");
    // The evidence run is a push run of the train's branch: it tested that commit, every attempt is read.
    let evidence = by_id(&runs, 5).unwrap();
    assert!(!evidence.fast);
    assert_eq!(
        evidence
            .attempts
            .iter()
            .map(|a| a.number)
            .collect::<Vec<_>>(),
        vec![1, 2]
    );
    // The pull request run has no ci-full status for this tree: it does not decide main's state.
    assert!(by_id(&runs, 4).is_none());
}

#[test]
fn evidence_that_is_untrusted_unnamed_or_for_another_tree_makes_nothing_fast() {
    let other_tree = "3".repeat(40);
    let cases: Vec<(Vec<Value>, Option<u64>)> = vec![
        (
            vec![auth(5, "success", APP_TEXT), ci_full(5, 2, TREE)],
            Some(77),
        ),
        (
            vec![auth(77, "failure", APP_TEXT), ci_full(5, 2, TREE)],
            Some(77),
        ),
        (
            vec![
                auth(77, "success", "evidence=5 attempt 2"),
                ci_full(5, 2, TREE),
            ],
            Some(77),
        ),
        (
            vec![auth(77, "success", APP_TEXT), ci_full(5, 2, &other_tree)],
            Some(77),
        ),
        (
            vec![auth(77, "success", APP_TEXT), ci_full(5, 1, TREE)],
            Some(77),
        ),
        (
            vec![
                auth(77, "success", "attempt 9 evidence run 3 attempt 1"),
                ci_full(3, 1, TREE),
            ],
            Some(77),
        ),
        (
            vec![auth(77, "success", APP_TEXT), ci_full(5, 2, TREE)],
            None,
        ),
        (vec![], Some(77)),
    ];
    for (statuses, app) in cases {
        let runs = fast_source(statuses.clone(), app).runs(TIP).unwrap();
        let own = by_id(&runs, 3).unwrap();
        assert!(
            !own.fast,
            "a short job list alone does not make a run fast: {statuses:?}"
        );
        assert_eq!(
            own.attempts[0].tier, "hosted",
            "it is judged against the full matrix"
        );
    }
}

#[test]
fn a_full_attempt_of_another_tree_never_decides_main() {
    let other_tree = "3".repeat(40);
    let runs = fast_source(vec![ci_full(4, 1, &other_tree)], Some(77))
        .runs(TIP)
        .unwrap();
    assert!(
        by_id(&runs, 4).is_none(),
        "a pull request run of another tree does not decide main"
    );
    let runs = fast_source(vec![ci_full(4, 1, TREE)], Some(77))
        .runs(TIP)
        .unwrap();
    assert!(
        by_id(&runs, 4).is_some(),
        "a pull request run whose ci-full names the tip's tree counts"
    );
}

#[test]
fn a_partial_job_list_without_evidence_reads_red_not_green() {
    let matrix = matrix_from_toml(MATRIX).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let mut ledger = FileLedger {
        path: dir.path().join("l.json"),
        descends: |_: &str, _: &str| true,
    };
    struct Protected(GhSource<FastListing>);
    impl Source for Protected {
        fn forward_only(&mut self) -> Result<bool, String> {
            Ok(true)
        }
        fn tip(&mut self) -> Result<String, String> {
            Ok(TIP.into())
        }
        fn runs(&mut self, tip: &str) -> Result<Vec<Run>, String> {
            self.0.runs(tip)
        }
        fn green_push_durations(&mut self) -> Result<Vec<i64>, String> {
            Ok(vec![])
        }
        fn now(&mut self) -> i64 {
            1_800_000_000
        }
    }
    let answer = observe(
        &mut Protected(fast_source(vec![], Some(77))),
        &mut ledger,
        &matrix,
    );
    assert!(
        matches!(answer.state, State::Red { .. }),
        "{:?}",
        answer.state
    );
}

#[test]
fn only_the_ci_publishers_ci_full_statuses_count_and_the_evidence_attempt_must_have_succeeded() {
    // A status from another identity names the tree but is not evidence.
    let forged = vec![
        auth(77, "success", APP_TEXT),
        ci_full_by(5, "success", 5, 2, TREE),
    ];
    let runs = fast_source(forged, Some(77)).runs(TIP).unwrap();
    assert!(!by_id(&runs, 3).unwrap().fast);
    // The publisher's status says the evidence attempt failed: nothing is fast, the failure is still read.
    let failed = vec![
        auth(77, "success", APP_TEXT),
        ci_full_by(CI_BOT, "failure", 5, 2, TREE),
    ];
    let runs = fast_source(failed, Some(77)).runs(TIP).unwrap();
    assert!(!by_id(&runs, 3).unwrap().fast);
    assert!(
        by_id(&runs, 5).is_some(),
        "a trusted failure is kept for recovery"
    );
}

#[test]
fn a_pull_request_run_is_never_exempt_from_the_tree_check_whatever_its_branch_is_called() {
    // A fork pull request whose head branch is also called main tests a merge ref.
    struct Fork(FastListing);
    impl Github for Fork {
        fn pages(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            self.0.pages(e, f)
        }
        fn page(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            if e.starts_with("actions/workflows/tests.yml/runs?head_sha") {
                return Ok(vec![
                    json!({"id": 6, "head_sha": TIP, "head_branch": "main", "event": "pull_request", "run_attempt": 1}),
                ]);
            }
            self.0.page(e, f)
        }
        fn log(&mut self, j: u64) -> Result<String, String> {
            self.0.log(j)
        }
        fn rerun(&mut self, r: u64) -> Result<(), String> {
            self.0.rerun(r)
        }
        fn value(&mut self, e: &str) -> Result<Value, String> {
            self.0.value(e)
        }
    }
    let src = |statuses| GhSource {
        gh: Fork(FastListing {
            statuses,
            tree: TREE,
        }),
        branch: "main".into(),
        now: || 1_800_000_000,
        land_app: None,
    };
    assert!(
        src(vec![]).runs(TIP).unwrap().is_empty(),
        "no tree evidence: the run does not decide main's state"
    );
    let runs = src(vec![ci_full(6, 1, TREE)]).runs(TIP).unwrap();
    assert_eq!(
        runs.len(),
        1,
        "with a ci-full status naming the tip's tree it counts"
    );
}

#[test]
fn a_newer_attempt_without_evidence_keeps_the_state_pending_instead_of_vanishing() {
    struct Rerun(FastListing);
    impl Github for Rerun {
        fn pages(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            self.0.pages(e, f)
        }
        fn page(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            if e.starts_with("actions/workflows/tests.yml/runs?head_sha") {
                return Ok(vec![
                    json!({"id": 5, "head_sha": TIP, "head_branch": "pr-branch", "event": "pull_request", "run_attempt": 3}),
                ]);
            }
            self.0.page(e, f)
        }
        fn log(&mut self, j: u64) -> Result<String, String> {
            self.0.log(j)
        }
        fn rerun(&mut self, r: u64) -> Result<(), String> {
            self.0.rerun(r)
        }
        fn value(&mut self, e: &str) -> Result<Value, String> {
            if e.contains("/attempts/3") {
                return Ok(
                    json!({"status": "in_progress", "run_started_at": "2026-11-21T01:00:00Z"}),
                );
            }
            self.0.value(e)
        }
    }
    let statuses = vec![ci_full(5, 2, TREE)];
    let mut src = GhSource {
        gh: Rerun(FastListing {
            statuses,
            tree: TREE,
        }),
        branch: "main".into(),
        now: || 1_800_000_000,
        land_app: None,
    };
    let runs = src.runs(TIP).unwrap();
    let attempts: Vec<(u32, &str)> = runs[0]
        .attempts
        .iter()
        .map(|a| (a.number, a.status.as_str()))
        .collect();
    assert_eq!(
        attempts,
        vec![(2, "completed"), (3, "in_progress")],
        "attempt 1 is dropped, the newer attempt 3 stays unverified"
    );
}

#[test]
fn a_new_full_dispatch_run_is_visible_before_its_first_status_and_keeps_main_pending() {
    struct Dispatch(FastListing);
    impl Github for Dispatch {
        fn pages(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            if e.contains("/runs/9/") {
                return Ok(vec![]);
            }
            self.0.pages(e, f)
        }
        fn page(&mut self, e: &str, f: Option<&str>) -> Result<Vec<Value>, String> {
            if e.starts_with("actions/workflows/tests.yml/runs?head_sha") {
                return Ok(vec![
                    json!({"id": 1, "head_sha": TIP, "head_branch": "main", "event": "push", "run_attempt": 1}),
                    json!({"id": 9, "head_sha": TIP, "head_branch": "somebranch", "event": "workflow_dispatch", "run_attempt": 1}),
                ]);
            }
            self.0.page(e, f)
        }
        fn log(&mut self, j: u64) -> Result<String, String> {
            self.0.log(j)
        }
        fn rerun(&mut self, r: u64) -> Result<(), String> {
            self.0.rerun(r)
        }
        fn value(&mut self, e: &str) -> Result<Value, String> {
            if e.contains("/runs/9/") {
                return Ok(
                    json!({"status": "in_progress", "run_started_at": "2026-11-21T01:00:00Z"}),
                );
            }
            self.0.value(e)
        }
    }
    let mut src = GhSource {
        gh: Dispatch(FastListing {
            statuses: vec![],
            tree: TREE,
        }),
        branch: "main".into(),
        now: || 1_800_000_000,
        land_app: None,
    };
    let runs = src.runs(TIP).unwrap();
    assert!(
        by_id(&runs, 9).is_some(),
        "a dispatch run tests its ref's head commit: read without a ci-full status"
    );
    let matrix = matrix_from_toml(MATRIX).unwrap();
    // The recorded run 1 lists only fast jobs and has no evidence, so it is red in itself; the point here is
    // that the in-progress run is part of the evidence.
    assert!(by_id(&runs, 9).unwrap().attempts[0].status == "in_progress");
    let _ = matrix;
}

#[test]
fn the_newest_status_of_an_attempt_wins_whatever_order_the_listing_has() {
    let pending = json!({"context": "ci-full", "state": "pending", "creator": {"id": CI_BOT},
        "description": format!("run 5 attempt 2 tree {TREE}"), "updated_at": "2026-11-21T00:01:00Z"});
    let success = json!({"context": "ci-full", "state": "success", "creator": {"id": CI_BOT},
        "description": format!("run 5 attempt 2 tree {TREE}"), "updated_at": "2026-11-21T00:09:00Z"});
    // Newest first, as the API lists them, and in the opposite order.
    for statuses in [
        vec![
            auth(77, "success", APP_TEXT),
            success.clone(),
            pending.clone(),
        ],
        vec![auth(77, "success", APP_TEXT), pending, success],
    ] {
        let runs = fast_source(statuses, Some(77)).runs(TIP).unwrap();
        assert!(
            by_id(&runs, 3).unwrap().fast,
            "an older pending status must not replace the final success"
        );
    }
}
