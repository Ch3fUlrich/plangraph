use super::*;

fn class_of(log: &str) -> Task {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.log = log.into();
    let defined = std::cell::Cell::new(0);
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        defined.set(defined.get() + 1);
        Ok(true)
    })
    .unwrap();
    assert_eq!(
        defined.get(),
        1,
        "every red head gets a fix task whatever its class"
    );
    tasks(temp.path()).remove(0)
}

#[test]
fn a_failing_test_row_carries_class_run_class_and_job_id() {
    let task = class_of(LOG);
    assert_eq!(
        (task.class.as_str(), task.run_class.as_str()),
        ("code", "code")
    );
    assert_ne!(task.job_id, 0);
}

#[test]
fn fmt_and_infrastructure_reds_still_get_a_task_with_their_class() {
    let fmt =
        class_of("Diff in /w/src/a.rs:3:\n-x\n+y\nError: Process completed with exit code 1.");
    assert_eq!(fmt.class, "fmt");
    let infra = class_of("lost communication with the server\n");
    assert_eq!(infra.class, "infrastructure");
}

#[test]
fn a_known_flake_row_is_classed_flake_when_its_rerun_is_spent() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.runs[0]["run_attempt"] = json!(2);
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:FLAKE".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    assert_eq!(tasks(temp.path())[0].class, "flake");
}

#[test]
fn one_known_flake_does_not_class_the_real_failure_of_the_same_job_as_flake() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.runs[0]["run_attempt"] = json!(2);
    gh.log.push_str("\nFAILED tests/x.py::test_real - boom");
    let flakes = BTreeMap::from([("seat_pool::tests::broken".into(), "plangraph:FLAKE".into())]);
    scan(&mut gh, "o/r", temp.path(), &flakes, false, |_| Ok(false)).unwrap();
    let rows = tasks(temp.path());
    let class = |test: &str| rows.iter().find(|t| t.test == test).unwrap().class.clone();
    assert_eq!(class("seat_pool::tests::broken"), "flake");
    assert_eq!(class("tests/x.py::test_real"), "code");
}

#[test]
fn two_red_jobs_without_test_ids_get_two_tasks() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.log = "lost communication with the server\n".into();
    gh.two_jobs = true;
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    let rows = tasks(temp.path());
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert_eq!(rows[0].job, rows[1].job, "two jobs with one display name");
    assert_ne!(rows[0].job_id, rows[1].job_id);
    assert_ne!(rows[0].key, rows[1].key);
}

#[test]
fn a_rerun_that_turned_green_leaves_no_current_task() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    assert!(tasks(temp.path()).iter().all(|t| t.current));
    gh.runs[0]["conclusion"] = json!("success");
    scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    let rows = tasks(temp.path());
    assert!(
        !rows.is_empty() && rows.iter().all(|t| !t.current),
        "{rows:?}"
    );
}

#[test]
fn every_red_run_is_recorded_once_with_its_class_and_the_report_counts_the_gaps() {
    let temp = tempfile::tempdir().unwrap();
    let mut gh = Fixture::new();
    gh.two_jobs = true;
    gh.log = "Diff in src/a.rs:3:\n-x\n+y\n".into();
    let first = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    let summary: Vec<_> = first.iter().filter(|r| r.starts_with("red: ")).collect();
    assert_eq!(summary.len(), 1, "one line per red run: {first:?}");
    assert!(
        summary[0].contains("pr-42")
            && summary[0].contains("class fmt")
            && summary[0].contains("2 task(s)"),
        "{summary:?}"
    );
    let again = scan(&mut gh, "o/r", temp.path(), &BTreeMap::new(), false, |_| {
        Ok(false)
    })
    .unwrap();
    assert!(again.iter().all(|r| !r.starts_with("red: ")), "{again:?}");
    let report = super::super::report::report(temp.path()).unwrap();
    assert_eq!(report["red_runs"], 1);
    assert_eq!(report["red_runs_by_class"]["fmt"], 1);
    let sha = tasks(temp.path())[0].head_sha.clone();
    assert_eq!(
        report["red_runs_per_head_sha_by_class"][format!("pr-42@{sha}")]["fmt"],
        1
    );
    assert_eq!(report["red_runs_without_class"], 0);
    assert_eq!(report["red_runs_without_task"], 0);
}

#[test]
fn the_report_keeps_two_head_commits_of_one_pull_request_apart_and_counts_real_fix_outcomes() {
    use super::super::report::{append, report, RunRow};
    let temp = tempfile::tempdir().unwrap();
    let row = |run_id, sha: &str| RunRow {
        run_id,
        attempt: 1,
        head: "pr-9".into(),
        head_sha: sha.into(),
        run_class: "fmt".into(),
        jobs: vec![("Rust".into(), run_id, "fmt".into())],
        tasks: 1,
        action: "fix-task".into(),
    };
    append(temp.path(), &row(1, "aaa")).unwrap();
    append(temp.path(), &row(2, "bbb")).unwrap();
    std::fs::write(
        temp.path().join("autofix.json"),
        json!({"done": {
            "o/r@aaa:fmt": "job 1: pushed deadbeef",
            "o/r@deadbeef:fmt": "produced by the fix of job 1",
            "o/r@bbb:fmt": "job 2: 80 lines; left to the fix writer"
        }})
        .to_string(),
    )
    .unwrap();
    let out = report(temp.path()).unwrap();
    assert_eq!(
        out["red_runs_per_head_sha_by_class"]["pr-9@aaa"]["fmt"], 1,
        "{out}"
    );
    assert_eq!(
        out["red_runs_per_head_sha_by_class"]["pr-9@bbb"]["fmt"], 1,
        "{out}"
    );
    assert_eq!(out["automatic_fixes_by_class"]["fmt"]["pushed"], 1, "{out}");
    assert_eq!(
        out["automatic_fixes_by_class"]["fmt"]["left_to_writer"], 1,
        "{out}"
    );
}
