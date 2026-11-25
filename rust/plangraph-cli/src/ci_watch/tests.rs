use super::*;
use serde_json::json;

const PRS: &str = include_str!("../../tests/fixtures/ci-watch/prs.json");
const RUNS: &str = include_str!("../../tests/fixtures/ci-watch/runs.json");
const JOBS: &str = include_str!("../../tests/fixtures/ci-watch/jobs.json");
const LOG: &str = include_str!("../../tests/fixtures/ci-watch/failure.log");
struct Fixture {
    prs: Vec<Value>,
    runs: Vec<Value>,
    log: String,
    reruns: Vec<u64>,
    reads: usize,
    fail_logs: bool,
    fail_rerun: bool,
    run_pages: usize,
    metadata_jobs: Option<Vec<Value>>,
}
impl Fixture {
    fn new() -> Self {
        Self {
            prs: serde_json::from_str(PRS).unwrap(),
            runs: serde_json::from_str(RUNS).unwrap(),
            log: LOG.into(),
            reruns: vec![],
            reads: 0,
            fail_logs: false,
            fail_rerun: false,
            run_pages: 0,
            metadata_jobs: None,
        }
    }
}
impl Github for Fixture {
    fn pages(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>> {
        if endpoint.starts_with("pulls?") {
            Ok(self.prs.clone())
        } else {
            assert!(
                !endpoint.starts_with("actions/workflows/"),
                "runs must not paginate"
            );
            assert_eq!(
                endpoint,
                format!(
                    "actions/runs/{}/jobs?filter=latest&per_page=100",
                    self.runs[0]["id"].as_u64().unwrap()
                )
            );
            Ok(serde_json::from_str(JOBS).unwrap())
        }
    }
    fn page(&mut self, endpoint: &str, _: Option<&str>) -> Result<Vec<Value>> {
        assert!(endpoint.starts_with("actions/workflows/"));
        assert!(endpoint.ends_with("&branch=main") || endpoint.ends_with("&head_sha=abc123"));
        self.run_pages += 1;
        Ok(self.runs.clone())
    }
    fn value(&mut self, endpoint: &str) -> Result<Value> {
        assert_eq!(endpoint, "actions/runs/901/attempts/1/jobs?per_page=100");
        let jobs = self
            .metadata_jobs
            .as_ref()
            .expect("unexpected metadata job lookup");
        Ok(json!({"jobs": jobs, "total_count": jobs.len()}))
    }
    fn log(&mut self, job: u64) -> Result<String> {
        assert_eq!(job, 100);
        self.reads += 1;
        if self.fail_logs {
            return Err("logs expired".into());
        }
        Ok(self.log.clone())
    }
    fn rerun(&mut self, run: u64) -> Result<()> {
        self.reruns.push(run);
        if self.fail_rerun {
            return Err("rerun refused".into());
        }
        Ok(())
    }
}
fn tasks(dir: &Path) -> Vec<Task> {
    fs::read_to_string(dir.join("tasks.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
#[test]
fn run_history_is_read_one_page_per_query() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), true, |_| {
        Ok(true)
    })
    .unwrap();
    // One open PR head plus main: two single-page requests, never paginated.
    assert_eq!(gh.run_pages, 2);
}
#[test]
fn a_green_metadata_run_never_advances_the_failure_watch_cursor() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let mut metadata = gh.runs[0].clone();
    metadata["id"] = json!(901);
    metadata["display_title"] = json!("PR metadata check 901");
    metadata["conclusion"] = json!("success");
    gh.runs.push(metadata);
    gh.metadata_jobs = Some(vec![
        json!({"name": "ci-pr-description", "conclusion": "success"}),
        json!({"name": "Select suites for this change", "conclusion": "skipped"}),
    ]);
    let rows = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(true)
    })
    .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(tasks(temp.path())[0].run_id, 900);
    let ledger: Reruns = read(&temp.path().join("reruns.json")).unwrap();
    assert_eq!(
        ledger.newest.values().copied().collect::<Vec<_>>(),
        vec![900]
    );
}
#[test]
fn unconfirmed_metadata_on_one_pr_allows_the_watch_to_scan_main() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let mut metadata = gh.runs[0].clone();
    metadata["id"] = json!(901);
    metadata["display_title"] = json!("PR metadata check 901");
    metadata["conclusion"] = json!("success");
    gh.runs.push(metadata);
    gh.metadata_jobs = Some(vec![]);
    gh.runs[0]["event"] = json!("push");
    gh.runs[0]["head_branch"] = json!("main");

    let rows = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(true)
    })
    .unwrap();

    assert!(rows
        .iter()
        .any(|row| row.contains("ci-watch unconfirmed run=901")));
    let tasks = tasks(temp.path());
    assert_eq!(tasks.len(), 1);
    assert_eq!((tasks[0].head.as_str(), tasks[0].run_id), ("main", 900));
    let ledger: Reruns = read(&temp.path().join("reruns.json")).unwrap();
    assert_eq!(
        ledger.newest.values().copied().collect::<Vec<_>>(),
        vec![900]
    );
}
#[test]
fn failed_pr_rescan_updates_one_task_and_defines_once() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let mut calls = 0;
    for _ in 0..2 {
        let rows = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |t| {
            assert_eq!(t.priority, 10);
            calls += 1;
            Ok(true)
        })
        .unwrap();
        assert_eq!(rows.len(), 1);
    }
    let records = tasks(temp.path());
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].pr, Some(42));
    assert_eq!(records[0].test, "seat_pool::tests::broken");
    assert_eq!(calls, 1);
    assert_eq!(gh.reads, 2);
    gh.log = "FAILED tests/a.py::test_other - AssertionError".into();
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    assert_eq!(tasks(temp.path()).len(), 2);
}
#[test]
fn flake_reruns_once_then_records_recurrence_and_task() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:FLAKE".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| {
        panic!("first flake must rerun")
    })
    .unwrap();
    assert!(!temp.path().join("tasks.jsonl").exists());
    let rows = scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert!(rows[0].contains("recurrence"));
    assert_eq!(gh.reruns, vec![900]);
    assert_eq!(tasks(temp.path()).len(), 1);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert_eq!(
        fs::read_to_string(temp.path().join("recurrences.jsonl"))
            .unwrap()
            .lines()
            .count(),
        2
    );
}
#[test]
fn github_attempt_two_never_reruns_and_mixed_failure_gets_task() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.runs[0]["run_attempt"] = json!(2);
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:F".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert!(gh.reruns.is_empty());
    assert_eq!(tasks(temp.path()).len(), 1);
    gh.runs[0]["run_attempt"] = json!(1);
    gh.runs[0]["id"] = json!(900);
    gh.log.push_str("\nFAILED tests/x.py::test_new - fail");
    let other = tempfile::tempdir().unwrap();
    scan(&mut gh, "o/r", other.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert_eq!(tasks(other.path())[0].test, "tests/x.py::test_new");
}
#[test]
fn main_failure_and_latest_success_suppresses_older_failure() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.prs.clear();
    gh.runs.truncate(1);
    gh.runs[0]["head_branch"] = json!("main");
    gh.runs[0]["event"] = json!("push");
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    let t = tasks(temp.path());
    assert_eq!(t[0].head, "main");
    assert_eq!(t[0].pr, None);
    gh.runs[0]["conclusion"] = json!("success");
    let mut older = gh.runs[0].clone();
    older["id"] = json!(899);
    older["conclusion"] = json!("failure");
    gh.runs.push(older);
    let newer = tempfile::tempdir().unwrap();
    assert!(scan(
        &mut gh,
        "o/r",
        newer.path(),
        &BTreeMap::new(),
        false,
        |_| Ok(false)
    )
    .unwrap()
    .is_empty());
}
#[test]
fn dry_run_has_no_mutations_and_job_fallback_is_not_lost() {
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path().join("absent");
    let mut gh = Fixture::new();
    gh.log = "build failed".into();
    let rows = scan(&mut gh, "o/r", &dir, &BTreeMap::new(), true, |_| {
        panic!("dry run keeper call")
    })
    .unwrap();
    assert!(rows[0].contains("<job-failure>"));
    assert!(!dir.exists());
    assert!(gh.reruns.is_empty());
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:F".into())]);
    gh.log = LOG.into();
    assert!(
        scan(&mut gh, "o/r", &dir, &flakes, true, |_| panic!()).unwrap()[0]
            .contains("rerun failed")
    );
    assert!(!dir.exists());
    assert!(gh.reruns.is_empty());
}
#[test]
fn secrets_and_forty_line_excerpt() {
    // Build synthetic URL credentials at runtime for the public source scrub.
    let raw = format!("https://{}@example.com/x ghp_123abc github_pat_123abc sk-secret123 TOKEN=private password:secret Bearer private https://example.com/?token=private", "user:pass");
    let clean = logs::scrub(&raw);
    for secret in ["user:pass", "123abc", "secret123", "private"] {
        assert!(!clean.contains(secret), "{clean}");
    }
    let log = format!(
        "{}\nFAILED tests/a.py::test_b - {raw}\n{}",
        "context\n".repeat(50),
        "after\n".repeat(50)
    );
    let parsed = logs::failures(&log);
    assert_eq!(parsed["tests/a.py::test_b"].lines().count(), 40);
    assert!(!parsed["tests/a.py::test_b"].contains("ghp_"));
}
#[test]
fn remote_formats() {
    for url in [
        "git@example.com:owner/repo.git",
        "https://github.com/owner/repo.git",
    ] {
        assert_eq!(github::repository(url).unwrap(), "owner/repo");
    }
}

#[test]
fn log_read_cap_limits_consumption_not_just_saved_excerpt() {
    struct Counting {
        read: usize,
    }
    impl std::io::Read for Counting {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            self.read += buf.len();
            buf.fill(b'x');
            Ok(buf.len())
        }
    }
    let mut source = Counting { read: 0 };
    let (bytes, capped) = github::capped_log(&mut source).unwrap();
    assert!(capped);
    assert_eq!(bytes.len(), logs::LOG_CAP);
    assert_eq!(source.read, logs::LOG_CAP + 1);
    let (bytes, capped) = github::capped_log(LOG.as_bytes()).unwrap();
    assert!(!capped);
    assert_eq!(bytes, LOG.as_bytes());
}

#[test]
fn paginated_gh_json_supports_older_gh_without_slurp() {
    let pages = format!("{PRS}\n{PRS}");
    assert_eq!(github::parse_pages(&pages, None).unwrap().len(), 2);
    let pages = format!("{{\"jobs\":{JOBS}}}\n{{\"jobs\":[]}}");
    assert_eq!(github::parse_pages(&pages, Some("jobs")).unwrap().len(), 2);
    assert!(github::parse_pages("invalid", None).is_err());
}

#[test]
fn outages_do_not_starve_other_local_tasks_and_pending_delivery_retries() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.log.push_str("\nFAILED tests/x.py::test_other - fail");
    let rows = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Err("keeper offline".into())
    })
    .unwrap();
    assert_eq!(tasks(temp.path()).len(), 2);
    assert_eq!(
        rows.iter()
            .filter(|r| r.starts_with("keeper delivery pending"))
            .count(),
        2
    );
    assert!(tasks(temp.path()).iter().all(|t| !t.keeper_defined));
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(true)
    })
    .unwrap();
    assert!(tasks(temp.path()).iter().all(|t| t.keeper_defined));
    gh.fail_logs = true;
    let other = tempfile::tempdir().unwrap();
    scan(
        &mut gh,
        "o/r",
        other.path(),
        &BTreeMap::new(),
        false,
        |_| Ok(false),
    )
    .unwrap();
    assert_eq!(tasks(other.path())[0].test, "<job-failure>");
}
#[test]
fn rerun_request_failure_escalates_without_another_request() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.fail_rerun = true;
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:F".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert_eq!(tasks(temp.path()).len(), 1);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert_eq!(gh.reruns, vec![900]);
}

#[test]
fn new_failure_run_rechecks_keeper_but_local_row_stays_unique() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let mut calls = 0;
    for run in [900, 900, 901, 901] {
        gh.runs[0]["id"] = json!(run);
        scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
            calls += 1;
            Ok(true)
        })
        .unwrap();
    }
    assert_eq!(calls, 2);
    assert_eq!(tasks(temp.path()).len(), 1);
    assert_eq!(tasks(temp.path())[0].run_id, 901);
}

#[test]
fn scrub_provider_tokens_secret_variables_and_key_blocks() {
    let jwt = [
        "eyJhbGciOiJIUzI1NiJ9",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        "signatureabcdefgh",
    ]
    .join(".");
    let pem = format!(
        "-----BEGIN {}-----\nsynthetic material\n-----END {}-----",
        "PRIVATE KEY", "PRIVATE KEY"
    );
    let slack = ["xoxb", "1234567890", "abcdefghijklmnop"].join("-");
    let aws = ["AKIA", "ABCDEFGHIJKLMNOP"].concat();
    let raw = format!(
        "AWS_SECRET_ACCESS_KEY=synthetic client_secret_value=synthetic {jwt} {pem} {slack} {aws}"
    );
    let clean = logs::scrub(&raw);
    for value in [&jwt, &pem, &slack, &aws] {
        assert!(!clean.contains(value));
    }
    assert!(!clean.contains("synthetic"));
    assert_eq!(logs::scrub("basic usage"), "basic usage");
}

#[test]
fn in_flight_rerun_does_not_revive_older_completed_failures() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:F".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    // GitHub omits run 900 while its next attempt is queued/in progress.
    gh.runs.remove(0);
    assert!(
        scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false))
            .unwrap()
            .is_empty()
    );
    assert_eq!(gh.reruns, vec![900]);
    assert_eq!(gh.reads, 1);
}

#[test]
fn secret_named_modules_and_pytest_classes_keep_distinct_failure_ids() {
    let log="test redact_tokens::tests::rotates ... FAILED\ntest writer_secret::tests::other ... FAILED\nFAILED tests/x.py::TestTokens::test_x - AssertionError\nAWS_SECRET_ACCESS_KEY=synthetic";
    let found = logs::failures(log);
    assert_eq!(found.len(), 3);
    for id in [
        "redact_tokens::tests::rotates",
        "writer_secret::tests::other",
        "tests/x.py::TestTokens::test_x",
    ] {
        assert!(found.contains_key(id), "missing {id}: {found:?}");
        assert!(!found[id].contains("synthetic"));
    }
    assert_eq!(logs::scrub("token=value"), "[REDACTED]");
    let pgp = format!(
        "-----BEGIN {}-----\nsynthetic material\n-----END {}-----",
        "PGP PRIVATE KEY BLOCK", "PGP PRIVATE KEY BLOCK"
    );
    assert_eq!(logs::scrub(&pgp), "[REDACTED]");
}
