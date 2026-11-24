use super::{
    round_cli::{run, transcript_path},
    round_host::{cmdline_runs, LocalHost},
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

struct World {
    _dir: tempfile::TempDir,
    state: std::path::PathBuf,
    routing: std::path::PathBuf,
    home: std::path::PathBuf,
}

impl World {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let world = Self {
            state: root.join("spawn"),
            routing: root.join("routing"),
            home: root.join("home"),
            _dir: dir,
        };
        fs::create_dir_all(world.routing.join("launch/sessions")).unwrap();
        world
    }

    /// A launched session: its per-session snapshot and, unless `mirror`, its transcript.
    fn session(&self, name: &str, id: &str, cwd: &str, transcript: Option<&Transcript>) {
        let spec = json!({"session_id": id, "cwd": cwd, "session_name": name});
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

    fn run(&self, at: i64) -> Vec<Value> {
        run(&self.state, &self.routing, &self.home, "ws", T0 + at)
    }

    fn rows(&self) -> Vec<Value> {
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

#[test]
fn an_owner_is_matched_by_the_command_text_in_argv() {
    let argv = b"/bin/zsh\0-c\0eval 'cargo  test -p x' < /dev/null\0";
    assert!(cmdline_runs(argv, "cargo test -p x"));
    assert!(!cmdline_runs(argv, "cargo test -p y"));
    assert!(!cmdline_runs(argv, "  "));
}

/// A command shell as the harness starts it: its own process group, the session id in
/// its environment, the command text in its argv.
fn command_shell(session_id: &str, command: &str) -> std::process::Child {
    use std::os::unix::process::CommandExt;
    let mut shell = std::process::Command::new("/bin/sh");
    shell
        .args(["-c", &format!("{command}; true")])
        .env("CLAUDE_CODE_SESSION_ID", session_id)
        .process_group(0);
    shell.spawn().unwrap()
}

fn read_json(path: &Path) -> Value {
    serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

#[test]
fn the_scheduled_pass_ends_the_recorded_command_once_and_blocks_the_second_wait() {
    let world = World::new();
    let id = "sess-e2e-0001";
    let command = "sleep 301";
    let mut shell = command_shell(id, command);
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":command,"run_in_background":true}),
    );
    t.result(2_500, "u1", Some("bgA"));
    t.request(3_000);
    t.tool(
        4_000,
        "w1",
        "Bash",
        json!({"command":"until grep -q rc= /t/tasks/bgA.output; do sleep 15; done"}),
    );
    world.session("writer-5", id, "/w/e2e", Some(&t));
    let owner_probe =
        LocalHost::new(world.state.clone(), id.into()).resolve_owner("writer-5", command);
    assert_eq!(
        owner_probe.map(|o| o.pid),
        Some(shell.id()),
        "the shell is the one owner"
    );

    assert!(world.run(MIN).is_empty(), "first pass records the owner");
    let rules = |lines: Vec<Value>| -> Vec<String> {
        lines
            .iter()
            .map(|l| l["rule"].as_str().unwrap().to_owned())
            .collect()
    };
    assert_eq!(
        rules(world.run(16 * MIN)),
        ["round-wait-stopped", "round-wait-notified"]
    );
    assert!(shell.wait().is_ok(), "the owner is gone");
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
    assert!(event["text"].as_str().unwrap().contains(command));

    // The second wait of the round is closed as BLOCKED, with the command line.
    let mut second = command_shell(id, "sleep 302");
    t.notified(17 * MIN, "bgA");
    t.result(17 * MIN, "w1", None);
    t.request(17 * MIN + 500);
    t.tool(
        17 * MIN + 1_000,
        "u2",
        "Bash",
        json!({"command":"sleep 302","run_in_background":true}),
    );
    t.result(17 * MIN + 1_500, "u2", Some("bgB"));
    t.request(17 * MIN + 2_000);
    t.tool(
        17 * MIN + 3_000,
        "w2",
        "Bash",
        json!({"command":"until grep -q rc= /t/tasks/bgB.output; do sleep 15; done"}),
    );
    world.session("writer-5", id, "/w/e2e", Some(&t));
    world.run(18 * MIN);
    let fired = rules(world.run(34 * MIN));
    assert!(fired.contains(&"round-blocked".to_owned()), "{fired:?}");
    assert!(second.wait().is_ok());
    let blocked = blocked_outcome(&state_file(&world.state, "writer-5")).unwrap();
    assert_eq!(blocked["command"], "sleep 302");
    assert_eq!(world.rows()[0]["outcome"], "BLOCKED");
}
