//! Fix-round time by cause, read from one writer transcript.
//!
//! Every gap between two transcript lines is booked once, to the cause the lines on
//! both sides and the tool calls still open across it prove. A gap without such
//! proof is booked as `unknown`; nothing is guessed. The same pass reports the wait
//! the writer is blocked in at the end of the transcript, which the watchdog reads.

use serde_json::{json, Map, Value};
use std::collections::HashMap;

/// Where the minutes of a round went. Order is the order of the digest.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Cause {
    ModelWait,
    ModelWrite,
    Test,
    Build,
    Gate,
    ToolOther,
    WaitTest,
    WaitBuild,
    WaitGate,
    WaitOther,
    WaitSleep,
    Harness,
    BetweenRounds,
    Unknown,
}

impl Cause {
    pub const ALL: [Cause; 14] = [
        Cause::ModelWait,
        Cause::ModelWrite,
        Cause::Test,
        Cause::Build,
        Cause::Gate,
        Cause::ToolOther,
        Cause::WaitTest,
        Cause::WaitBuild,
        Cause::WaitGate,
        Cause::WaitOther,
        Cause::WaitSleep,
        Cause::Harness,
        Cause::BetweenRounds,
        Cause::Unknown,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Cause::ModelWait => "model_wait",
            Cause::ModelWrite => "model_write",
            Cause::Test => "test",
            Cause::Build => "build",
            Cause::Gate => "gate",
            Cause::ToolOther => "tool_other",
            Cause::WaitTest => "wait_test",
            Cause::WaitBuild => "wait_build",
            Cause::WaitGate => "wait_gate",
            Cause::WaitOther => "wait_other",
            Cause::WaitSleep => "wait_sleep",
            Cause::Harness => "harness",
            Cause::BetweenRounds => "between_rounds",
            Cause::Unknown => "unknown",
        }
    }

    fn wait_for(class: Class) -> Cause {
        match class {
            Class::Test => Cause::WaitTest,
            Class::Build => Cause::WaitBuild,
            Class::Gate => Cause::WaitGate,
            Class::Other | Class::Wait => Cause::WaitOther,
        }
    }
}

/// What a command does. `Wait` is a call that only waits (sleep loop, task output).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Class {
    Test,
    Build,
    Gate,
    Other,
    Wait,
}

fn has(command: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| command.contains(needle))
}

/// Class of a shell command; a gate outranks a test because a gate runs tests.
pub fn classify_command(command: &str) -> Class {
    let first = command.trim_start();
    if has(
        command,
        &[
            "git push",
            "preflight.py",
            "ci_commit_gate",
            "check_changelog",
            "check_file_size",
        ],
    ) {
        Class::Gate
    } else if has(
        command,
        &["cargo test", "cargo nextest", "pytest", "affected_tests"],
    ) {
        Class::Test
    } else if has(
        command,
        &[
            "cargo build",
            "cargo check",
            "cargo clippy",
            "cargo fmt",
            "cargo install",
        ],
    ) {
        Class::Build
    } else if first.starts_with("sleep ")
        || first.starts_with("until ")
        || first.starts_with("while ")
        || first.starts_with("tail -f")
    {
        Class::Wait
    } else {
        Class::Other
    }
}

/// One row per writer round, keyed so a re-read of the transcript rewrites it.
#[derive(Clone, Debug, PartialEq)]
pub struct RoundRow {
    pub round_key: String,
    pub index: usize,
    pub started_ms: i64,
    pub ended_ms: i64,
    pub causes: HashMap<Cause, i64>,
}

impl RoundRow {
    /// Wall time of the round: every cause except the gap before its prompt.
    pub fn wall_ms(&self) -> i64 {
        self.causes
            .iter()
            .filter(|(cause, _)| **cause != Cause::BetweenRounds)
            .map(|(_, ms)| *ms)
            .sum()
    }

    pub fn to_json(&self, host: &str, session: &str) -> Value {
        let mut causes = Map::new();
        for cause in Cause::ALL {
            causes.insert(
                cause.name().into(),
                json!(self.causes.get(&cause).copied().unwrap_or(0)),
            );
        }
        json!({
            "round_key": self.round_key, "host": host, "session": session,
            "index": self.index, "started_ms": self.started_ms,
            "ended_ms": self.ended_ms, "wall_ms": self.wall_ms(), "causes": causes,
        })
    }
}

/// The wait a writer is blocked in at the end of the transcript.
#[derive(Clone, Debug, PartialEq)]
pub struct OpenWait {
    pub round_key: String,
    pub task_id: String,
    pub command: String,
    pub class: Class,
    /// Wait time already booked against the task plus the open tail.
    pub waited_ms: i64,
}

#[derive(Debug)]
struct Background {
    id: String,
    command: String,
    class: Class,
    waited_ms: i64,
    tool_use_id: String,
}

#[derive(Debug)]
struct OpenTool {
    name: String,
    class: Class,
    command: String,
    task_ref: Option<String>,
    background: bool,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Prev {
    None,
    Request,
    Assistant { end_turn: bool },
    Other,
}

#[derive(Default)]
pub struct Reader {
    pub rows: Vec<RoundRow>,
    session: String,
    prev_ts: Option<i64>,
    prev: Option<Prev>,
    open: HashMap<String, OpenTool>,
    background: Vec<Background>,
    /// Background tool_use_id seen, waiting for its toolUseResult task id.
    pending_background: HashMap<String, String>,
    ended: bool,
}

fn parse_ts(value: &Value) -> Option<i64> {
    let text = value.as_str()?;
    chrono::DateTime::parse_from_rfc3339(text)
        .ok()
        .map(|t| t.timestamp_millis())
}

impl Reader {
    pub fn new(session: &str) -> Self {
        Self {
            session: session.into(),
            prev: Some(Prev::None),
            ..Self::default()
        }
    }

    /// Read a whole transcript. Lines that are not JSON or carry no timestamp are skipped.
    pub fn read(session: &str, text: &str) -> Self {
        let mut reader = Self::new(session);
        for line in text.lines() {
            if let Ok(value) = serde_json::from_str::<Value>(line) {
                reader.feed(&value);
            }
        }
        reader
    }

    fn current(&mut self) -> Option<&mut RoundRow> {
        self.rows.last_mut()
    }

    fn book(&mut self, cause: Cause, ms: i64) {
        if let Some(row) = self.current() {
            *row.causes.entry(cause).or_insert(0) += ms;
            row.ended_ms += if cause == Cause::BetweenRounds { 0 } else { ms };
        }
    }

    fn wait_target(&self, ids: &[&str], hint: Option<&str>) -> Option<usize> {
        let mentioned = |bg: &Background| {
            hint.is_some_and(|text| text.contains(&bg.id)) || ids.contains(&bg.id.as_str())
        };
        self.background
            .iter()
            .position(mentioned)
            .or(if self.background.is_empty() {
                None
            } else {
                Some(0)
            })
    }

    /// The cause of the gap that ends at this line, from what is open across it.
    fn cause_of_gap(&mut self, next: Prev, human: bool, gap: i64) -> Cause {
        if human {
            return Cause::BetweenRounds;
        }
        if !self.open.is_empty() {
            return self.tool_cause(gap);
        }
        match (self.prev.unwrap_or(Prev::None), next) {
            (Prev::Assistant { end_turn: true }, _) if !self.background.is_empty() => {
                self.book_wait(0, gap)
            }
            (Prev::Request, Prev::Assistant { .. }) => Cause::ModelWait,
            (Prev::Assistant { .. }, Prev::Assistant { .. }) => Cause::ModelWrite,
            (Prev::Assistant { .. } | Prev::Other, Prev::Request) => Cause::Harness,
            _ => Cause::Unknown,
        }
    }

    fn book_wait(&mut self, index: usize, gap: i64) -> Cause {
        let Some(task) = self.background.get_mut(index) else {
            return Cause::WaitSleep;
        };
        task.waited_ms += gap;
        Cause::wait_for(task.class)
    }

    /// Overlapping tool calls: a wait outranks a gate, a test, a build, any other tool.
    fn tool_cause(&mut self, gap: i64) -> Cause {
        let waiting = self.open.values().find(|tool| tool.class == Class::Wait);
        if let Some(tool) = waiting {
            let reference = tool.task_ref.clone().unwrap_or_default();
            let command = tool.command.clone();
            let hint = format!("{reference} {command}");
            return match self.wait_target(&[], Some(&hint)) {
                Some(index) => self.book_wait(index, gap),
                None => Cause::WaitSleep,
            };
        }
        let rank = |class| match class {
            Class::Gate => 3,
            Class::Test => 2,
            Class::Build => 1,
            _ => 0,
        };
        let top = self
            .open
            .values()
            .map(|tool| tool.class)
            .max_by_key(|class| rank(*class))
            .unwrap_or(Class::Other);
        match top {
            Class::Gate => Cause::Gate,
            Class::Test => Cause::Test,
            Class::Build => Cause::Build,
            _ => Cause::ToolOther,
        }
    }

    pub fn feed(&mut self, value: &Value) {
        let Some(ts) = parse_ts(&value["timestamp"]) else {
            return;
        };
        let kind = value["type"].as_str().unwrap_or("");
        let origin = value["origin"]["kind"].as_str();
        let human = kind == "user"
            && value["message"]["content"]
                .as_array()
                .is_none_or(|blocks| blocks.iter().all(|b| b["type"] != "tool_result"))
            && (origin == Some("human") || origin.is_none());
        let next = match kind {
            "api-request" => Prev::Request,
            "assistant" => Prev::Assistant {
                end_turn: value["message"]["stop_reason"] == "end_turn",
            },
            "user" => Prev::Other,
            _ => return,
        };
        if kind == "user" && !human && origin != Some("task-notification") {
            // Tool results are the only other user lines that move the clock.
            if value["message"]["content"].as_array().is_none() {
                return;
            }
        }
        let ts = self.prev_ts.map_or(ts, |previous| ts.max(previous));
        if human {
            self.start_round(value, ts);
        }
        if let Some(previous) = self.prev_ts {
            let gap = ts - previous;
            if gap > 0 && !self.rows.is_empty() {
                let cause = self.cause_of_gap(next, human && self.rows.len() > 1, gap);
                self.book(cause, gap);
            }
        }
        self.prev_ts = Some(ts);
        self.prev = Some(next);
        match kind {
            "assistant" => self.open_tools(value),
            "user" => self.close_tools(value),
            _ => {}
        }
    }

    fn start_round(&mut self, value: &Value, ts: i64) {
        let id = value["promptId"]
            .as_str()
            .or_else(|| value["uuid"].as_str())
            .unwrap_or("");
        self.open.clear();
        self.pending_background.clear();
        self.background.clear();
        self.rows.push(RoundRow {
            round_key: format!("{}:{id}", self.session),
            index: self.rows.len() + 1,
            started_ms: ts,
            ended_ms: ts,
            causes: HashMap::new(),
        });
        self.ended = false;
    }

    fn open_tools(&mut self, value: &Value) {
        for block in value["message"]["content"].as_array().into_iter().flatten() {
            if block["type"] != "tool_use" {
                continue;
            }
            let name = block["name"].as_str().unwrap_or("").to_owned();
            let input = &block["input"];
            let command = input["command"].as_str().unwrap_or("").to_owned();
            let waits = matches!(name.as_str(), "TaskOutput" | "BashOutput" | "Monitor");
            let class = if waits {
                Class::Wait
            } else if name == "Bash" {
                classify_command(&command)
            } else {
                Class::Other
            };
            let id = block["id"].as_str().unwrap_or("").to_owned();
            let background = input["run_in_background"] == true;
            let task_ref = ["task_id", "bash_id"]
                .iter()
                .find_map(|key| input[*key].as_str())
                .map(str::to_owned);
            if background {
                self.pending_background.insert(id.clone(), command.clone());
            }
            self.open.insert(
                id,
                OpenTool {
                    name,
                    class,
                    command,
                    task_ref,
                    background,
                },
            );
        }
    }

    fn close_tools(&mut self, value: &Value) {
        for block in value["message"]["content"].as_array().into_iter().flatten() {
            if block["type"] != "tool_result" {
                continue;
            }
            let id = block["tool_use_id"].as_str().unwrap_or("");
            let Some(tool) = self.open.remove(id) else {
                continue;
            };
            let task = value["toolUseResult"]["backgroundTaskId"].as_str();
            if let (true, Some(task)) = (tool.background, task) {
                self.background.push(Background {
                    id: task.into(),
                    class: classify_command(&tool.command),
                    command: tool.command,
                    waited_ms: 0,
                    tool_use_id: id.into(),
                });
            }
            let _ = tool.name;
        }
        if value["origin"]["kind"] == "task-notification" {
            let text = value["message"]["content"].as_str().unwrap_or("");
            self.background
                .retain(|bg| !text.contains(&format!("<task-id>{}</task-id>", bg.id)));
        }
    }

    /// The wait the writer is in at `now_ms`, with the open tail booked against it.
    pub fn open_wait(&self, now_ms: i64) -> Option<OpenWait> {
        let row = self.rows.last()?;
        let tail = (now_ms - self.prev_ts?).max(0);
        let hint: String = self
            .open
            .values()
            .filter(|tool| tool.class == Class::Wait)
            .map(|tool| {
                format!(
                    "{} {}",
                    tool.task_ref.clone().unwrap_or_default(),
                    tool.command
                )
            })
            .collect::<Vec<_>>()
            .join(" ");
        let idle =
            self.open.is_empty() && matches!(self.prev, Some(Prev::Assistant { end_turn: true }));
        let blocked = idle || self.open.values().any(|tool| tool.class == Class::Wait);
        let ids: Vec<&str> = Vec::new();
        let target = if blocked {
            self.wait_target(&ids, Some(&hint))
        } else {
            None
        }?;
        let task = &self.background[target];
        Some(OpenWait {
            round_key: row.round_key.clone(),
            task_id: task.id.clone(),
            command: task.command.clone(),
            class: task.class,
            waited_ms: task.waited_ms + tail,
        })
    }

    /// Tool use that started a background task, for correlating owners.
    pub fn background_tool_use(&self, task_id: &str) -> Option<&str> {
        self.background
            .iter()
            .find(|bg| bg.id == task_id)
            .map(|bg| bg.tool_use_id.as_str())
    }
}
