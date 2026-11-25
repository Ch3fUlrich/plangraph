use super::round_reader::Reader;
use super::round_time::{classify_command, Cause, Class};
use serde_json::{json, Value};

pub(crate) const T0: i64 = 1_700_000_000_000;

pub(crate) fn stamp_from(origin: i64, ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(origin + ms)
        .unwrap()
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// Lines of a transcript and the epoch millisecond its relative times count from.
pub(crate) struct Transcript(pub Vec<Value>, pub i64);

impl Transcript {
    pub fn new() -> Self {
        Self(Vec::new(), T0)
    }
    /// A transcript whose times count from `origin` (processes started now need real times).
    pub fn at(origin: i64) -> Self {
        Self(Vec::new(), origin)
    }
    pub fn cut(&self, lines: usize) -> Self {
        Self(self.0[..lines].to_vec(), self.1)
    }
    pub fn prompt(&mut self, at: i64, id: &str) {
        self.0.push(
            json!({"type":"user","timestamp":stamp_from(self.1, at),"promptId":id,"uuid":id,
            "origin":{"kind":"human"},"message":{"role":"user","content":"go"}}),
        );
    }
    pub fn request(&mut self, at: i64) {
        self.0.push(
            json!({"type":"api-request","timestamp":stamp_from(self.1, at),"id":format!("r{at}")}),
        );
    }
    /// Request id of the last request line: what the next output lines answer.
    fn last_request(&self) -> String {
        self.0
            .iter()
            .rev()
            .find(|line| line["type"] == "api-request")
            .and_then(|line| line["id"].as_str())
            .unwrap_or("none")
            .to_owned()
    }
    pub fn text(&mut self, at: i64, end_turn: bool) {
        let request = self.last_request();
        self.text_in(at, &request, end_turn);
    }
    pub fn text_in(&mut self, at: i64, request: &str, end_turn: bool) {
        self.0.push(json!({"type":"assistant","timestamp":stamp_from(self.1, at),"requestRef":request,
            "message":{"id":format!("m{at}"),"stop_reason": if end_turn {"end_turn"} else {"tool_use"},
            "content":[{"type":"text","text":"x"}]}}));
    }
    pub fn tool(&mut self, at: i64, id: &str, name: &str, input: Value) {
        let request = self.last_request();
        self.0.push(
            json!({"type":"assistant","timestamp":stamp_from(self.1, at),"requestRef":request,
            "message":{"id":format!("m{at}"),"stop_reason":"tool_use",
            "content":[{"type":"tool_use","id":id,"name":name,"input":input}]}}),
        );
    }
    pub fn result(&mut self, at: i64, id: &str, background: Option<&str>) {
        let mut line = json!({"type":"user","timestamp":stamp_from(self.1, at),
            "message":{"role":"user","content":[{"type":"tool_result","tool_use_id":id,"content":"ok"}]}});
        if let Some(task) = background {
            line["toolUseResult"] = json!({"backgroundTaskId": task});
            line["message"]["content"][0]["content"] = json!(format!(
                "Command running in background with ID: {task}. Output is being written to: /t/tasks/{task}.output. You will be notified when it completes"));
        }
        self.0.push(line);
    }
    pub fn notified(&mut self, at: i64, task: &str) {
        self.0.push(json!({"type":"user","timestamp":stamp_from(self.1, at),
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
        json!({"command":"until grep -q rc= /t/tasks/bg1.output; do sleep 15; done"}),
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
    if wait_call == "TaskOutput" {
        t.tool(
            4_000,
            "u2",
            "TaskOutput",
            json!({"task_id": task, "block": true}),
        );
    } else {
        t.tool(4_000, "u2", "Bash", json!({"command": wait_call}));
    }
    t
}

#[test]
fn open_wait_names_the_task_and_counts_the_open_tail() {
    let call = "TaskOutput";
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
    let idle = t.cut(6).read();
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
    let mut t = explicit_wait("bg1", "TaskOutput");
    t.prompt(5 * MIN, "p2");
    assert_eq!(t.read().open_wait(T0 + 60 * MIN), None);
}

#[test]
fn writing_is_one_request_and_an_output_of_another_request_is_unknown() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.text_in(2_000, "r1000", false);
    t.text_in(5_000, "r1000", false);
    t.text_in(9_000, "r-other", true);
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
    assert_eq!(
        classify_command("until grep -q rc= /tmp/pytest-of-u/t/tasks/b.output; do sleep 15; done"),
        Class::Wait
    );
    assert_eq!(classify_command("sleep 30 && cargo test -p x"), Class::Test);
}

#[test]
fn a_first_output_of_another_request_is_not_model_wait() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.text_in(4_000, "r-not-this-one", true);
    let row = &t.read().rows[0];
    assert_eq!(cause(row, Cause::ModelWait), 0);
    assert_eq!(cause(row, Cause::Unknown), 3_000);
}

#[test]
fn a_task_id_is_named_exactly_never_as_a_prefix() {
    // bg1 is running; a call for bg10 names none of the running tasks.
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    t.tool(
        4_000,
        "u2",
        "TaskOutput",
        json!({"task_id": "bg10", "block": true}),
    );
    assert_eq!(t.read().open_wait(T0 + 60 * MIN), None);
    let wait = explicit_wait("bg10", "TaskOutput")
        .read()
        .open_wait(T0 + 60 * MIN)
        .unwrap();
    assert_eq!(
        (wait.task_id.as_str(), wait.call_id.as_str()),
        ("bg10", "u2")
    );
}

#[test]
fn idle_time_before_a_wait_call_does_not_count_toward_the_cap() {
    let mut t = background_test_wait().cut(6);
    // The writer sat idle for 20 minutes, then opens a wait call on the still running task.
    t.request(20 * MIN);
    t.tool(
        20 * MIN + 1_000,
        "w9",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let reader = t.read();
    let wait = reader.open_wait(T0 + 20 * MIN + 31_000).unwrap();
    assert_eq!(wait.waited_ms, 30_000, "only the open wait call counts");
    assert!(
        cause(&reader.rows[0], Cause::WaitTest) > 19 * MIN,
        "idle time is still booked"
    );
}

#[test]
fn lines_after_a_closed_round_are_not_booked_but_the_open_gap_is_booked_to_the_close() {
    let t = background_test_wait();
    let text: Vec<String> = t.0.iter().map(Value::to_string).collect();
    let closed = std::collections::HashMap::from([("sess:p1".to_string(), T0 + 10 * MIN)]);
    let reader = Reader::read_closed("sess", &text.join("\n"), closed);
    // The writer idled from 6 s on; the 20 minute notification is after the close.
    assert_eq!(reader.rows[0].ended_ms, T0 + 10 * MIN);
    assert_eq!(cause(&reader.rows[0], Cause::WaitTest), 10 * MIN - 6_000);
}

#[test]
fn a_shell_command_naming_the_output_path_is_telemetry_only() {
    let loop_call = "until grep -q rc= /t/tasks/bg1.output; do sleep 15; done";
    for call in [
        loop_call,
        "until grep -q rc= \"/t/tasks/bg1.output\"; do sleep 15; done",
        "sleep 900 # waiting for /t/tasks/bg1.output",
        "sleep 900; echo bg1",
        "tail -f /t/tasks/bg1.output",
    ] {
        let t = explicit_wait("bg1", call);
        assert_eq!(t.read().open_wait(T0 + 60 * MIN), None, "{call}");
    }
    // The same loop still books its minutes to the task's class.
    let mut t = explicit_wait("bg1", loop_call);
    t.result(4_000 + 9 * MIN, "u2", None);
    assert_eq!(cause(&t.read().rows[0], Cause::WaitTest), 9 * MIN);
    // A comment or a bare id binds nothing, not even for telemetry.
    let mut c = explicit_wait("bg1", "sleep 900 # /t/tasks/bg1.output");
    c.result(4_000 + 9 * MIN, "u2", None);
    assert_eq!(cause(&c.read().rows[0], Cause::WaitSleep), 9 * MIN);
}

#[test]
fn a_task_output_call_binds_by_its_structured_task_id_and_only_when_it_blocks() {
    let call = |input: serde_json::Value| {
        let mut t = explicit_wait("bg1", "echo started");
        t.0.pop();
        t.tool(4_000, "u2", "TaskOutput", input);
        t.read().open_wait(T0 + 20 * MIN)
    };
    assert_eq!(
        call(json!({"task_id": "bg1", "block": true}))
            .unwrap()
            .task_id,
        "bg1"
    );
    assert_eq!(
        call(json!({"task_id": "bg1"})).unwrap().task_id,
        "bg1",
        "block defaults on"
    );
    assert_eq!(
        call(json!({"task_id": "bg1", "block": false})),
        None,
        "a poll is no wait"
    );
    assert_eq!(call(json!({"task_id": "bg10", "block": true})), None);
    assert_eq!(call(json!({"block": true})), None, "no task named");
}

#[test]
fn a_request_line_without_an_id_leaves_nothing_to_match() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.text_in(2_000, "r1000", false);
    // The next request carries no id; the output that follows names the earlier one.
    t.0.push(json!({"type":"api-request","timestamp": stamp_from(T0, 3_000)}));
    t.text_in(6_000, "r1000", true);
    let row = &t.read().rows[0];
    assert_eq!(cause(row, Cause::ModelWait), 1_000);
    assert_eq!(cause(row, Cause::Unknown), 3_000);
}

#[test]
fn a_closed_round_books_its_open_wait_up_to_the_close() {
    let t = explicit_wait(
        "bg1",
        "until grep -q rc= /t/tasks/bg1.output; do sleep 15; done",
    );
    let text: Vec<String> = t.0.iter().map(Value::to_string).collect();
    let closed = std::collections::HashMap::from([("sess:p1".to_string(), T0 + 20 * MIN)]);
    let row = &Reader::read_closed("sess", &text.join("\n"), closed).rows[0];
    assert_eq!(cause(row, Cause::WaitTest), 20 * MIN - 4_000);
    assert_eq!(row.ended_ms, T0 + 20 * MIN);
}

#[test]
fn time_after_an_ended_turn_with_nothing_running_is_between_rounds_not_harness() {
    let mut t = Transcript::new();
    t.prompt(0, "p1");
    t.request(1_000);
    t.text(2_000, true);
    // Driven again 30 minutes later by a steer or a notification, then a new request.
    t.request(30 * MIN);
    t.text(30 * MIN + 1_000, true);
    let row = &t.read().rows[0];
    assert_eq!(cause(row, Cause::BetweenRounds), 30 * MIN - 2_000);
    assert_eq!(cause(row, Cause::Harness), 1_000);
    assert_eq!(
        row.wall_ms(),
        3_000,
        "prompt to request, and two model waits"
    );
}

#[test]
fn consecutive_blocking_calls_on_one_task_are_one_episode_named_by_its_first_call() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.tool(
        4_000 + 5 * MIN + 500,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = t.read().open_wait(T0 + 4_000 + 8 * MIN).unwrap();
    assert_eq!(wait.call_id, "c1");
    assert_eq!(
        wait.waited_ms,
        8 * MIN - 500,
        "the time inside the chain's calls, not the gap"
    );
    // Work in between starts a new episode with its own first call and its own age.
    let mut broken = explicit_wait("bg1", "echo started");
    broken.0.pop();
    broken.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    broken.result(4_000 + 5 * MIN, "c1", None);
    broken.tool(
        4_000 + 5 * MIN + 100,
        "e1",
        "Edit",
        json!({"file_path": "a.rs"}),
    );
    broken.result(4_000 + 5 * MIN + 200, "e1", None);
    broken.tool(
        4_000 + 5 * MIN + 500,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = broken.read().open_wait(T0 + 4_000 + 8 * MIN).unwrap();
    assert_eq!(wait.call_id, "c2");
    assert_eq!(wait.waited_ms, 8 * MIN - 5 * MIN - 500);
    assert!(broken
        .read()
        .episode_alive("bg1", "c2", T0 + 4_000 + 5 * MIN + 10_000));
    assert!(
        !broken
            .read()
            .episode_alive("bg1", "c2", T0 + 4_000 + 9 * MIN),
        "a long silence is no gap between calls"
    );
    assert!(!broken
        .read()
        .episode_alive("bg1", "c1", T0 + 4_000 + 5 * MIN + 10_000));
}

#[test]
fn only_time_inside_blocking_calls_counts_and_a_long_gap_starts_a_new_episode() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // Two five minute calls back to back, then twenty minutes of nothing, then a new call.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.tool(
        4_000 + 5 * MIN + 2_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 10 * MIN, "c2", None);
    let chain = t.read().open_wait(T0 + 4_000 + 10 * MIN + 1_000);
    assert!(chain.is_none(), "no call is open");
    let at = 4_000 + 30 * MIN;
    t.tool(
        at,
        "c3",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = t.read().open_wait(T0 + at + 10_000).unwrap();
    assert_eq!(
        wait.call_id, "c3",
        "a call after a long gap is a new episode"
    );
    assert_eq!(
        wait.waited_ms, 10_000,
        "the idle twenty minutes are not wait time"
    );
    // Within the gap the same chain goes on and counts only its calls.
    let mut chain = explicit_wait("bg1", "echo started");
    chain.0.pop();
    chain.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    chain.result(4_000 + 5 * MIN, "c1", None);
    chain.tool(
        4_000 + 5 * MIN + 2_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let wait = chain.read().open_wait(T0 + 4_000 + 7 * MIN).unwrap();
    assert_eq!(wait.call_id, "c1");
    assert_eq!(wait.waited_ms, 5 * MIN + (7 * MIN - 5 * MIN - 2_000));
}

#[test]
fn overlapping_blocking_calls_count_their_union_not_their_sum() {
    let mut t = explicit_wait("bg1", "echo started");
    t.0.pop();
    // Three calls on the task: two five-minute calls that overlap almost completely and
    // are back to back with a third that is still open. Fifteen minutes of calls, but
    // only about eight minutes of waiting.
    t.tool(
        4_000,
        "c1",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.tool(
        6_000,
        "c2",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    t.result(4_000 + 5 * MIN, "c1", None);
    t.result(6_000 + 5 * MIN, "c2", None);
    t.tool(
        6_000 + 5 * MIN + 1_000,
        "c3",
        "TaskOutput",
        json!({"task_id": "bg1", "block": true}),
    );
    let now = T0 + 6_000 + 5 * MIN + 1_000 + 3 * MIN;
    let wait = t.read().open_wait(now).unwrap();
    assert_eq!(wait.call_id, "c1");
    // Union: [4 s, 5 min 6 s] plus [5 min 7 s, 8 min 7 s] = 5 min 2 s + 3 min.
    assert_eq!(wait.waited_ms, (5 * MIN + 2_000) + 3 * MIN);
}
