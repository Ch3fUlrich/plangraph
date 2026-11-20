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
