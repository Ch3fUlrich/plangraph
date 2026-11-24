use super::*;

fn run(state: &Path, id: &str, ended: i64, leg: &str, verdict: bool, log: &str) -> Value {
    let dir = state.join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("live.log"), log).unwrap();
    json!({"role":"review", "status":"completed", "started":ended - 1, "ended":ended, "run_id":id,
        "chosen_leg":{"id":leg}, "review_result":if verdict {"verdict"} else {"missing_verdict"}})
}

const CONTRACT: &str = "Name each finding as file:line. End with the line HEAD: abc and then the line VERDICT: ACCEPT or VERDICT: REQUEST_CHANGES.\n";

/// Store the prompt of a run in one of the two recorded layouts: `after` closes with the
/// `=== REVIEW OUTPUT` contract, `before` carries the contract ahead of the inline diff.
fn prompt(state: &Path, id: &str, after: bool) {
    let body = "x".repeat(5000);
    let text = if after {
        format!("brief\n=== INLINE DIFF (base a, head b) ===\n{body}\n=== END INLINE DIFF ===\n\n=== REVIEW OUTPUT\n{CONTRACT}")
    } else {
        format!("brief\n{CONTRACT}\n\n=== INLINE DIFF (base a, head b) ===\n{body}\n=== END INLINE DIFF ===\n")
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
    let report = compare(s, &records, WINDOW, &[], &[]);
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
    let only_x = compare(s, &records, WINDOW, &["x".into()], &[]);
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
    let report = compare(s, &[bare, late_started, nostart], WINDOW, &[], &[]);
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
    let report = compare(s, &[mixed, single, empty_mixed, after], WINDOW, &[], &[]);
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
    let report = compare(s, &[retried, after], WINDOW, &[], &[]);
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
    let report = compare(s, &[trailing, none, after], WINDOW, &[], &[]);
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

#[test]
fn layouts_come_from_prompt_structure_and_unknown_ones_are_reported_apart() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let store = |id: &str, text: &str| {
        std::fs::write(s.join("runs").join(id).join("prompt.txt"), text).unwrap();
    };
    let ids = [
        "run-mention",
        "run-shortdiff",
        "run-nocontract",
        "run-contractlast",
        "run-trailing",
    ];
    let records: Vec<_> = ids
        .iter()
        .enumerate()
        .map(|(i, id)| run(s, id, 10 + i as i64, "w", true, &step(1, 1)))
        .collect();
    // a mid-line mention of the heading is not the contract section
    store(
        "run-mention",
        &format!("brief\nsee the === REVIEW OUTPUT heading\n{CONTRACT}\n=== ITEM CONTEXT\nx\n"),
    );
    // old order with a short diff after the contract must not look like a closing contract
    store("run-shortdiff", &format!("brief\n{CONTRACT}\n=== INLINE DIFF (base a, head b) ===\n+x\n=== END INLINE DIFF ===\n"));
    // no output contract at all
    store("run-nocontract", "brief only\n");
    // a contract with no heading and nothing after it carries no layout evidence
    store("run-contractlast", &format!("brief\n{CONTRACT}"));
    // a marked prompt whose evidence still follows the contract is the order the policy reorders
    store(
        "run-trailing",
        &format!("brief\n=== REVIEW OUTPUT\n{CONTRACT}\n=== ITEM CONTEXT\nx\n"),
    );
    let closing = cohort_run(s, "run-closing", 90, "w", true, true, &step(1, 1));
    let records: Vec<_> = records.into_iter().chain([closing]).collect();
    let report = compare(s, &records, WINDOW, &[], &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 3); // mention + shortdiff + marked old
    assert_eq!(report["strata"]["w"]["after"]["reviews"], 1);
    assert_eq!(report["excluded"]["unknown_layout"], 2);
}

#[test]
fn strata_report_host_and_harness_provenance_and_filter_by_host() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let leg = json!({"id":"w", "harness":"opencode", "roles":["review","implement"]});
    let mut one = cohort_run(s, "run-h1", 10, "w", true, false, &step(1, 1));
    one["host"] = json!("host-a");
    one["chosen_leg"] = leg.clone();
    let mut two = cohort_run(s, "run-h2", 11, "w", true, true, &step(1, 1));
    two["host"] = json!("host-b");
    two["chosen_leg"] = leg.clone();
    let mut three = cohort_run(s, "run-h3", 20, "w", true, true, &step(1, 1));
    three["host"] = json!("host-a");
    three["chosen_leg"] = leg;
    let all = [one, two, three];
    let report = compare(s, &all, WINDOW, &[], &[]);
    let prov = &report["strata"]["w"]["provenance"];
    assert_eq!(prov["hosts"], json!({"host-a":2, "host-b":1}));
    assert_eq!(prov["harnesses"], json!({"opencode":3}));
    assert_eq!(prov["leg_roles"], json!(["implement", "review"]));
    let only_a = compare(s, &all, WINDOW, &[], &["host-a".into()]);
    assert_eq!(only_a["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(only_a["strata"]["w"]["after"]["reviews"], 1);
}

fn store_prompt(s: &Path, id: &str, text: &str) {
    std::fs::write(s.join("runs").join(id).join("prompt.txt"), text).unwrap();
}

#[test]
fn old_order_prompts_with_any_evidence_heading_after_the_contract_are_before() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let record = run(s, "run-old", 10, "w", true, &step(1, 1));
    store_prompt(
        s,
        "run-old",
        &format!("brief\n{CONTRACT}\n=== Evidence\nfixture evidence\n"),
    );
    let closing = cohort_run(s, "run-new", 30, "w", true, true, &step(1, 1));
    let report = compare(s, &[record, closing], WINDOW, &[], &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(report["excluded"]["unknown_layout"], 0);
}

#[test]
fn a_leading_layout_after_the_host_first_closing_prompt_was_not_reordered() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut early = cohort_run(s, "run-early", 10, "w", true, false, &step(1, 1));
    early["host"] = json!("h1");
    let mut first_new = cohort_run(s, "run-new", 30, "w", true, true, &step(1, 1));
    first_new["host"] = json!("h1");
    // same old layout, dispatched after this host already produced a reordered prompt
    let mut unchanged = cohort_run(s, "run-late", 40, "w", true, false, &step(1, 1));
    unchanged["host"] = json!("h1");
    // another host has no reordered prompt: exposure cannot be verified, so it counts nowhere
    let mut other = cohort_run(s, "run-other", 50, "w", true, false, &step(1, 1));
    other["host"] = json!("h2");
    let report = compare(s, &[early, first_new, unchanged, other], WINDOW, &[], &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1); // early
    assert_eq!(report["strata"]["w"]["after"]["reviews"], 1);
    assert_eq!(report["excluded"]["post_rollout_unreordered"], 1);
    assert_eq!(report["excluded"]["no_rollout_evidence"], 1);
}

#[test]
fn the_command_accepts_host_and_returns_json() {
    let state = tempfile::tempdir().unwrap();
    let args: Vec<String> = [
        "--state",
        state.path().to_str().unwrap(),
        "--since",
        "2026-01-01T00:00:00Z",
        "--until",
        "2026-02-01T00:00:00Z",
        "--leg",
        "w",
        "--host",
        "h1,h2",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let mut out = Vec::new();
    execute(args, &mut out).unwrap();
    let report: Value = serde_json::from_slice(&out).unwrap();
    assert_eq!(report["excluded"]["unknown_layout"], 0);
}

#[test]
fn provenance_comes_from_the_attempt_used_for_grouping() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut failed = cohort_run(s, "run-fail", 10, "w", false, false, &step(1, 1));
    failed.as_object_mut().unwrap().remove("chosen_leg");
    failed["attempts"] = json!([
        {"leg":{"id":"skip", "harness":"agy", "roles":["review"]}, "status":"skipped"},
        {"leg":{"id":"w", "harness":"opencode", "roles":["implement","review"]}, "status":"failed"}
    ]);
    let after = cohort_run(s, "run-after", 30, "w", true, true, &step(1, 1));
    let report = compare(s, &[failed, after], WINDOW, &[], &[]);
    let prov = &report["strata"]["w"]["provenance"];
    assert_eq!(prov["harnesses"]["opencode"], 1);
    assert!(prov["harnesses"]["agy"].is_null());
    assert_eq!(prov["leg_roles"], json!(["implement", "review"]));
}

#[test]
fn an_old_layout_needs_a_reordered_prompt_on_its_host_to_count_as_before() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let mut lone = cohort_run(s, "run-lone", 10, "w", true, false, &step(1, 1));
    lone["host"] = json!("h-none"); // this host never produced a reordered prompt
    let report = compare(s, &[lone], WINDOW, &[], &[]);
    assert!(report["strata"]["w"].is_null());
    assert_eq!(report["excluded"]["no_rollout_evidence"], 1);
}

#[test]
fn a_skipped_only_run_does_not_set_the_rollout_cutoff() {
    let state = tempfile::tempdir().unwrap();
    let s = state.path();
    let before = cohort_run(s, "run-before", 10, "w", true, false, &step(1, 1));
    let mut skipped_only = cohort_run(s, "run-skip", 5, "w", true, true, &step(1, 1));
    skipped_only["attempts"] = attempts(&[("w", "skipped")]);
    let first_launched = cohort_run(s, "run-first", 30, "w", true, true, &step(1, 1));
    let report = compare(s, &[before, skipped_only, first_launched], WINDOW, &[], &[]);
    assert_eq!(report["strata"]["w"]["before"]["reviews"], 1);
    assert_eq!(report["strata"]["w"]["after"]["reviews"], 1);
    assert_eq!(report["excluded"]["unlaunched"], 1);
}
