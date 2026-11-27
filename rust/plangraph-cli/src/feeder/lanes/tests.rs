use super::*;
use std::fs::File;

fn session(dir: &Path, name: &str, extra: Value) {
    let mut rec = json!({"name":"writer","session_name":name,"host":"workstation","ts":1000});
    for (k, v) in extra.as_object().unwrap() {
        rec[k] = v.clone();
    }
    fs::write(dir.join(format!("{name}.json")), rec.to_string()).unwrap();
}
fn job(dir: &Path, job: &str, state: &str) -> PathBuf {
    fs::create_dir_all(dir.join(job)).unwrap();
    let file = dir.join(job).join("state.json");
    fs::write(&file, json!({"state":state}).to_string()).unwrap();
    file
}
fn age(file: &Path, secs: u64) {
    let at = SystemTime::now() - Duration::from_secs(secs);
    File::options()
        .write(true)
        .open(file)
        .unwrap()
        .set_modified(at)
        .unwrap();
}
fn ids(rows: &[Value]) -> Vec<&str> {
    rows.iter().map(|r| r["run_id"].as_str().unwrap()).collect()
}
fn evidence<'a>(
    sessions: &'a Path,
    jobs: &'a Path,
    listed: &'a BTreeSet<String>,
    roster: &'a dyn Fn() -> Option<Vec<Value>>,
    pid_alive: &'a dyn Fn(u64) -> bool,
) -> Evidence<'a> {
    Evidence {
        sessions,
        jobs,
        now: SystemTime::now(),
        listed,
        roster,
        pid_alive,
    }
}

#[test]
fn live_writer_sessions_become_runs_and_dead_or_other_ones_do_not() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    session(
        &sessions,
        "writer-1",
        json!({"job_id":"aaa","card":"plangraph:ONE batch7"}),
    );
    session(
        &sessions,
        "writer-2",
        json!({"job_id":"bbb","card":"lane-name-only"}),
    );
    session(
        &sessions,
        "writer-3",
        json!({"job_id":"ccc","card":"plangraph:DONE"}),
    );
    session(
        &sessions,
        "writer-4",
        json!({"job_id":"ddd","card":"plangraph:STOPPED"}),
    );
    session(
        &sessions,
        "writer-7",
        json!({"job_id":"missing","card":"plangraph:NOJOB"}),
    );
    session(
        &sessions,
        "hostprobe-6",
        json!({"name":"hostprobe","job_id":"fff"}),
    );
    job(&jobs, "aaa", "working");
    job(&jobs, "bbb", "working");
    job(&jobs, "ccc", "done");
    job(&jobs, "ddd", "stopped");
    job(&jobs, "fff", "working");
    fs::write(sessions.join("junk.json"), "not json").unwrap();
    let none = BTreeSet::new();
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|_| false));
    assert_eq!(ids(&rows), ["lane:writer-1", "lane:writer-2"]);
    assert_eq!(rows[0]["item_key"], "plangraph:ONE");
    assert_eq!(rows[0]["host"], "workstation");
    assert_eq!(rows[1]["item_key"], "");
}

#[test]
fn an_old_state_file_keeps_its_slot_while_the_roster_lists_the_session() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    for (n, sid) in [("writer-1", "s1"), ("writer-2", "s2"), ("writer-3", "s3")] {
        session(
            &sessions,
            n,
            json!({"job_id":format!("j{n}"),"session_id":sid}),
        );
        age(&job(&jobs, &format!("j{n}"), "working"), 7 * 3600);
    }
    let none = BTreeSet::new();
    let roster = || {
        Some(vec![
            json!({"sessionId":"s1","state":"working"}),
            json!({"sessionId":"s2","state":"done"}),
        ])
    };
    // Listed and working stays; listed done and absent from the roster go.
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &roster, &|_| false));
    assert_eq!(ids(&rows), ["lane:writer-1"]);
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|_| false));
    assert!(rows.is_empty());
}

#[test]
fn a_fresh_but_malformed_or_missing_state_file_is_not_proof_of_life() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    session(
        &sessions,
        "writer-1",
        json!({"session_id":"s1","job_id":"j1","pid":4343}),
    );
    session(
        &sessions,
        "writer-2",
        json!({"session_id":"s2","job_id":"j2","pid":4343}),
    );
    session(
        &sessions,
        "writer-3",
        json!({"session_id":"s3","job_id":"j3","pid":4242}),
    );
    // j1: just written but not JSON. j2: no state file at all. j3: malformed, live pid.
    fs::create_dir_all(jobs.join("j1")).unwrap();
    fs::write(jobs.join("j1/state.json"), "{not json").unwrap();
    fs::create_dir_all(jobs.join("j3")).unwrap();
    fs::write(jobs.join("j3/state.json"), "").unwrap();
    let none = BTreeSet::new();
    // Dead pid, empty roster: nothing keeps writers 1 and 2; the live pid keeps writer 3.
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| Some(vec![]), &|p| {
        p == 4242
    }));
    assert_eq!(ids(&rows), ["lane:writer-3"]);
}

#[test]
fn roster_and_pid_evidence_each_keep_a_slot_on_their_own() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    // 1: roster lists it, no state file, no pid. 2: live pid, old state, roster omits it.
    // 3: nothing lists it. 4: PID and roster keep it despite a terminal job file.
    session(
        &sessions,
        "writer-1",
        json!({"session_id":"s1","job_id":"j1"}),
    );
    session(
        &sessions,
        "writer-2",
        json!({"session_id":"s2","job_id":"j2","pid":4242}),
    );
    session(
        &sessions,
        "writer-3",
        json!({"session_id":"s3","job_id":"j3"}),
    );
    session(
        &sessions,
        "writer-4",
        json!({"session_id":"s4","job_id":"j4","pid":4242}),
    );
    age(&job(&jobs, "j2", "working"), 7 * 3600);
    age(&job(&jobs, "j3", "working"), 7 * 3600);
    job(&jobs, "j4", "done");
    let none = BTreeSet::new();
    let roster = || {
        Some(vec![
            json!({"sessionId":"s1","state":"working"}),
            json!({"sessionId":"s4","state":"working"}),
        ])
    };
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &roster, &|p| p == 4242));
    assert_eq!(
        ids(&rows),
        ["lane:writer-1", "lane:writer-2", "lane:writer-4"]
    );
}

#[test]
fn a_roster_larger_than_a_pipe_buffer_is_read_whole() {
    let d = tempfile::tempdir().unwrap();
    let big: Vec<Value> = (0..4000)
        .map(|n| json!({"sessionId":format!("session-{n:06}-padding-padding"),"state":"working"}))
        .collect();
    let file = d.path().join("roster.json");
    fs::write(&file, serde_json::to_vec(&big).unwrap()).unwrap();
    assert!(fs::metadata(&file).unwrap().len() > 131_072);
    let roster = roster_of(Command::new("cat").arg(&file)).unwrap();
    assert_eq!(roster.len(), 4000);
    assert!(roster_of(&mut Command::new("false")).is_none());
}

#[test]
fn a_session_with_only_a_pid_lives_while_that_pid_does() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    session(
        &sessions,
        "writer-1",
        json!({"pid":4242,"card":"plangraph:PIDLANE"}),
    );
    session(&sessions, "writer-2", json!({"pid":4343}));
    session(&sessions, "writer-3", json!({}));
    let none = BTreeSet::new();
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &none, &|| None, &|p| p == 4242));
    assert_eq!(ids(&rows), ["lane:writer-1"]);
    assert_eq!(rows[0]["item_key"], "plangraph:PIDLANE");
    #[cfg(target_os = "linux")]
    assert!(pid_alive(u64::from(std::process::id())));
}

#[test]
fn only_whole_tokens_that_are_item_keys_are_read_from_a_card() {
    assert_eq!(
        item_keys("lane plangraph:A, plangraph:B-2 plangraph: xplangraph:C plangraph:D;rm"),
        ["plangraph:A", "plangraph:B-2"]
    );
    assert!(item_keys("").is_empty());
}

#[test]
fn lane_cards_map_the_launcher_run_id_to_the_parsed_keys() {
    let d = tempfile::tempdir().unwrap();
    session(
        d.path(),
        "writer-1",
        json!({"run_id":"run-1","card":"lane plangraph:A, plangraph:B"}),
    );
    session(d.path(), "writer-2", json!({"card":"plangraph:C"}));
    session(
        d.path(),
        "review-3",
        json!({"name":"review","run_id":"run-3","card":"plangraph:D"}),
    );
    let cards = lane_cards(d.path());
    assert_eq!(cards.len(), 1);
    assert_eq!(cards["run-1"], ["plangraph:A", "plangraph:B"]);
}

#[test]
fn a_session_the_spawner_lists_is_not_counted_twice() {
    let d = tempfile::tempdir().unwrap();
    let (sessions, jobs) = (d.path().join("s"), d.path().join("j"));
    fs::create_dir_all(&sessions).unwrap();
    session(
        &sessions,
        "writer-1",
        json!({"job_id":"aaa","run_id":"run-1"}),
    );
    session(
        &sessions,
        "writer-2",
        json!({"job_id":"bbb","run_id":"run-2"}),
    );
    job(&jobs, "aaa", "working");
    job(&jobs, "bbb", "working");
    let listed = BTreeSet::from(["run-1".to_string()]);
    let rows = live_lane_runs(&evidence(&sessions, &jobs, &listed, &|| None, &|_| false));
    assert_eq!(ids(&rows), ["run-2"]);
}

#[test]
fn the_launcher_state_directory_and_claude_config_directory_are_honoured() {
    let home = Path::new("fixture-home");
    let mut env = Env::new();
    assert_eq!(
        sessions_dir(home, &env),
        Path::new("fixture-home/.local/state/routing/launch/sessions")
    );
    assert_eq!(jobs_dir(home, &env), Path::new("fixture-home/.claude/jobs"));
    env.insert("ROUTING_STATE_DIR".into(), "/srv/routing".into());
    env.insert("CLAUDE_CONFIG_DIR".into(), "/srv/claude".into());
    assert_eq!(
        sessions_dir(home, &env),
        Path::new("/srv/routing/launch/sessions")
    );
    assert_eq!(jobs_dir(home, &env), Path::new("/srv/claude/jobs"));
}
