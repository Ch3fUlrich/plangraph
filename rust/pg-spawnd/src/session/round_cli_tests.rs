use super::{
    round_cli::{run, transcript_path},
    round_host::cmdline_runs,
    round_store,
    round_time_tests::{Transcript, MIN, T0},
};
use serde_json::json;
use std::fs;

fn one_round() -> Transcript {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":"cargo test","run_in_background":true}),
    );
    t.result(2_500, "u1", Some("bg1"));
    t.request(3_000);
    t.text(4_000, true);
    t.notified(10 * MIN, "bg1");
    t.request(10 * MIN + 500);
    t.text(10 * MIN + 1_500, true);
    t
}

#[test]
fn a_pass_books_one_row_per_round_and_a_second_pass_rewrites_it() {
    let dir = tempfile::tempdir().unwrap();
    let (state, home) = (dir.path().join("state"), dir.path().join("home"));
    fs::create_dir_all(state.join("launch")).unwrap();
    fs::write(
        state.join("launch/writer.json"),
        json!({"session_id":"abc","cwd":"/w/x.y","session_name":"writer-1"}).to_string(),
    )
    .unwrap();
    let path = transcript_path(&home, "/w/x.y", "abc");
    assert!(path.ends_with(".claude/projects/-w-x-y/abc.jsonl"));
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let lines: Vec<String> = one_round().0.iter().map(|v| v.to_string()).collect();
    fs::write(&path, lines.join("\n")).unwrap();
    // The pass happens 20 minutes after the writer went idle with no command alive.
    assert!(run(&state, &home, "ws", T0 + 20 * MIN)
        .iter()
        .all(|l| l["rule"] != "round-telemetry-error"));
    run(&state, &home, "ws", T0 + 20 * MIN);
    let rows = round_store::read(&state.join("round-telemetry/rows.jsonl"));
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["round_key"], "writer-1:p1");
    assert_eq!(rows[0]["causes"]["wait_test"], 10 * MIN - 4_000);
    let digest = round_store::digest(&rows);
    assert_eq!(digest[0]["host"], "ws");
    let line = round_store::digest_line(&digest[0]);
    assert!(
        line.starts_with("ws: 1 rounds, median 10.0 min; wait_test "),
        "{line}"
    );
}

#[test]
fn digest_gives_the_median_and_the_shares_per_host() {
    let row = |host: &str, wall: i64, wait: i64| {
        json!({"round_key": format!("{host}{wall}"), "host": host, "wall_ms": wall,
            "causes": {"wait_test": wait, "test": wall - wait}})
    };
    let rows = [
        row("a", 10 * MIN, 5 * MIN),
        row("a", 30 * MIN, 15 * MIN),
        row("a", 20 * MIN, 10 * MIN),
        row("b", 4 * MIN, 0),
    ];
    let digest = round_store::digest(&rows);
    assert_eq!(digest[0]["median_round_min"], 20.0);
    assert_eq!(digest[0]["share_pct"]["wait_test"], 50.0);
    assert_eq!(digest[1]["rounds"], 1);
}

#[test]
fn an_owner_is_matched_by_the_command_text_in_argv() {
    let argv = b"/bin/zsh\0-c\0eval 'cargo  test -p x' < /dev/null\0";
    assert!(cmdline_runs(argv, "cargo test -p x"));
    assert!(!cmdline_runs(argv, "cargo test -p y"));
    assert!(!cmdline_runs(argv, "  "));
}
