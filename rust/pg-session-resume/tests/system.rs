use pg_session_resume::lock::Lock;
use pg_session_resume::system::{parse_job_state, proc_start_ticks, transcript_path};
use std::path::Path;

#[test]
fn proc_start_is_field_22_even_when_the_command_name_has_spaces_and_parentheses() {
    let stat = "1234 (my (odd) cmd) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 424242 23 24";
    assert_eq!(proc_start_ticks(stat), Some(424242));
    assert_eq!(proc_start_ticks("garbage"), None);
}

#[test]
fn job_state_reads_state_and_running_tasks() {
    let st = parse_job_state(r#"{"state":"done","inFlight":{"tasks":2,"queued":0}}"#).unwrap();
    assert_eq!((st.state.as_str(), st.tasks), ("done", 2));
    assert_eq!(parse_job_state(r#"{"state":"working"}"#).unwrap().tasks, 0);
    assert!(parse_job_state("not json").is_none() && parse_job_state("{}").is_none());
}

#[test]
fn the_transcript_dir_replaces_every_non_alphanumeric_with_a_dash() {
    let base = Path::new("base");
    let p = transcript_path(base, "/a/b/.claude/worktrees/x_y", "sid");
    assert_eq!(
        p,
        base.join(".claude/projects/-a-b--claude-worktrees-x-y/sid.jsonl")
    );
}

#[test]
fn a_second_tick_cannot_take_a_held_lock_and_a_dead_owner_is_replaced() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("lock");
    let first = Lock::acquire(&path).expect("free lock");
    assert!(Lock::acquire(&path).is_none(), "held by this live process");
    drop(first);
    assert!(!path.exists());
    std::fs::write(&path, "4294967290").unwrap(); // no such process
    assert!(Lock::acquire(&path).is_some(), "stale lock taken over");
}
