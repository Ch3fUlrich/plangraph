//! Transcript reader: books each gap between two lines once and names the open wait.
//!
//! Timing evidence comes from four line kinds of the harness transcript: `api-request`
//! (the request left, with its id), `assistant` (first output, then more output of the
//! same request, named by `requestRef`),
//! tool calls with their results, and task notifications. A gap that these lines do not
//! explain is booked as `unknown`.

use super::{
    round_time::{classify_command, Cause, Class, RoundRow},
    round_wait::{exit_code_of, output_path_of, TaskRef},
};
use serde_json::Value;
use std::collections::HashMap;

/// Calls of one wait follow each other within this many milliseconds.
pub(super) const EPISODE_GAP_MS: i64 = 30_000;

/// Back-to-back blocking task-output calls on one task, with the first call's id.
#[derive(Debug)]
pub(super) struct Episode {
    pub(super) first_call: String,
    /// Time spent inside its closed blocking calls: idle time between calls is not counted.
    pub(super) blocked_ms: i64,
    /// When its last call returned; a call after a longer gap starts a new episode.
    pub(super) last_end_ms: i64,
    /// Another call or a new round came between: the next wait call starts a new episode.
    pub(super) interrupted: bool,
}

#[derive(Debug)]
pub(super) struct Background {
    pub(super) task: TaskRef,
    pub(super) command: String,
    pub(super) class: Class,
}

#[derive(Debug)]
pub(super) struct OpenTool {
    pub(super) class: Class,
    pub(super) command: String,
    /// `task_id` of a task-output call: a structured binding to one task.
    pub(super) task_ref: Option<String>,
    pub(super) background: bool,
    /// A blocking task-output call naming one task: the only wait that authorises a stop.
    pub(super) structured: bool,
    /// When the call was issued: a wait is as old as its own call.
    pub(super) started_ms: i64,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum Prev {
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
    pub(super) session: String,
    pub(super) prev_ts: Option<i64>,
    pub(super) prev: Option<Prev>,
    pub(super) prev_request: Option<String>,
    /// Rounds the watchdog closed (round key to closing time): later lines are not booked.
    pub(super) closed: HashMap<String, i64>,
    pub(super) open: HashMap<String, OpenTool>,
    pub(super) background: Vec<Background>,
    /// Exit codes the harness reported for finished tasks.
    pub(super) exits: HashMap<String, i64>,
    /// Wait episodes by task id.
    pub(super) episodes: HashMap<String, Episode>,
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

    pub(super) fn book(&mut self, cause: Cause, ms: i64) {
        if let Some(row) = self.rows.last_mut() {
            *row.causes.entry(cause).or_insert(0) += ms;
            if cause != Cause::BetweenRounds {
                row.ended_ms += ms;
            }
        }
    }

    pub(super) fn cause_of_gap(
        &mut self,
        next: Prev,
        request: Option<&str>,
        human: bool,
        gap: i64,
    ) -> Cause {
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
            // The writer ended its turn with nothing running: the time until it is driven
            // again (a notification, a steer, the next prompt) is time between rounds.
            (Prev::Assistant { end_turn: true }, Prev::Request | Prev::Other) => {
                Cause::BetweenRounds
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
    pub(super) fn tool_cause(&mut self, _gap: i64) -> Cause {
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
        self.episodes.clear();
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
            let task_output = matches!(name, "TaskOutput" | "BashOutput");
            // A task-output call blocks unless it says `block: false`.
            let blocking = input["block"] != false;
            let class = if task_output {
                if blocking {
                    Class::Wait
                } else {
                    Class::Other
                }
            } else if name == "Monitor" {
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
            let structured = task_output && blocking && task_ref.is_some();
            // An episode is a chain of back-to-back blocking calls on one task: a single
            // call cannot block longer than the harness allows, so the writer that waits
            // for a command keeps calling. Any other call ends every episode.
            match (structured, &task_ref) {
                (true, Some(task)) => {
                    for (other, episode) in self.episodes.iter_mut() {
                        episode.interrupted |= other != task;
                    }
                    let continues = self
                        .episodes
                        .get(task)
                        .is_some_and(|e| !e.interrupted && ts - e.last_end_ms <= EPISODE_GAP_MS);
                    if !continues {
                        let episode = Episode {
                            first_call: id.clone(),
                            blocked_ms: 0,
                            last_end_ms: ts,
                            interrupted: false,
                        };
                        self.episodes.insert(task.clone(), episode);
                    }
                }
                _ => self
                    .episodes
                    .values_mut()
                    .for_each(|e| e.interrupted = true),
            }
            self.open.insert(
                id,
                OpenTool {
                    class,
                    command,
                    structured,
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
            if let (true, Some(task)) = (tool.structured, &tool.task_ref) {
                if let Some(episode) = self.episodes.get_mut(task) {
                    episode.blocked_ms += (ts - tool.started_ms).max(0);
                    episode.last_end_ms = ts;
                }
            }
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
            for bg in &self.background {
                if text.contains(&format!("<task-id>{}</task-id>", bg.task.id)) {
                    if let Some(code) = exit_code_of(text) {
                        self.exits.insert(bg.task.id.clone(), code);
                    }
                }
            }
            self.episodes
                .retain(|task, _| !text.contains(&format!("<task-id>{task}</task-id>")));
            self.background
                .retain(|bg| !text.contains(&format!("<task-id>{}</task-id>", bg.task.id)));
        }
    }
}
