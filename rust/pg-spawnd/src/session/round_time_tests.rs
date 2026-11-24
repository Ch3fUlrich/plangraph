use super::round_reader::Reader;
use super::round_time::{classify_command, Cause, Class};
use serde_json::{json, Value};

pub(crate) const T0: i64 = 1_700_000_000_000;

pub(crate) fn stamp(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(T0 + ms)
        .unwrap()
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

pub(crate) struct Transcript(pub Vec<Value>);

impl Transcript {
    pub fn new() -> Self {
        Self(Vec::new())
    }
    pub fn prompt(&mut self, at: i64, id: &str) {
        self.0.push(
            json!({"type":"user","timestamp":stamp(at),"promptId":id,"uuid":id,
            "origin":{"kind":"human"},"message":{"role":"user","content":"go"}}),
        );
    }
    pub fn request(&mut self, at: i64) {
        self.0
            .push(json!({"type":"api-request","timestamp":stamp(at)}));
    }
    pub fn text(&mut self, at: i64, end_turn: bool) {
        self.text_in(at, &format!("m{at}"), end_turn);
    }
    pub fn text_in(&mut self, at: i64, message: &str, end_turn: bool) {
        self.0.push(json!({"type":"assistant","timestamp":stamp(at),
            "message":{"id":message,"stop_reason": if end_turn {"end_turn"} else {"tool_use"},
            "content":[{"type":"text","text":"x"}]}}));
    }
    pub fn tool(&mut self, at: i64, id: &str, name: &str, input: Value) {
        self.0.push(json!({"type":"assistant","timestamp":stamp(at),
            "message":{"id":format!("m{at}"),"stop_reason":"tool_use",
            "content":[{"type":"tool_use","id":id,"name":name,"input":input}]}}));
    }
    pub fn result(&mut self, at: i64, id: &str, background: Option<&str>) {
        let mut line = json!({"type":"user","timestamp":stamp(at),
            "message":{"role":"user","content":[{"type":"tool_result","tool_use_id":id,"content":"ok"}]}});
        if let Some(task) = background {
            line["toolUseResult"] = json!({"backgroundTaskId": task});
        }
        self.0.push(line);
    }
    pub fn notified(&mut self, at: i64, task: &str) {
        self.0.push(json!({"type":"user","timestamp":stamp(at),
            "origin":{"kind":"task-notification"},
            "message":{"role":"user","content":format!("<task-notification><task-id>{task}</task-id></task-notification>")}}));
    }
    pub fn read(&self) -> Reader {
        let text: Vec<String> = self.0.iter().map(Value::to_string).collect();
        Reader::read("sess", &text.join("\n"))
    }
}

pub(crate) const MIN: i64 = 60_000;

fn cause(row: &super::round_time::RoundRow, which: Cause) -> i64 {
    row.causes.get(&which).copied().unwrap_or(0)
}

/// A writer that starts the tests in the background and ends its turn waits 20 minutes.
fn background_test_wait() -> Transcript {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        3_000,
        "u1",
        "Bash",
        json!({"command":"cargo test -p x","run_in_background":true}),
    );
    t.result(3_500, "u1", Some("bg1"));
    t.request(4_000);
    t.text(6_000, true);
    t.notified(6_000 + 20 * MIN, "bg1");
    t.request(6_500 + 20 * MIN);
    t.text(8_000 + 20 * MIN, true);
    t
}

#[test]
fn twenty_minute_wait_on_a_background_test_is_booked_once_as_wait_test() {
    let reader = background_test_wait().read();
    let row = &reader.rows[0];
    assert_eq!(cause(row, Cause::WaitTest), 20 * MIN);
    assert_eq!(cause(row, Cause::ModelWait), 2_000 + 2_000 + 1_500);
    assert_eq!(cause(row, Cause::Harness), 1_000 + 500 + 500);
    assert_eq!(row.wall_ms(), row.ended_ms - row.started_ms);
    assert_eq!(row.round_key, "sess:p1");
}

#[test]
fn sleep_loop_over_a_background_task_is_a_wait_on_that_task_class() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":"pytest -q","run_in_background":true}),
    );
    t.result(2_100, "u1", Some("bg1"));
    t.request(2_300);
    t.tool(
        3_000,
        "u2",
        "Bash",
        json!({"command":"until grep -q rc= tasks/bg1.output; do sleep 15; done"}),
    );
    t.result(3_000 + 9 * MIN, "u2", None);
    let reader = t.read();
    assert_eq!(cause(&reader.rows[0], Cause::WaitTest), 9 * MIN);
    assert_eq!(cause(&reader.rows[0], Cause::WaitSleep), 0);
}

#[test]
fn sleep_without_any_background_task_is_wait_sleep() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(500);
    t.tool(1_000, "u1", "Bash", json!({"command":"sleep 30"}));
    t.result(31_000, "u1", None);
    assert_eq!(cause(&t.read().rows[0], Cause::WaitSleep), 30_000);
}

#[test]
fn a_round_of_only_edits_books_no_wait() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(500);
    t.tool(1_500, "u1", "Edit", json!({"file_path":"a.rs"}));
    t.result(1_800, "u1", None);
    t.request(2_000);
    t.text(3_000, true);
    let row = &t.read().rows[0];
    for waiting in [
        Cause::WaitTest,
        Cause::WaitBuild,
        Cause::WaitGate,
        Cause::WaitOther,
        Cause::WaitSleep,
    ] {
        assert_eq!(cause(row, waiting), 0, "{waiting:?}");
    }
    assert_eq!(cause(row, Cause::ToolOther), 300);
    assert_eq!(cause(row, Cause::ModelWait), 1_000 + 1_000);
}

#[test]
fn foreground_tools_book_by_class_and_the_prompt_gap_is_between_rounds() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(100);
    t.tool(200, "u1", "Bash", json!({"command":"cargo build -p x"}));
    t.result(200 + 3 * MIN, "u1", None);
    t.request(300 + 3 * MIN);
    t.tool(
        400 + 3 * MIN,
        "u2",
        "Bash",
        json!({"command":"git push --dry-run origin HEAD"}),
    );
    t.result(400 + 8 * MIN, "u2", None);
    t.text(500 + 8 * MIN, true);
    t.prompt(500 + 8 * MIN + 17 * MIN, "p2");
    let reader = t.read();
    assert_eq!(reader.rows.len(), 2);
    assert_eq!(cause(&reader.rows[0], Cause::Build), 3 * MIN);
    assert_eq!(cause(&reader.rows[0], Cause::Gate), 5 * MIN);
    assert_eq!(cause(&reader.rows[1], Cause::BetweenRounds), 17 * MIN);
    assert_eq!(reader.rows[1].wall_ms(), 0);
}

#[test]
fn overlapping_calls_book_the_gap_once_with_the_wait_first() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(100);
    t.tool(200, "u1", "Bash", json!({"command":"cargo test"}));
    t.tool(300, "u2", "Bash", json!({"command":"sleep 60"}));
    t.result(300 + MIN, "u2", None);
    t.result(300 + 2 * MIN, "u1", None);
    let row = t.read().rows.remove(0);
    assert_eq!(cause(&row, Cause::WaitSleep), MIN);
    assert_eq!(cause(&row, Cause::Test), 100 + MIN);
    assert_eq!(row.wall_ms(), 300 + 2 * MIN);
}

#[test]
fn a_gap_without_evidence_is_unknown() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.text(40_000, true);
    assert_eq!(cause(&t.read().rows[0], Cause::Unknown), 40_000);
}

/// The writer started `cargo test` in the background and then waits on its output file.
pub(crate) fn explicit_wait(task: &str, wait_call: &str) -> Transcript {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.tool(
        2_000,
        "u1",
        "Bash",
        json!({"command":"cargo test -p x","run_in_background":true}),
    );
    t.result(2_500, "u1", Some(task));
    t.request(3_000);
    t.tool(4_000, "u2", "Bash", json!({"command": wait_call}));
    t
}

#[test]
fn open_wait_names_the_task_and_counts_the_open_tail() {
    let call = "until grep -q rc= /t/tasks/bg1.output; do sleep 15; done";
    let mut t = explicit_wait("bg1", call);
    let wait = t.read().open_wait(T0 + 4_000 + 16 * MIN).expect("blocked");
    assert_eq!(wait.task_id, "bg1");
    assert_eq!(wait.class, Class::Test);
    assert_eq!(wait.command, "cargo test -p x");
    assert_eq!(wait.waited_ms, 16 * MIN);
    // The wait call returns, or the command is reported finished: the wait is closed.
    t.result(4_000 + 16 * MIN, "u2", None);
    assert_eq!(t.read().open_wait(T0 + 30 * MIN), None);
    let mut done = explicit_wait("bg1", call);
    done.notified(4_000 + 16 * MIN, "bg1");
    done.result(4_000 + 16 * MIN, "u2", None);
    assert_eq!(done.read().open_wait(T0 + 30 * MIN), None);
}

#[test]
fn an_idle_writer_or_an_unnamed_sleep_is_no_wait_to_stop() {
    let t = background_test_wait();
    let idle = Transcript(t.0[..6].to_vec()).read();
    assert_eq!(idle.open_wait(T0 + 60 * MIN), None, "idle after end_turn");
    assert_eq!(
        idle.running().len(),
        1,
        "the command is still known for its owner record"
    );
    let unnamed = explicit_wait("bg1", "sleep 900").read();
    assert_eq!(unnamed.open_wait(T0 + 60 * MIN), None, "names no task");
    let other = explicit_wait(
        "bg1",
        "until grep -q rc= /t/tasks/zzzz.output; do sleep 5; done",
    );
    assert_eq!(
        other.read().open_wait(T0 + 60 * MIN),
        None,
        "names another task"
    );
}

#[test]
fn a_new_round_closes_the_wait() {
    let mut t = explicit_wait(
        "bg1",
        "until grep -q rc= /t/tasks/bg1.output; do sleep 15; done",
    );
    t.prompt(5 * MIN, "p2");
    assert_eq!(t.read().open_wait(T0 + 60 * MIN), None);
}

#[test]
fn writing_is_one_message_and_a_new_message_without_a_request_is_unknown() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.text_in(2_000, "m1", false);
    t.text_in(5_000, "m1", false);
    t.text_in(9_000, "m2", true);
    let row = &t.read().rows[0];
    assert_eq!(cause(row, Cause::ModelWrite), 3_000);
    assert_eq!(cause(row, Cause::Unknown), 4_000);
    assert_eq!(cause(row, Cause::ModelWait), 1_000);
}

#[test]
fn a_writer_that_is_working_is_not_waiting() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(500);
    t.tool(
        1_000,
        "u1",
        "Bash",
        json!({"command":"cargo test","run_in_background":true}),
    );
    t.result(1_100, "u1", Some("bg1"));
    t.request(1_200);
    t.tool(2_000, "u2", "Edit", json!({"file_path":"a.rs"}));
    assert_eq!(t.read().open_wait(T0 + 60 * MIN), None);
}

#[test]
fn commands_are_classified() {
    assert_eq!(classify_command("cd x && cargo test -p a"), Class::Test);
    assert_eq!(
        classify_command("python3 tools/preflight.py --lane"),
        Class::Gate
    );
    assert_eq!(classify_command("cargo clippy"), Class::Build);
    assert_eq!(classify_command("until ls; do sleep 5; done"), Class::Wait);
    assert_eq!(classify_command("git status"), Class::Other);
}
