use pg_session_resume::contract::{findings_name, findings_round, glob_match, parse_status};

#[test]
fn findings_names_round_trip_and_reject_lookalikes() {
    assert_eq!(findings_name("pr17", 3), "pr17.fix-round-3.txt");
    assert_eq!(findings_round("pr17", "pr17.fix-round-3.txt"), Some(3));
    for bad in [
        "pr17.fix-round-0.txt",
        "pr17.fix-round-.txt",
        "pr17.fix-round-3.txt.tmp",
        "pr170.fix-round-3.txt",
        "xpr17.fix-round-3.txt",
        "pr17.fix-round-3a.txt",
        "pr17.fix-round-1234567890.txt",
        "pr17.status",
    ] {
        assert_eq!(findings_round("pr17", bad), None, "{bad}");
    }
}

#[test]
fn status_needs_the_lane_done_line_and_a_full_head() {
    let ok = parse_status(
        "l",
        "DONE k 1\nl DONE\nHEAD: ABCDEF0123456789abcdef0123456789abcdef01\nVERDICT: ACCEPT\n",
    );
    assert!(ok.done);
    assert_eq!(
        ok.head.as_deref(),
        Some("abcdef0123456789abcdef0123456789abcdef01")
    );
    assert!(!parse_status("l", "other DONE\n").done);
    assert_eq!(parse_status("l", "HEAD: abc\n").head, None);
}

#[test]
fn glob_star_matches_runs() {
    assert!(glob_match("*-L1", "build-L1"));
    assert!(glob_match("*-L1-*", "build-L1-0412"));
    assert!(glob_match("plan-*", "plan-order"));
    assert!(glob_match("a*b*c", "aXbYc"));
    assert!(!glob_match("*-L1", "build-L1-0412"));
    assert!(!glob_match("routing", "routing-1"));
    assert!(!glob_match("ab*ba", "aba"));
}

#[test]
fn the_last_head_line_decides_and_a_malformed_one_clears_the_head() {
    let good = "0123456789abcdef0123456789abcdef01234567";
    assert_eq!(
        parse_status("l", &format!("HEAD: {good}\nHEAD: 12345\n")).head,
        None
    );
    assert_eq!(
        parse_status("l", &format!("HEAD: 12345\nHEAD: {good}\n"))
            .head
            .as_deref(),
        Some(good)
    );
}
