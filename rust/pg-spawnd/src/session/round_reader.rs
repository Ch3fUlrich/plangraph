//! Transcript reader: books each gap between two lines once and names the open wait.
//!
//! Timing evidence comes from four line kinds of the harness transcript: `api-request`
//! (the request left, with its id), `assistant` (first output, then more output of the
//! same request, named by `requestRef`),
//! tool calls with their results, and task notifications. A gap that these lines do not
//! explain is booked as `unknown`.

use super::{
    round_time::{classify_command, Cause, Class, OpenWait, RoundRow},
    round_wait::{command_reads, output_path_of, TaskRef},
};
use serde_json::Value;
use std::collections::HashMap;

#[derive(Debug)]
struct Background {
    task: TaskRef,
    command: String,
    class: Class,
}

#[derive(Debug)]
struct OpenTool {
    class: Class,
    command: String,
    /// `task_id` of a task-output call: a structured binding to one task.
    task_ref: Option<String>,
    background: bool,
    /// When the call was issued: a wait is as old as its own call.
    started_ms: i64,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Prev {
    None,
    Request,
    Assistant { end_turn: bool },
    Other,
}

/// A background task that is still running, for the owner record.
#[derive(Clone, Debug, PartialEq)]
pub struct Running {
    pub round_key: String,
    pub task: TaskRef,
    pub command: String,
}

#[derive(Default)]
pub struct Reader {
    pub rows: Vec<RoundRow>,
    session: String,
    prev_ts: Option<i64>,
    prev: Option<Prev>,
    prev_request: Option<String>,
    /// Rounds the watchdog closed (round key to closing time): later lines are not booked.
    closed: HashMap<String, i64>,
    open: HashMap<String, OpenTool>,
    background: Vec<Background>,
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

    /// Read a transcript in which the given rounds were closed at the given times.
    pub fn read_closed(session: &str, text: &str, closed: HashMap<String, i64>) -> Self {
        let mut reader = Self::new(session);
        reader.closed = closed;
        reader.feed_all(text);
        reader.book_closed_tail();
        reader
    }

    fn feed_all(&mut self, text: &str) {
        for line in text.lines() {
            if let Ok(value) = serde_json::from_str::<Value>(line) {
                self.feed(&value);
            }
        }
    }

    /// Read a whole transcript. Lines that are not JSON or carry no timestamp are skipped.
    pub fn read(session: &str, text: &str) -> Self {
        Self::read_closed(session, text, HashMap::new())
    }

    fn book(&mut self, cause: Cause, ms: i64) {
        if let Some(row) = self.rows.last_mut() {
            *row.causes.entry(cause).or_insert(0) += ms;
            if cause != Cause::BetweenRounds {
                row.ended_ms += ms;
            }
        }
    }

    /// The running task a wait call is bound to: by the structured task id of a
    /// task-output call, or by the task's full output path as a word of the command.
    fn named_task(&self, tool: &OpenTool) -> Option<usize> {
        if let Some(id) = &tool.task_ref {
            return self.background.iter().position(|bg| &bg.task.id == id);
        }
        self.background.iter().position(|bg| {
            bg.task
                .output_path
                .as_deref()
                .is_some_and(|path| command_reads(&tool.command, path))
        })
    }

    fn cause_of_gap(&mut self, next: Prev, request: Option<&str>, human: bool, gap: i64) -> Cause {
        if human {
            return Cause::BetweenRounds;
        }
        if !self.open.is_empty() {
            return self.tool_cause(gap);
        }
        match (self.prev.unwrap_or(Prev::None), next) {
            (Prev::Assistant { end_turn: true }, _) if !self.background.is_empty() => {
                // The writer ended its turn with a command running: the time is booked
                // to the oldest one, but no call names it, so it never counts toward a stop.
                Cause::wait_for_class(self.background[0].class)
            }
            // The request line and the first output are one request: their ids agree.
            (Prev::Request, Prev::Assistant { .. })
                if request.is_some() && request == self.prev_request.as_deref() =>
            {
                Cause::ModelWait
            }
            // More output counts as writing only inside one request.
            (Prev::Assistant { .. }, Prev::Assistant { .. })
                if request.is_some() && request == self.prev_request.as_deref() =>
            {
                Cause::ModelWrite
            }
            (Prev::Assistant { .. } | Prev::Other, Prev::Request) => Cause::Harness,
            _ => Cause::Unknown,
        }
    }

    /// Overlapping tool calls: a wait outranks a gate, a test, a build, any other tool.
    fn tool_cause(&mut self, _gap: i64) -> Cause {
        let waiting = self.open.values().find(|tool| tool.class == Class::Wait);
        if let Some(tool) = waiting {
            return match self.named_task(tool) {
                Some(index) => Cause::wait_for_class(self.background[index].class),
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
        let plain_text = value["message"]["content"].as_array().is_none();
        if kind == "user" && !human && plain_text && origin != Some("task-notification") {
            return;
        }
        let ts = self.prev_ts.map_or(ts, |previous| ts.max(previous));
        let closed_at = self
            .rows
            .last()
            .and_then(|row| self.closed.get(&row.round_key));
        if !human && closed_at.is_some_and(|at| ts > *at) {
            return;
        }
        if human {
            self.start_round(value, ts);
        }
        let request = match kind {
            "api-request" => value["id"].as_str(),
            "assistant" => value["requestRef"].as_str(),
            _ => None,
        }
        .map(str::to_owned);
        if let Some(previous) = self.prev_ts {
            let gap = ts - previous;
            if gap > 0 && !self.rows.is_empty() {
                let cause =
                    self.cause_of_gap(next, request.as_deref(), human && self.rows.len() > 1, gap);
                self.book(cause, gap);
            }
        }
        self.prev_ts = Some(ts);
        self.prev = Some(next);
        // A request or output line without an id leaves nothing to match against.
        if matches!(kind, "api-request" | "assistant") {
            self.prev_request = request;
        }
        match kind {
            "assistant" => self.open_tools(value, ts),
            "user" => self.close_tools(value, ts),
            _ => {}
        }
    }

    fn start_round(&mut self, value: &Value, ts: i64) {
        let id = value["promptId"]
            .as_str()
            .or_else(|| value["uuid"].as_str())
            .unwrap_or("");
        // A round that ends closes every wait and forgets the commands of that round.
        self.open.clear();
        self.background.clear();
        self.rows.push(RoundRow {
            round_key: format!("{}:{id}", self.session),
            index: self.rows.len() + 1,
            started_ms: ts,
            ended_ms: ts,
            causes: HashMap::new(),
            outcome: None,
        });
    }

    fn open_tools(&mut self, value: &Value, ts: i64) {
        for block in value["message"]["content"].as_array().into_iter().flatten() {
            if block["type"] != "tool_use" {
                continue;
            }
            let name = block["name"].as_str().unwrap_or("");
            let input = &block["input"];
            let command = input["command"].as_str().unwrap_or("").to_owned();
            let waits = matches!(name, "TaskOutput" | "BashOutput" | "Monitor");
            let class = if waits {
                Class::Wait
            } else if name == "Bash" {
                classify_command(&command)
            } else {
                Class::Other
            };
            let id = block["id"].as_str().unwrap_or("").to_owned();
            let task_ref = ["task_id", "bash_id"]
                .iter()
                .find_map(|key| input[*key].as_str())
                .map(str::to_owned);
            self.open.insert(
                id,
                OpenTool {
                    class,
                    command,
                    task_ref,
                    background: input["run_in_background"] == true,
                    started_ms: ts,
                },
            );
        }
    }

    fn close_tools(&mut self, value: &Value, ts: i64) {
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
                    task: TaskRef {
                        id: task.into(),
                        output_path: output_path_of(block),
                        launched_from_ms: tool.started_ms,
                        launched_to_ms: ts,
                    },
                    class: classify_command(&tool.command),
                    command: tool.command,
                });
            }
        }
        if value["origin"]["kind"] == "task-notification" {
            let text = value["message"]["content"].as_str().unwrap_or("");
            self.background
                .retain(|bg| !text.contains(&format!("<task-id>{}</task-id>", bg.task.id)));
        }
    }

    /// Background tasks that have started and not yet been reported finished.
    pub fn running(&self) -> Vec<Running> {
        let Some(row) = self.rows.last() else {
            return Vec::new();
        };
        self.background
            .iter()
            .map(|bg| Running {
                round_key: row.round_key.clone(),
                task: bg.task.clone(),
                command: bg.command.clone(),
            })
            .collect()
    }

    /// The wait the writer is in at `now_ms`: the oldest open wait call bound to one
    /// running task. Its age is the age of that call alone; idle time, an earlier call
    /// or a sleep that names no task does not count.
    pub fn open_wait(&self, now_ms: i64) -> Option<OpenWait> {
        let row = self.rows.last()?;
        let mut calls: Vec<(&String, &OpenTool)> = self
            .open
            .iter()
            .filter(|(_, tool)| tool.class == Class::Wait)
            .collect();
        calls.sort_by_key(|(id, _)| id.as_str());
        let (call_id, tool, index) = calls
            .into_iter()
            .find_map(|(id, tool)| Some((id.clone(), tool, self.named_task(tool)?)))?;
        let bg = &self.background[index];
        Some(OpenWait {
            round_key: row.round_key.clone(),
            task_id: bg.task.id.clone(),
            task: bg.task.clone(),
            call_id,
            command: bg.command.clone(),
            class: bg.class,
            waited_ms: (now_ms - tool.started_ms).max(0),
        })
    }

    /// A round the watchdog closed ends at its terminal time: the open wait that earned
    /// the close is booked up to it, so the row holds the minutes that triggered it.
    fn book_closed_tail(&mut self) {
        let Some(at) = self
            .rows
            .last()
            .and_then(|row| self.closed.get(&row.round_key))
            .copied()
        else {
            return;
        };
        let Some(previous) = self.prev_ts.filter(|p| *p < at) else {
            return;
        };
        let gap = at - previous;
        let cause = if !self.open.is_empty() {
            self.tool_cause(gap)
        } else {
            self.cause_of_gap(Prev::Other, None, false, gap)
        };
        self.book(cause, gap);
        self.prev_ts = Some(at);
    }
}
