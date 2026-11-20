use super::*;
use serde_json::json;

#[test]
fn groups_by_top_level_dir_and_packs_small_dirs_and_splits_big_dir() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 200}); // 600 chars
    let files = vec![
        ("alpha/a1.rs".into(), vec![b'a'; 250]),
        ("alpha/a2.rs".into(), vec![b'b'; 250]),
        ("beta/b1.rs".into(), vec![b'c'; 100]),
        ("gamma/g1.rs".into(), vec![b'd'; 100]),
    ];
    let groups = plan_groups_from_measured(files, &leg, "review", true).unwrap();
    assert_eq!(
        groups,
        vec![
            vec!["alpha/a1.rs".to_string()],
            vec!["alpha/a2.rs".to_string()],
            vec!["beta/b1.rs".to_string(), "gamma/g1.rs".to_string()],
        ]
    );
}

#[test]
fn keeps_small_directory_together_even_across_many_files() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 200}); // 600 chars
    let files = vec![
        ("alpha/a1.rs".into(), vec![b'a'; 50]),
        ("alpha/a2.rs".into(), vec![b'b'; 50]),
        ("beta/b1.rs".into(), vec![b'c'; 250]),
    ];
    let groups = plan_groups_from_measured(files, &leg, "review", true).unwrap();
    assert_eq!(
        groups,
        vec![
            vec!["alpha/a1.rs".to_string(), "alpha/a2.rs".to_string()],
            vec!["beta/b1.rs".to_string()],
        ]
    );
}

#[test]
fn single_file_exceeding_limit_refuses_naming_file() {
    let leg = json!({"tool_mode": "inline-only", "max_request_tokens": 120});
    let files = vec![
        ("small.rs".into(), vec![b'a'; 10]),
        ("crates/big/lib.rs".into(), vec![b'x'; 200]),
    ];
    let err = plan_groups_from_measured(files, &leg, "review", true).unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("prompt_too_large"), "{msg}");
    assert!(msg.contains("crates/big/lib.rs"), "{msg}");
}

#[test]
fn parse_verdict_ignores_fences_and_quotes_and_uses_last_line() {
    assert_eq!(
        parse_verdict_text(
            "```\nVERDICT: REQUEST_CHANGES\n```\n> VERDICT: FAIL\nVERDICT: ACCEPT\n"
        ),
        Some(SeatVerdict::Pass)
    );
    assert_eq!(
        parse_verdict_text("VERDICT: ACCEPT\nWait:\nVERDICT: REQUEST_CHANGES\n"),
        Some(SeatVerdict::RequestChanges)
    );
    assert_eq!(parse_verdict_text("VERDICT: pass | fix"), None);
    assert_eq!(parse_verdict_text("VERDICT: pass?"), None);
}

fn seat(status: &str, exit: i64, verdict: Option<&str>) -> Value {
    let mut r = json!({"run_id": "run-x", "status": status, "exit_code": exit,
                       "review_group": {"index": 1, "total": 2, "paths": ["a/x"]}});
    if let Some(v) = verdict {
        r["verdict"] = json!(v);
    }
    r
}

#[test]
fn aggregate_fails_the_review_when_any_group_does_not_pass() {
    let st = Path::new("/nonexistent");
    let pass = || seat("completed", 0, Some("PASS"));
    let all = aggregate_group_seats(st, "seats-a", &[pass(), pass()], false);
    assert_eq!(
        (all["status"].as_str(), all["verdict"].as_str()),
        (Some("completed"), Some("PASS"))
    );
    let changes = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 0, Some("REQUEST_CHANGES"))],
        false,
    );
    assert_eq!(changes["status"], "failed");
    assert_eq!(changes["verdict"], "REQUEST_CHANGES");
    assert_eq!(changes["reason"], "review_request_changes");
    let halt = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 0, Some("HALT"))],
        false,
    );
    assert_eq!(
        (halt["verdict"].as_str(), halt["reason"].as_str()),
        (Some("FAIL"), Some("review_failed"))
    );
    let exit = aggregate_group_seats(
        st,
        "seats-a",
        &[pass(), seat("completed", 3, Some("PASS"))],
        false,
    );
    assert_eq!(exit["verdict"], "FAIL");
    let pending = aggregate_group_seats(st, "seats-a", &[pass(), seat("running", 0, None)], false);
    assert_eq!(pending["status"], "running");
    assert_eq!(pending["passed"], false);
    let none = aggregate_group_seats(st, "seats-a", &[], false);
    assert_eq!(
        (none["status"].as_str(), none["passed"].as_bool()),
        (Some("failed"), Some(false))
    );
}
