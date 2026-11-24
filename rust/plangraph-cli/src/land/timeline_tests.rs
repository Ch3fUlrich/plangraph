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
    assert_eq!((by("t1").wall_secs, by("t1").post_merge_secs), (5460, 140));
    assert!(!by("t1").critical_path.contains(&"host_sync".to_owned()));
    assert_eq!(r.top_stages[0].0, "review", "{:?}", r.top_stages);
    // Only the last N trains count.
    let last_one = report(&stages, 1);
    assert_eq!(last_one.trains.len(), 1);
    assert_eq!(last_one.trains[0].subject, "t3");
    let lines = render(&r);
    assert!(lines
        .iter()
        .any(|l| l.starts_with("t1: 5460s") && l.contains("bottleneck review 1500s")));
    assert!(lines
        .last()
        .unwrap()
        .starts_with("slowest stage over 3 trains: review"));
}

/// A recorded landed train as the keeper's `timeline` action returns it (milliseconds): the stage rows of
/// the train and its CI jobs. The whole-run `ci` row is replaced by the jobs.
fn keeper_reply(base_s: i64, review_s: i64) -> serde_json::Value {
    let row = |stage: &str, head: &str, start: i64, end: i64| {
        json!({"stage": stage, "head": head, "attempt": 1, "host": "box-1", "result": "ok", "detail": "",
               "started_at_ms": (base_s + start) * 1000, "ended_at_ms": (base_s + end) * 1000})
    };
    let gate = |family: &str, start: i64, end: i64| {
        json!({"stage": "fast_checks", "head": "h", "attempt": base_s + start, "host": "box-1",
               "result": if family == "rust" { "failed" } else { "ok" }, "detail": family,
               "started_at_ms": (base_s + start) * 1000, "ended_at_ms": (base_s + end) * 1000})
    };
    let job = |name: &str, conclusion: &str, start: i64, end: i64| {
        json!({"name": name, "head": "h", "conclusion": conclusion,
               "started_at_ms": (base_s + start) * 1000, "ended_at_ms": (base_s + end) * 1000})
    };
    json!({
        "stages": [
            row("fast_checks", "h", 0, 300),
            gate("policy", 0, 100),
            gate("rust", 100, 300),
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
    let rust = stages
        .iter()
        .find(|s| s.stage == "fast_checks:rust")
        .unwrap();
    assert_eq!(
        (rust.host.as_str(), rust.result.as_str(), rust.secs()),
        ("box-1", "failed", 200)
    );
    let bare = stages.iter().find(|s| s.stage == "fast_checks").unwrap();
    assert_eq!(bare.result, "ok");
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
        .retain(|r| r["stage"] != "host_sync" && r["stage"] != "go" && r["detail"] != "rust");
    let stages = from_keeper("train-9", &reply);
    assert_eq!(missing(&stages), vec!["go", "host_sync"]);
    // The policy sub-stage is still there, so the push gate counts as reported.
    let lines = render(&report(&stages, 5));
    assert!(
        lines[0].contains("incomplete: no go, host_sync"),
        "{lines:?}"
    );
}

#[test]
fn a_train_without_fast_checks_review_or_push_gate_stages_is_incomplete() {
    // Only what the lander and the importer write: push, jobs, GO, merge, host sync.
    let row = |stage: &str, start: i64, end: i64| {
        json!({"stage": stage, "head": "h", "attempt": 1,
               "started_at_ms": start * 1000, "ended_at_ms": end * 1000})
    };
    let reply = json!({
        "stages": [row("push", 0, 600), row("go", 1500, 1800), row("merge", 1800, 1830), row("host_sync", 1830, 1900)],
        "jobs": [{"name": "Rust workspace", "head": "h", "conclusion": "success",
                  "started_at_ms": 650_000, "ended_at_ms": 1_450_000}],
    });
    let stages = from_keeper("train-7", &reply);
    assert_eq!(
        missing(&stages),
        vec!["fast_checks", "review", PUSH_GATE_STAGES]
    );
    // Rows that name no host or result say so instead of claiming one.
    let push = stages.iter().find(|s| s.stage == "push").unwrap();
    assert_eq!(
        (push.host.as_str(), push.result.as_str()),
        ("unknown", "unknown")
    );
    assert!(render(&report(&stages, 1))[0]
        .contains("incomplete: no fast_checks, review, push_gate_stages"));
}

#[test]
fn the_time_to_merge_ends_at_the_merge_and_the_host_sync_is_reported_after_it() {
    let r = report(&from_keeper("t", &keeper_reply(0, 100)), 1);
    let t = &r.trains[0];
    // Merge ends at 3960 + 100; the host sync runs 140 s more.
    assert_eq!((t.wall_secs, t.post_merge_secs), (4060, 140));
    assert!(!t.critical_path.iter().any(|s| s == "host_sync"));
    assert!(render(&r)[0].contains("4060s from writer done to merge (140s after it)"));
}

#[test]
fn a_roster_train_without_any_stage_is_kept_and_reported_incomplete() {
    let stages = from_keeper("landed", &keeper_reply(0, 100));
    let roster = vec!["silent".to_owned(), "landed".to_owned()];
    let r = report_roster(&roster, &stages, 10);
    assert_eq!(
        r.trains
            .iter()
            .map(|t| t.subject.as_str())
            .collect::<Vec<_>>(),
        ["silent", "landed"]
    );
    assert_eq!(r.trains[0].missing.len(), REQUIRED.len() + 1);
    assert_eq!(r.trains[0].wall_secs, 0);
    assert!(render(&r)[0].starts_with("silent: 0s"));
    assert!(r.trains[1].missing.is_empty());
    assert_eq!(report_roster(&roster, &stages, 1).trains.len(), 1);
}
