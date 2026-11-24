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

/// A recorded landed train as the keeper's `timeline` action returns it (milliseconds): the stage rows of
/// the train and its CI jobs. The whole-run `ci` row is replaced by the jobs.
fn keeper_reply(base_s: i64, review_s: i64) -> serde_json::Value {
    let row = |stage: &str, head: &str, start: i64, end: i64| {
        json!({"stage": stage, "head": head, "attempt": 1,
               "started_at_ms": (base_s + start) * 1000, "ended_at_ms": (base_s + end) * 1000})
    };
    let job = |name: &str, conclusion: &str, start: i64, end: i64| {
        json!({"name": name, "head": "h", "conclusion": conclusion,
               "started_at_ms": (base_s + start) * 1000, "ended_at_ms": (base_s + end) * 1000})
    };
    json!({
        "stages": [
            row("fast_checks", "h", 0, 300),
            row("push", "h", 400, 2200),
            row("ci", "h", 2250, 3250),
            row("review", "h", 3300, 3300 + review_s),
            row("go", "h", 3300 + review_s, 3900 + review_s),
            row("merge", "h", 3900 + review_s, 3960 + review_s),
            row("host_sync", "h", 3960 + review_s, 4100 + review_s),
        ],
        "jobs": [
            job("Rust on Windows", "failure", 2300, 3250),
            job("worker crate", "success", 2300, 2890),
        ],
    })
}

#[test]
fn a_keeper_timeline_becomes_stages_with_jobs_in_place_of_the_ci_run() {
    let stages = from_keeper("train-1", &keeper_reply(0, 1500));
    let names: Vec<&str> = stages.iter().map(|s| s.stage.as_str()).collect();
    assert!(!names.contains(&"ci"), "{names:?}");
    let win = stages
        .iter()
        .find(|s| s.stage == "ci:Rust on Windows")
        .unwrap();
    assert_eq!(
        (win.host.as_str(), win.result.as_str(), win.secs()),
        ("github", "failure", 950)
    );
    assert!(missing(&stages).is_empty(), "{:?}", missing(&stages));
}

#[test]
fn a_landed_train_report_names_the_slowest_stage_per_train_and_over_the_last_trains() {
    let mut stages = Vec::new();
    for (name, base, review) in [
        ("train-1", 0, 1500),
        ("train-2", 100_000, 1400),
        ("train-3", 200_000, 9000),
    ] {
        stages.extend(from_keeper(name, &keeper_reply(base, review)));
    }
    let r = report(&stages, 10);
    let by = |n: &str| r.trains.iter().find(|t| t.subject == n).unwrap();
    assert_eq!(by("train-1").bottleneck, ("push".to_owned(), 1800));
    assert_eq!(by("train-3").bottleneck, ("review".to_owned(), 9000));
    assert!(r.trains.iter().all(|t| t.missing.is_empty()));
    assert_eq!(r.top_stages[0].0, "review");
    let lines = render(&r);
    assert!(
        lines
            .last()
            .unwrap()
            .starts_with("slowest stage over 3 trains: review"),
        "{lines:?}"
    );
}

#[test]
fn a_train_with_a_missing_stage_is_reported_incomplete() {
    let mut reply = keeper_reply(0, 100);
    reply["stages"]
        .as_array_mut()
        .unwrap()
        .retain(|r| r["stage"] != "host_sync" && r["stage"] != "go");
    let stages = from_keeper("train-9", &reply);
    assert_eq!(missing(&stages), vec!["go", "host_sync"]);
    let lines = render(&report(&stages, 5));
    assert!(
        lines[0].contains("incomplete: no go, host_sync"),
        "{lines:?}"
    );
}
