use super::timeline::*;
use serde_json::json;

fn st(subject: &str, stage: &str, start: i64, end: i64) -> Stage {
    Stage {
        subject: subject.into(),
        head: "h".into(),
        stage: stage.into(),
        host: "host".into(),
        start,
        end,
        result: "success".into(),
    }
}

/// A recorded train: fast checks, three failed push gates, a long wait, parallel CI jobs, review, GO, merge.
fn fixture_train(name: &str, base: i64) -> Vec<Stage> {
    vec![
        st(name, "fast_checks", base, base + 300),
        st(name, "push_gate:rust", base + 400, base + 1000),
        st(name, "push_gate:tests", base + 1000, base + 2200),
        st(name, "ci:Rust on Windows", base + 2300, base + 3250),
        st(name, "ci:worker crate", base + 2300, base + 2890),
        st(name, "ci:Rust workspace", base + 2300, base + 2800),
        st(name, "review", base + 3300, base + 4800),
        st(name, "go_request", base + 4800, base + 5400),
        st(name, "merge", base + 5400, base + 5460),
        st(name, "host_sync", base + 5460, base + 5600),
    ]
}

#[test]
fn the_critical_path_takes_the_slowest_parallel_job_once() {
    let path = critical_path(&fixture_train("t1", 0));
    let names: Vec<&str> = path.iter().map(|s| s.stage.as_str()).collect();
    assert_eq!(
        names,
        vec![
            "fast_checks",
            "push_gate:rust",
            "push_gate:tests",
            "ci:Rust on Windows",
            "review",
            "go_request",
            "merge",
            "host_sync"
        ]
    );
}

#[test]
fn the_report_names_the_slowest_stage_per_train_and_over_the_last_trains() {
    let mut stages = fixture_train("t1", 0);
    stages.extend(fixture_train("t2", 100_000));
    // Train 3 spends its time waiting in review.
    let mut t3 = fixture_train("t3", 200_000);
    t3.iter_mut().find(|s| s.stage == "review").unwrap().end += 7000;
    for s in t3
        .iter_mut()
        .filter(|s| matches!(s.stage.as_str(), "go_request" | "merge" | "host_sync"))
    {
        s.start += 7000;
        s.end += 7000;
    }
    stages.extend(t3);
    let r = report(&stages, 10);
    assert_eq!(r.trains.len(), 3);
    let by = |n: &str| r.trains.iter().find(|t| t.subject == n).unwrap();
    assert_eq!(by("t1").bottleneck, ("review".to_owned(), 1500));
    assert_eq!(by("t3").bottleneck, ("review".to_owned(), 8500));
    assert_eq!(by("t1").wall_secs, 5600);
    assert_eq!(r.top_stages[0].0, "review", "{:?}", r.top_stages);
    // Only the last N trains count.
    let last_one = report(&stages, 1);
    assert_eq!(last_one.trains.len(), 1);
    assert_eq!(last_one.trains[0].subject, "t3");
    let lines = render(&r);
    assert!(lines
        .iter()
        .any(|l| l.starts_with("t1: 5600s") && l.contains("bottleneck review 1500s")));
    assert!(lines
        .last()
        .unwrap()
        .starts_with("slowest stage over 3 trains: review"));
}

#[test]
fn ci_jobs_become_stages_with_their_runner_and_result() {
    let jobs = vec![
        json!({"name": "Rust on Windows", "runner_name": "win-1", "conclusion": "failure",
               "started_at": "2026-11-21T07:26:00Z", "completed_at": "2026-11-21T07:41:54Z"}),
        json!({"name": "queued", "started_at": null, "completed_at": null}),
    ];
    let stages = ci_stages("train1", "abc", &jobs);
    assert_eq!(stages.len(), 1, "a job that never ran has no stage");
    assert_eq!(
        (
            stages[0].stage.as_str(),
            stages[0].host.as_str(),
            stages[0].result.as_str()
        ),
        ("ci:Rust on Windows", "win-1", "failure")
    );
    assert_eq!(stages[0].secs(), 954);
}

#[test]
fn the_ledger_round_trips_and_a_missing_one_is_empty() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("sub/ledger.jsonl");
    assert!(load(&path).unwrap().is_empty());
    for s in fixture_train("t1", 0) {
        append(&path, &s).unwrap();
    }
    assert_eq!(load(&path).unwrap(), fixture_train("t1", 0));
}
