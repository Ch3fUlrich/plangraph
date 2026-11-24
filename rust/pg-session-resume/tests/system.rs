use pg_session_resume::host::Host;
use pg_session_resume::record::Record;
use pg_session_resume::system::{
    parent_pid, parse_job_state, proc_start_ticks, transcript_path, System,
};
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

fn host(dir: &Path) -> System {
    System {
        log_file: dir.join("log"),
        log_failed: false,
    }
}

#[test]
fn parent_pid_is_field_4() {
    assert_eq!(parent_pid("12 (a (b) c) S 99 5 6"), Some(99));
}

#[test]
fn a_spawned_resume_runs_in_its_own_process_group_and_is_found_as_a_child() {
    let dir = tempfile::tempdir().unwrap();
    let mut h = host(dir.path());
    let pid = h.spawn(dir.path(), &["sleep".into(), "3".into()]).unwrap();
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let pgrp: u32 = stat[stat.rfind(')').unwrap() + 1..]
        .split_whitespace()
        .nth(2)
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        pgrp, pid,
        "its own group, so a group kill of the tick misses it"
    );
    assert_ne!(pgrp, std::os::unix::process::parent_id());
    assert_eq!(
        h.has_children(std::process::id()),
        Some(true),
        "the sleep is our child"
    );
    assert!(h.proc_start(pid).is_some());
}

#[test]
fn a_process_without_children_is_idle() {
    let dir = tempfile::tempdir().unwrap();
    let mut child = std::process::Command::new("sleep")
        .arg("3")
        .spawn()
        .unwrap();
    assert_eq!(host(dir.path()).has_children(child.id()), Some(false));
    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn the_launcher_named_activity_file_is_the_activity_signal() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("opencode-activity");
    std::fs::write(&file, "x").unwrap();
    let past = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000);
    std::fs::File::options()
        .write(true)
        .open(&file)
        .unwrap()
        .set_modified(past)
        .unwrap();
    let rec = Record {
        path: dir.path().join("r.json"),
        value: serde_json::json!({"agent": "opencode", "opencode_session": "S-1", "activity_path": file}),
    };
    assert_eq!(host(dir.path()).activity_ts(&rec), Some(1_000_000));
}

#[test]
fn the_worktree_identity_differs_between_two_checkouts() {
    let dir = tempfile::tempdir().unwrap();
    let h = host(dir.path());
    for n in ["a", "b"] {
        let wt = dir.path().join(n);
        std::fs::create_dir(&wt).unwrap();
        assert!(std::process::Command::new("git")
            .arg("-C")
            .arg(&wt)
            .args(["init", "-q"])
            .status()
            .unwrap()
            .success());
    }
    let (a, b) = (
        h.worktree_id(&dir.path().join("a")),
        h.worktree_id(&dir.path().join("b")),
    );
    assert!(a.is_some() && b.is_some() && a != b);
    assert_eq!(h.worktree_id(&dir.path().join("missing")), None);
}

#[test]
fn the_tick_unit_leaves_resumed_writers_running_when_it_exits() {
    let unit = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../deploy/systemd/pg-session-resume.service");
    let text = std::fs::read_to_string(unit).unwrap();
    assert!(
        text.lines().any(|l| l.trim() == "KillMode=process"),
        "oneshot cleanup would kill the resumed children"
    );
}
