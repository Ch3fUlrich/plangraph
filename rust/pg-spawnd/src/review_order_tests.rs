use super::*;

fn run(state: &Path, id: &str, ended: i64, leg: &str, verdict: bool, log: &str) -> Value {
    let dir = state.join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("live.log"), log).unwrap();
    json!({"role":"review", "status":"completed", "started":ended - 1, "ended":ended, "run_id":id,
        "chosen_leg":{"id":leg}, "review_result":if verdict {"verdict"} else {"missing_verdict"}})
}

const CONTRACT: &str = "=== REVIEW OUTPUT\nAnswer each vector.\n";

/// Store the prompt of a run: `after` closes with the review contract, `before` carries it early.
fn prompt(state: &Path, id: &str, after: bool) {
    let body = "x".repeat(5000);
    let text = if after {
        format!("brief\n{body}\n{CONTRACT}")
    } else {
        format!("brief\n{CONTRACT}{body}\n")
    };
    std::fs::write(state.join("runs").join(id).join("prompt.txt"), text).unwrap();
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

fn attempts(rows: &[(&str, &str)]) -> Value {
    json!(rows
        .iter()
        .map(|(leg, status)| json!({"leg":{"id":leg}, "status":status}))
        .collect::<Vec<_>>())
}

const WINDOW: (i64, i64) = (0, 100);

fn cohort_run(
    s: &Path,
    id: &str,
    ended: i64,
    leg: &str,
    verdict: bool,
    after: bool,
    log: &str,
) -> Value {
    let record = run(s, id, ended, leg, verdict, log);
    prompt(s, id, after);
    record
}

#[test]
fn compares_prompt_order_cohorts_per_matched_leg_with_denominators_and_missing_usage() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let records = vec![
        cohort_run(s, "run-b1", 10, "w", false, false, &step(100, 0)),
        cohort_run(s, "run-b2", 11, "w", true, false, &step(100, 100)),
        cohort_run(s, "run-b3", 12, "w", true, false, "no usage here\n"),
        cohort_run(s, "run-a1", 30, "w", true, true, &step(20, 180)),
        cohort_run(s, "run-a2", 31, "w", true, true, &step(20, 180)),
        cohort_run(s, "run-onlyafter", 32, "x", true, true, &step(1, 1)),
        cohort_run(s, "run-late", 999, "w", true, true, &step(1, 1)),
    ];
    let report = compare(s, &records, WINDOW, &[]);
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
    let only_x = compare(s, &records, WINDOW, &["x".into()]);
    assert_eq!(only_x["matched_total"]["before"]["reviews"], 0);
    assert_eq!(
        only_x["matched_total"]["before"]["no_verdict_share"],
        Value::Null
    );
}

#[test]
fn a_run_without_a_stored_prompt_belongs_to_no_cohort() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let bare = run(s, "run-bare", 10, "w", true, &step(1, 1));
    let mut late_started = cohort_run(s, "run-start", 20, "w", true, true, &step(1, 1));
    late_started["started"] = json!(500); // dispatched outside the window
    let mut nostart = cohort_run(s, "run-nostart", 21, "w", true, true, &step(1, 1));
    nostart.as_object_mut().unwrap().remove("started");
    let report = compare(s, &[bare, late_started, nostart], WINDOW, &[]);
    assert_eq!(report["excluded"]["no_prompt"], 1);
    assert_eq!(report["excluded"]["no_start_time"], 1);
    assert!(report["strata"]["w"].is_null());
}

#[test]
fn shared_log_fallthrough_is_excluded_without_counting_as_missing_usage() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut mixed = cohort_run(s, "run-mix", 10, "w", true, false, &step(100, 100));
    mixed["attempts"] = attempts(&[
        ("first", "limited"),
        ("skip", "skipped"),
        ("w", "completed"),
    ]);
    let mut single = cohort_run(s, "run-one", 11, "w", true, false, &step(10, 90));
    single["attempts"] = attempts(&[("skip", "skipped"), ("w", "completed")]);
    let mut empty_mixed = cohort_run(s, "run-mix2", 12, "w", true, false, "none\n");
    empty_mixed["attempts"] = attempts(&[("first", "limited"), ("w", "completed")]);
    let after = cohort_run(s, "run-after", 30, "w", true, true, &step(1, 1));
    let report = compare(s, &[mixed, single, empty_mixed, after], WINDOW, &[]);
    let before = &report["strata"]["w"]["before"];
    assert_eq!(before["reviews"], 3);
    assert_eq!(before["mixed_leg_excluded"], 2);
    assert_eq!(before["missing_usage"], 0);
    assert_eq!(before["usage_runs"], 1);
    assert_eq!(before["input_tokens"], 100);
    assert_eq!(before["cached_input_tokens"], 90);
}

#[test]
fn archived_attempt_logs_leave_a_clean_final_leg_log_that_counts() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut retried = cohort_run(s, "run-retry", 10, "w", true, false, &step(10, 90));
    retried["attempts"] = attempts(&[("first", "limited"), ("w", "completed")]);
    std::fs::write(s.join("runs/run-retry/live-attempt-1.log"), step(999, 0)).unwrap();
    let after = cohort_run(s, "run-after", 30, "w", true, true, &step(1, 1));
    let report = compare(s, &[retried, after], WINDOW, &[]);
    let before = &report["strata"]["w"]["before"];
    assert_eq!(before["mixed_leg_excluded"], 0);
    assert_eq!(before["usage_runs"], 1);
    assert_eq!(before["input_tokens"], 100);
    assert_eq!(before["cached_input_tokens"], 90);
}

#[test]
fn string_valued_legs_and_trailing_skips_resolve_to_the_last_launched_leg() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    // Recorded shape: legs are bare strings and the record has no chosen_leg.
    let mut trailing = cohort_run(s, "run-trail", 10, "unused", true, false, &step(10, 90));
    trailing.as_object_mut().unwrap().remove("chosen_leg");
    trailing["attempts"] = json!([
        {"leg":"w", "status":"completed"},
        {"leg":"skipped-leg", "status":"skipped"}
    ]);
    let mut none = cohort_run(s, "run-none", 11, "unused", false, false, &step(500, 0));
    none.as_object_mut().unwrap().remove("chosen_leg");
    none["attempts"] = json!([
        {"leg":"skipped-leg", "status":"skipped"},
        {"leg":"other", "status":"skipped"}
    ]);
    let mut after = cohort_run(s, "run-after", 30, "w", true, true, &step(1, 1));
    after["attempts"] = attempts(&[("w", "completed")]);
    let report = compare(s, &[trailing, none, after], WINDOW, &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(report["strata"]["w"]["before"]["input_tokens"], 100);
    assert!(report["strata"]["skipped-leg"].is_null());
    assert_eq!(report["excluded"]["unlaunched"], 1);
}

#[test]
fn execute_refuses_a_missing_or_empty_window() {
    let args = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert!(execute(args(&["--since", "2026-01-01T00:00:00Z"]), &mut Vec::new()).is_err());
    let empty = args(&[
        "--since",
        "2026-01-02T00:00:00Z",
        "--until",
        "2026-01-01T00:00:00Z",
    ]);
    assert!(execute(empty, &mut Vec::new()).is_err());
    let mut out = Vec::new();
    execute(args(&["--help"]), &mut out).unwrap();
    assert!(String::from_utf8(out).unwrap().contains("--since"));
}
