//! Fix-round time by cause, read from one writer transcript.
//!
//! Every gap between two transcript lines is booked once, to the cause the lines on
//! both sides and the tool calls still open across it prove. A gap without such
//! proof is booked as `unknown`; nothing is guessed. The same pass reports the wait
//! the writer is blocked in at the end of the transcript, which the watchdog reads.
//! This module holds the vocabulary; `round_reader` reads the transcript.

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

    pub fn wait_for_class(class: Class) -> Cause {
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
    /// `BLOCKED` once the watchdog closed the round; set by the caller from its record.
    pub outcome: Option<String>,
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
            "outcome": self.outcome,
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
