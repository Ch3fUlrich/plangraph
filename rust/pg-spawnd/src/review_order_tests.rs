use super::*;

fn run(state: &Path, id: &str, ended: i64, leg: &str, verdict: bool, log: &str) -> Value {
    let dir = state.join("runs").join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("live.log"), log).unwrap();
    json!({"role":"review", "status":"completed", "ended":ended, "run_id":id,
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
