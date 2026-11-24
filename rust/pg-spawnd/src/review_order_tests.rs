use super::*;

fn run(state: &Path, id: &str, ended: i64, leg: &str, verdict: bool, log: &str) -> Value {
    let dir = state.join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("live.log"), log).unwrap();
    json!({"role":"review", "status":"completed", "started":ended - 1, "ended":ended, "run_id":id,
        "chosen_leg":{"id":leg}, "review_result":if verdict {"verdict"} else {"missing_verdict"}})
}

fn step(input: u64, read: u64) -> String {
    format!("{{\"type\":\"step_finish\",\"part\":{{\"tokens\":{{\"input\":{input},\"output\":1,\"cache\":{{\"read\":{read},\"write\":0}}}}}}}}\n")
}

#[test]
fn usage_counts_cache_writes_in_the_input_total_but_not_as_cached() {
    let frame = "{\"type\":\"step_finish\",\"part\":{\"tokens\":{\"input\":10,\"output\":1,\"cache\":{\"read\":60,\"write\":30}}}}\n";
    assert_eq!(usage(frame), Some((100, 60)));
}

#[test]
fn usage_adds_cache_read_to_opencode_input_and_keeps_codex_input() {
    assert_eq!(usage(&(step(10, 90) + &step(5, 0))), Some((105, 90)));
    let codex = "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":120,\"cached_input_tokens\":100}}\n";
    assert_eq!(usage(codex), Some((120, 100)));
    assert_eq!(usage("plain text\n{\"type\":\"text\"}\n"), None);
}

#[test]
fn compares_windows_per_matched_leg_with_denominators_and_missing_usage() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let records = vec![
        run(s, "run-b1", 10, "w", false, &step(100, 0)),
        run(s, "run-b2", 11, "w", true, &step(100, 100)),
        run(s, "run-b3", 12, "w", true, "no usage here\n"),
        run(s, "run-a1", 30, "w", true, &step(20, 180)),
        run(s, "run-a2", 31, "w", true, &step(20, 180)),
        run(s, "run-onlyafter", 32, "x", true, &step(1, 1)),
        run(s, "run-outside", 99, "w", true, &step(1, 1)),
    ];
    let report = compare(s, &records, (0, 20), (20, 40), &[]);
    let w = &report["strata"]["w"];
    assert_eq!(w["matched"], true);
    assert_eq!(w["before"]["reviews"], 3);
    assert_eq!(w["before"]["no_verdict"], 1);
    assert_eq!(w["before"]["missing_usage"], 1);
    assert_eq!(w["before"]["input_tokens"], 300);
    assert_eq!(w["before"]["cached_input_tokens"], 100);
    assert_eq!(w["after"]["reviews"], 2);
    assert_eq!(w["after"]["no_verdict_share"], 0.0);
    assert_eq!(w["after"]["cached_input_share"], 0.9);
    assert_eq!(report["unmatched"], json!(["x"]));
    assert_eq!(report["matched_total"]["after"]["reviews"], 2);
    let only_x = compare(s, &records, (0, 20), (20, 40), &["x".into()]);
    assert_eq!(only_x["matched_total"]["before"]["reviews"], 0);
    assert_eq!(
        only_x["matched_total"]["before"]["no_verdict_share"],
        Value::Null
    );
}

#[test]
fn execute_refuses_overlapping_or_missing_windows() {
    let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert!(execute(
        args(&["--before-since", "2026-01-01T00:00:00Z"]),
        &mut Vec::new()
    )
    .is_err());
    let overlap = args(&[
        "--before-since",
        "2026-01-01T00:00:00Z",
        "--before-until",
        "2026-01-03T00:00:00Z",
        "--after-since",
        "2026-01-02T00:00:00Z",
        "--after-until",
        "2026-01-04T00:00:00Z",
    ]);
    assert!(execute(overlap, &mut Vec::new()).is_err());
    let mut out = Vec::new();
    execute(args(&["--help"]), &mut out).unwrap();
    assert!(String::from_utf8(out).unwrap().contains("--before-since"));
}

fn attempts(rows: &[(&str, &str)]) -> Value {
    json!(rows
        .iter()
        .map(|(leg, status)| json!({"leg":{"id":leg}, "status":status}))
        .collect::<Vec<_>>())
}

#[test]
fn shared_log_fallthrough_is_excluded_and_counted_apart_from_missing_usage() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut mixed = run(s, "run-mix", 10, "w", true, &step(100, 100));
    mixed["attempts"] = attempts(&[
        ("first", "limited"),
        ("skip", "skipped"),
        ("w", "completed"),
    ]);
    let mut single = run(s, "run-one", 11, "w", true, &step(10, 90));
    single["attempts"] = attempts(&[("skip", "skipped"), ("w", "completed")]);
    let after = run(s, "run-after", 30, "w", true, &step(1, 1));
    let report = compare(s, &[mixed, single, after], (0, 20), (20, 40), &[]);
    let before = &report["strata"]["w"]["before"];
    assert_eq!(before["reviews"], 2);
    assert_eq!(before["mixed_leg_excluded"], 1);
    assert_eq!(before["missing_usage"], 1);
    assert_eq!(before["usage_runs"], 1);
    assert_eq!(before["input_tokens"], 100);
    assert_eq!(before["cached_input_tokens"], 90);
}

#[test]
fn archived_attempt_logs_leave_a_clean_final_leg_log_that_counts() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut retried = run(s, "run-retry", 10, "w", true, &step(10, 90));
    retried["attempts"] = attempts(&[("first", "limited"), ("w", "completed")]);
    std::fs::write(s.join("runs/run-retry/live-attempt-1.log"), step(999, 0)).unwrap();
    let after = run(s, "run-after", 30, "w", true, &step(1, 1));
    let report = compare(s, &[retried, after], (0, 20), (20, 40), &[]);
    let before = &report["strata"]["w"]["before"];
    assert_eq!(before["mixed_leg_excluded"], 0);
    assert_eq!(before["usage_runs"], 1);
    assert_eq!(before["input_tokens"], 100);
    assert_eq!(before["cached_input_tokens"], 90);
}

#[test]
fn reviews_group_by_the_last_launched_leg_and_unlaunched_runs_are_reported_apart() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut trailing = run(s, "run-trail", 10, "skipped-leg", true, &step(10, 90));
    trailing["attempts"] = attempts(&[("w", "completed"), ("skipped-leg", "skipped")]);
    let mut none = run(s, "run-none", 11, "skipped-leg", false, &step(500, 0));
    none["attempts"] = attempts(&[("skipped-leg", "skipped"), ("other", "skipped")]);
    let mut after = run(s, "run-after", 30, "w", true, &step(1, 1));
    after["attempts"] = attempts(&[("w", "completed")]);
    let report = compare(s, &[trailing, none, after], (0, 20), (20, 40), &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(report["strata"]["w"]["before"]["input_tokens"], 100);
    assert!(report["strata"]["skipped-leg"].is_null());
    assert_eq!(report["excluded"]["unlaunched"]["before"], 1);
}

#[test]
fn a_review_that_started_before_the_cutoff_is_not_an_after_review() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut crossing = run(s, "run-cross", 25, "w", true, &step(1, 1));
    crossing["started"] = json!(15); // dispatched in the before window, finished after
    let mut nostart = run(s, "run-nostart", 26, "w", true, &step(1, 1));
    nostart.as_object_mut().unwrap().remove("started");
    let before = run(s, "run-before", 10, "w", true, &step(1, 1));
    let after = run(s, "run-after", 30, "w", true, &step(1, 1));
    let report = compare(
        s,
        &[crossing, nostart, before, after],
        (0, 20),
        (20, 40),
        &[],
    );
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(report["strata"]["w"]["after"]["reviews"], 1);
    assert_eq!(report["excluded"]["boundary_crossing"]["before"], 1);
    assert_eq!(report["excluded"]["no_start_time"], 1);
}
