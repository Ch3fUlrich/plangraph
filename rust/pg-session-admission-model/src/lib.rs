//! Pure model of the session hook and admission commands for one attempt.
mod transition;

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Missing,
    Accepted,
    Starting,
    Running,
    Revoked,
    Failed,
    Published,
}

/// Process values identify a PID plus its start time. Generation identifies the conversation.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct Caller {
    pub attempt: u8,
    pub root: u8,
    pub job: u8,
    pub process: u8,
    pub generation: u8,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Gate {
    Healthy,
    Malformed,
    StateError,
    LockError,
    HookFailure,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tool {
    Admit { attempt: u8 },
    Read { file: usize, call: u8, whole: bool },
    Received { attempt: u8 },
    ReadyOrStatus,
    Work,
    ExternalWork,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Event {
    Accept,
    TakeStart,
    BindLaunchedJob {
        job: u8,
        process: u8,
    },
    ObserveScope {
        job: u8,
        process: u8,
    },
    Revoke,
    FailUnclaimed,
    FailEnded,
    PreTool {
        caller: Caller,
        gate: Gate,
        tool: Tool,
    },
    Admit {
        caller: Caller,
        gate: Gate,
    },
    PostRead {
        caller: Caller,
        gate: Gate,
        file: usize,
        call: u8,
        returned: String,
        guidance_enabled: bool,
    },
    AdvanceConversation {
        job: u8,
        generation: u8,
    },
    Receipt {
        caller: Caller,
        gate: Gate,
    },
    Publish {
        verified_generation: Option<u8>,
    },
    Ready {
        caller: Caller,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Applied,
    Bootstrap,
    TaskRead,
    ReceiptAllowed,
    Marked { guidance: bool },
    IgnoredRead,
    WorkAllowed,
    ReadyOrStatus,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Denied {
    Gate,
    Phase,
    Identity,
    Scope,
    Tool,
    Delivery,
    Ready,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct Identity {
    job: u8,
    process: u8,
}

impl Caller {
    fn identity(self) -> Identity {
        Identity {
            job: self.job,
            process: self.process,
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    attempt: u8,
    root: u8,
    files: Vec<String>,
    phase: Phase,
    launched: Option<Identity>,
    scope: Option<Identity>,
    claimed: Option<Identity>,
    conversation: u8,
    pending_reads: BTreeMap<u8, usize>,
    marks: BTreeSet<usize>,
    receipt: Option<u8>,
    published: Option<u8>,
    ready: bool,
}

impl Model {
    /// Start with a missing attempt and its proposed immutable manifest.
    pub fn new(attempt: u8, root: u8, files: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            attempt,
            root,
            files: files.into_iter().map(Into::into).collect(),
            phase: Phase::Missing,
            launched: None,
            scope: None,
            claimed: None,
            conversation: 0,
            pending_reads: BTreeMap::new(),
            marks: BTreeSet::new(),
            receipt: None,
            published: None,
            ready: false,
        }
    }

    pub fn phase(&self) -> Phase {
        self.phase
    }

    /// Check the durable identity, delivery and scope relations after each event.
    pub fn invariants_hold(&self) -> bool {
        (self.claimed.is_none() || (self.claimed == self.launched && self.claimed == self.scope))
            && (!matches!(self.phase, Phase::Running | Phase::Published) || self.claimed.is_some())
            && (self.phase != Phase::Revoked || self.claimed.is_none())
            && (self.phase != Phase::Published || self.published.is_some())
            && self.receipt.is_none_or(|generation| {
                generation == self.conversation && self.all_files_read() && self.claimed.is_some()
            })
            && self.marks.iter().all(|&file| file < self.files.len())
    }

    fn all_files_read(&self) -> bool {
        !self.files.is_empty() && self.marks.len() == self.files.len()
    }
}
