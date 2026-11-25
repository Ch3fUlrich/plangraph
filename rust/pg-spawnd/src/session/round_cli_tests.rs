use super::{
    round_cli::{run, transcript_path},
    round_host::LocalHost,
    round_store,
    round_time_tests::{Transcript, MIN, T0},
    round_watch::{blocked_outcome, state_file, Host},
};
use serde_json::{json, Value};
use std::{fs, path::Path};

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

pub(super) struct World {
    pub(super) _dir: tempfile::TempDir,
    pub(super) state: std::path::PathBuf,
    pub(super) routing: std::path::PathBuf,
    pub(super) home: std::path::PathBuf,
    /// The epoch millisecond that the relative times of its transcripts and passes count from.
    pub(super) origin: i64,
}

impl World {
    pub(super) fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let world = Self {
            state: root.join("spawn"),
            routing: root.join("routing"),
            home: root.join("home"),
            origin: T0,
            _dir: dir,
        };
        fs::create_dir_all(world.routing.join("launch/sessions")).unwrap();
        world
    }

    /// A launched session: its per-session snapshot and, unless `mirror`, its transcript.
    pub(super) fn session(&self, name: &str, id: &str, cwd: &str, transcript: Option<&Transcript>) {
        self.session_as("writer", name, id, cwd, transcript);
    }

    pub(super) fn session_as(
        &self,
        role: &str,
        name: &str,
        id: &str,
        cwd: &str,
        transcript: Option<&Transcript>,
    ) {
        let spec = json!({"name": role, "session_id": id, "cwd": cwd, "session_name": name});
        fs::write(
            self.routing.join(format!("launch/sessions/{name}.json")),
            spec.to_string(),
        )
        .unwrap();
        if let Some(t) = transcript {
            let path = transcript_path(&self.home, cwd, id);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let lines: Vec<String> = t.0.iter().map(Value::to_string).collect();
            fs::write(path, lines.join("\n")).unwrap();
        }
    }

    /// A world whose times are real: processes started now must fall into launch windows.
    pub(super) fn live() -> Self {
        Self {
            origin: chrono::Utc::now().timestamp_millis(),
            ..Self::new()
        }
    }

    pub(super) fn run(&self, at: i64) -> Vec<Value> {
        run(
            &self.state,
            &self.routing,
            &self.home,
            "ws",
            self.origin + at,
            true,
        )
    }

    pub(super) fn rows(&self) -> Vec<Value> {
        round_store::read(&self.state.join("round-telemetry/rows.jsonl"))
    }
}

#[test]
fn a_pass_books_one_row_per_round_and_a_second_pass_rewrites_it() {
    let world = World::new();
    world.session("writer-1", "abc", "/w/x.y", Some(&one_round()));
    let path = transcript_path(&world.home, "/w/x.y", "abc");
    assert!(path.ends_with(".claude/projects/-w-x-y/abc.jsonl"));
    assert!(world
        .run(20 * MIN)
        .iter()
        .all(|l| l["rule"] != "round-telemetry-error"));
    world.run(20 * MIN);
    let rows = world.rows();
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
fn underscores_in_the_working_directory_map_to_hyphens_like_the_session_store() {
    let world = World::new();
    world.session("writer-1", "abc", "/srv/u/my_proj", Some(&one_round()));
    assert!(transcript_path(&world.home, "/srv/u/my_proj", "abc")
        .to_string_lossy()
        .contains("/-srv-u-my-proj/"));
    world.run(MIN);
    assert_eq!(world.rows().len(), 1);
}

#[test]
fn a_replacement_launch_does_not_hide_the_session_it_replaced() {
    let world = World::new();
    world.session("writer-1", "abc", "/w/a", Some(&one_round()));
    world.session("writer-2", "def", "/w/a", Some(&one_round()));
    world.run(MIN);
    let mut keys: Vec<String> = world
        .rows()
        .iter()
        .map(|r| r["round_key"].to_string())
        .collect();
    keys.sort();
    assert_eq!(keys, ["\"writer-1:p1\"", "\"writer-2:p1\""]);
}

#[test]
fn a_record_mirrored_from_another_host_books_nothing_and_stops_nothing() {
    let world = World::new();
    world.session("writer-9", "zzz", "/w/elsewhere", None);
    assert!(world.run(60 * MIN).is_empty());
    assert!(world.rows().is_empty());
    assert!(!world.state.join("round-watch").exists());
}

#[test]
fn digest_gives_the_median_and_the_shares_per_host_with_between_rounds() {
    let row = |host: &str, wall: i64, wait: i64, between: i64| {
        json!({"round_key": format!("{host}{wall}"), "host": host, "wall_ms": wall,
            "causes": {"wait_test": wait, "test": wall - wait, "between_rounds": between}})
    };
    let rows = [
        row("a", 10 * MIN, 5 * MIN, 0),
        row("a", 30 * MIN, 15 * MIN, 0),
        row("a", 20 * MIN, 10 * MIN, 20 * MIN),
        row("b", 4 * MIN, 0, 0),
    ];
    let digest = round_store::digest(&rows);
    assert_eq!(digest[0]["median_round_min"], 20.0);
    assert_eq!(digest[0]["share_pct"]["wait_test"], 30.0 / 80.0 * 100.0);
    assert_eq!(digest[0]["share_pct"]["between_rounds"], 25.0);
    assert_eq!(digest[1]["rounds"], 1);
}

/// A command shell as the harness starts it: its own process group, the session id in
/// its environment, and its output going to `.../tasks/<task id>.output`.
pub(super) fn command_shell(
    root: &Path,
    session_id: &str,
    task: &str,
    command: &str,
) -> std::process::Child {
    use std::os::unix::process::CommandExt;
    let tasks = root.join("tasks");
    fs::create_dir_all(&tasks).unwrap();
    let output = fs::File::create(tasks.join(format!("{task}.output"))).unwrap();
    let mut shell = std::process::Command::new("/bin/sh");
    shell
        .args(["-c", &format!("{command}; true")])
        .env("CLAUDE_CODE_SESSION_ID", session_id)
        .stdout(output.try_clone().unwrap())
        .stderr(output)
        .process_group(0);
    shell.spawn().unwrap()
}

/// The start of a background task as the transcript records it, with the real output path.
fn launch(t: &mut Transcript, root: &Path, at: i64, call: &str, task: &str, command: &str) {
    t.request(at - 500);
    t.tool(
        at,
        call,
        "Bash",
        json!({"command": command, "run_in_background": true}),
    );
    t.result(at + 500, call, Some(task));
    let result = t.0.last_mut().unwrap();
    let path = root.join("tasks").join(format!("{task}.output"));
    result["message"]["content"][0]["content"] = json!(format!(
        "Command running in background with ID: {task}. Output is being written to: {}. You will be notified",
        path.display()
    ));
}

fn wait_call(t: &mut Transcript, at: i64, id: &str, task: &str) {
    t.request(at - 500);
    t.tool(
        at,
        id,
        "TaskOutput",
        json!({"task_id": task, "block": true}),
    );
}

fn rules(lines: Vec<Value>) -> Vec<String> {
    lines
        .iter()
        .map(|l| l["rule"].as_str().unwrap().to_owned())
        .collect()
}

fn stub_claude(root: &Path) -> std::path::PathBuf {
    let log = root.join("stop.log");
    pg_worker_core::executable_test_support::write_script(
        root,
        "claude-stub",
        &format!(
            "#!/bin/sh\n[ \"$1\" = stop ] && echo \"$@\" >> {}\nexit 0\n",
            log.display()
        ),
    )
}

#[test]
fn the_scheduled_pass_ends_one_task_blocks_the_second_wait_and_ends_the_session_after_delivery() {
    let world = World::live();
    let id = "sess-e2e-0001";
    let root = world.state.parent().unwrap().to_path_buf();
    std::env::set_var("PLANGRAPH_CLAUDE_BIN", stub_claude(&root));
    let stop_log = root.join("stop.log");

    let secret_command = "sleep 301 # DEPLOY_TOKEN=hunter2hunter2hunter2";
    let mut first = command_shell(&root, id, "bgA", secret_command);
    // A second shell of the same session runs the very same command text for another task.
    let mut twin = command_shell(&root, id, "bgZ", "sleep 301");
    let mut t = Transcript::at(world.origin);
    t.prompt(0, "p1");
    // Both tasks are launched at the start, as their processes are; the writer waits on
    // bgA first and on bgB when bgA is reported ended.
    let mut second = command_shell(&root, id, "bgB", "sleep 302");
    launch(&mut t, &root, 2_000, "u1", "bgA", secret_command);
    launch(&mut t, &root, 3_000, "u2", "bgB", "sleep 302");
    wait_call(&mut t, 4_000, "w1", "bgA");
    world.session("writer-5", id, "/w/e2e", Some(&t));

    let task = |name: &str| {
        let path = root.join("tasks").join(format!("{name}.output"));
        super::round_wait::TaskRef {
            id: name.into(),
            output_path: Some(path.display().to_string()),
            launched_from_ms: world.origin,
            launched_to_ms: world.origin + 5_000,
        }
    };
    let probe = LocalHost::new(world.state.clone(), id.into());
    assert_eq!(
        probe.resolve_owner("writer-5", &task("bgA")).map(|o| o.pid),
        Some(first.id())
    );
    assert_eq!(
        probe.resolve_owner("writer-5", &task("bgZ")).map(|o| o.pid),
        Some(twin.id())
    );
    let mut elsewhere = task("bgA");
    elsewhere.output_path = Some("/tmp/other/tasks/bgA.output".into());
    assert_eq!(
        probe.resolve_owner("writer-5", &elsewhere),
        None,
        "the full path must match"
    );
    let mut late = task("bgA");
    late.launched_from_ms = world.origin - 3_600_000;
    late.launched_to_ms = world.origin - 3_500_000;
    assert_eq!(
        probe.resolve_owner("writer-5", &late),
        None,
        "started outside the launch window"
    );

    assert!(world.run(MIN).is_empty(), "first pass records the owner");
    let stopped = world.run(16 * MIN);
    assert!(
        !serde_json::to_string(&stopped).unwrap().contains("hunter2"),
        "nor the watchdog log: {stopped:?}"
    );
    assert_eq!(
        rules(stopped),
        ["round-wait-stopped", "round-wait-notified"]
    );
    assert!(first.wait().is_ok(), "the owner of bgA is gone");
    assert!(
        twin.try_wait().unwrap().is_none(),
        "the twin with the same command text lives"
    );
    assert!(
        world.run(17 * MIN).is_empty(),
        "no second stop, no second notice"
    );
    let outbox: Vec<_> = fs::read_dir(world.state.join("watchdog/outbox"))
        .unwrap()
        .collect();
    assert_eq!(outbox.len(), 1);
    let event = read_json(&outbox[0].as_ref().unwrap().path());
    assert_eq!(event["recipient"], "writer-5");
    assert!(event["text"].as_str().unwrap().contains("sleep 301"));
    assert!(
        !event["text"].as_str().unwrap().contains("hunter2"),
        "credentials in the command text never reach the outbox: {event}"
    );
    // The bridge delivers: the outbox file goes away, the sent marker stays.
    fs::remove_file(outbox[0].as_ref().unwrap().path()).unwrap();
    assert!(world.run(18 * MIN).is_empty());
    assert_eq!(
        fs::read_dir(world.state.join("watchdog/outbox"))
            .unwrap()
            .count(),
        0,
        "not queued again"
    );

    // The second wait of the round closes it as BLOCKED, with the command line.
    t.notified(19 * MIN, "bgA");
    t.result(19 * MIN, "w1", None);
    wait_call(&mut t, 19 * MIN + 3_000, "w2", "bgB");
    world.session("writer-5", id, "/w/e2e", Some(&t));
    world.run(20 * MIN);
    let fired = rules(world.run(36 * MIN));
    assert!(fired.contains(&"round-blocked".to_owned()), "{fired:?}");
    assert!(
        !fired.contains(&"round-session-ended".to_owned()),
        "its notices are still queued: {fired:?}"
    );
    assert!(second.wait().is_ok());
    assert!(
        !stop_log.exists(),
        "the writer is not stopped before it can read why"
    );
    let blocked = blocked_outcome(&state_file(&world.state, "writer-5")).unwrap();
    assert_eq!(blocked["command"], "sleep 302");
    assert_eq!(blocked["closed_ms"], world.origin + 36 * MIN);
    // The bridge flushes the BLOCKED notices; the next pass ends the session once.
    for entry in fs::read_dir(world.state.join("watchdog/outbox")).unwrap() {
        fs::remove_file(entry.unwrap().path()).unwrap();
    }
    assert_eq!(rules(world.run(37 * MIN)), ["round-session-ended"]);
    assert_eq!(fs::read_to_string(&stop_log).unwrap(), "stop sess-e2e\n");

    // The writer keeps talking after the close: none of it is booked into the round, and
    // the silent wait up to the close is.
    t.request(40 * MIN);
    t.text(41 * MIN, true);
    world.session("writer-5", id, "/w/e2e", Some(&t));
    assert!(world.run(42 * MIN).is_empty());
    let row = &world.rows()[0];
    assert_eq!(row["outcome"], "BLOCKED");
    assert_eq!(row["closed_ms"], world.origin + 36 * MIN);
    assert_eq!(row["ended_ms"], world.origin + 36 * MIN);
    assert!(
        row["causes"]["wait_other"].as_i64().unwrap() > 15 * MIN,
        "the open wait is booked up to the close: {row}"
    );
    assert_eq!(
        fs::read_to_string(&stop_log).unwrap(),
        "stop sess-e2e\n",
        "ended once"
    );
    twin.kill().unwrap();
    let _ = twin.wait();
}

#[test]
fn a_review_or_planning_session_with_an_open_wait_is_not_measured_or_stopped() {
    let world = World::live();
    let root = world.state.parent().unwrap().to_path_buf();
    let mut shell = command_shell(&root, "sess-rev", "bgR", "sleep 303");
    let mut t = Transcript::at(world.origin);
    t.prompt(0, "p1");
    launch(&mut t, &root, 2_000, "u1", "bgR", "sleep 303");
    wait_call(&mut t, 4_000, "w1", "bgR");
    world.session_as("review", "review-3", "sess-rev", "/w/rev", Some(&t));
    assert!(world.run(MIN).is_empty());
    assert!(world.run(60 * MIN).is_empty());
    assert!(
        shell.try_wait().unwrap().is_none(),
        "the review's command is untouched"
    );
    assert!(world.rows().is_empty());
    shell.kill().unwrap();
    let _ = shell.wait();
}

#[test]
fn a_native_run_record_books_one_row_from_its_stage_intervals() {
    let world = World::new();
    let dir = world.state.join("runs/run-9");
    fs::create_dir_all(&dir).unwrap();
    let stage = |name: &str, from: i64, to: i64| json!({"stage": name, "started_at_ms": from, "ended_at_ms": to});
    fs::write(
        dir.join("record.json"),
        json!({"run_id": "run-9", "item_key": "k", "stage_timings": [
            stage("writing", 1_000, 61_000), stage("final_gates", 61_000, 91_000)]})
        .to_string(),
    )
    .unwrap();
    world.run(MIN);
    world.run(MIN);
    let rows = world.rows();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["round_key"], "run-9:native:1000");
    assert_eq!(rows[0]["causes"]["native_turn"], 60_000);
    assert_eq!(rows[0]["causes"]["gate"], 30_000);
}

fn read_json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}
