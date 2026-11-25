//! How a transcript names a background task and the call that waits on it.
//!
//! A task is known by the id and the output path its start result reports. A wait call
//! is bound to a task only by a structured field (`task_id` of a task-output call) or by
//! the task's full output path as a word of a shell command outside its comments; the
//! bare id in free text, a comment or another path binds nothing.

use serde_json::Value;

/// A started background task with the evidence of when and where it launched.
#[derive(Clone, Debug, PartialEq)]
pub struct TaskRef {
    pub id: String,
    /// Full path the harness gave as the task's output file.
    pub output_path: Option<String>,
    /// The launching call was issued at this time, and its result came back at that one.
    pub launched_from_ms: i64,
    pub launched_to_ms: i64,
}

const MARK: &str = "Output is being written to: ";

/// The output path of a start result: the word after the fixed statement, minus its stop.
pub fn output_path_of(result: &Value) -> Option<String> {
    let text = match &result["content"] {
        Value::String(text) => text.clone(),
        Value::Array(blocks) => blocks.iter().filter_map(|b| b["text"].as_str()).collect(),
        _ => return None,
    };
    let rest = &text[text.find(MARK)? + MARK.len()..];
    let word = rest.split_whitespace().next()?;
    let path = word.strip_suffix('.').unwrap_or(word);
    path.starts_with('/').then(|| path.to_owned())
}

/// Shell words of `command` outside comments: split at blanks and shell punctuation.
fn words(command: &str) -> Vec<String> {
    let mut out = Vec::new();
    for line in command.lines() {
        let mut kept = String::new();
        let mut prev_blank = true;
        for c in line.chars() {
            if c == '#' && prev_blank {
                break;
            }
            prev_blank = c.is_whitespace();
            kept.push(c);
        }
        out.extend(
            kept.split(|c: char| c.is_whitespace() || ";|&()<>\"'`".contains(c))
                .filter(|w| !w.is_empty())
                .map(str::to_owned),
        );
    }
    out
}

/// True when `command` names `path` as one of its words outside comments.
pub fn command_reads(command: &str, path: &str) -> bool {
    words(command).iter().any(|word| word == path)
}

impl TaskRef {
    pub fn to_json(&self) -> Value {
        serde_json::json!({
            "id": self.id, "output_path": self.output_path,
            "launched_from_ms": self.launched_from_ms, "launched_to_ms": self.launched_to_ms,
        })
    }

    pub fn from_json(value: &Value) -> Option<Self> {
        Some(Self {
            id: value["id"].as_str()?.to_owned(),
            output_path: value["output_path"].as_str().map(str::to_owned),
            launched_from_ms: value["launched_from_ms"].as_i64()?,
            launched_to_ms: value["launched_to_ms"].as_i64()?,
        })
    }
}

/// The exit code a task notification reports: "... exit code 143".
pub fn exit_code_of(notification: &str) -> Option<i64> {
    let rest = &notification[notification.find("exit code ")? + "exit code ".len()..];
    let digits: String = rest
        .chars()
        .enumerate()
        .take_while(|(i, c)| c.is_ascii_digit() || (*i == 0 && *c == '-'))
        .map(|(_, c)| c)
        .collect();
    digits.parse().ok()
}
