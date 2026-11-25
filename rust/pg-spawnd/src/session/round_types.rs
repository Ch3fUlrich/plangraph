//! The vocabulary of the round watchdog: its limits, its host effects and its log events.

use super::round_wait::TaskRef;
use std::io;

/// Longest wait on one background command before the watchdog ends it.
pub const WAIT_LIMIT_MS: i64 = 15 * 60_000;
/// Waits ended in one round before the round is BLOCKED.
pub const STRIKE_LIMIT: usize = 2;
/// How long a BLOCKED round waits for its notices to be delivered before it closes anyway.
pub const NOTICE_GRACE_MS: i64 = 10 * 60_000;

/// The process that runs a background command, found on the executing host.
#[derive(Clone, Debug, PartialEq)]
pub struct Owner {
    pub pid: u32,
    pub start_time: Option<u64>,
    /// Device and inode of the file the owner writes its output to: the identity of the
    /// output, whatever the path now names.
    pub output: Option<(u64, u64)>,
}

/// What the first signal of a stop found.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Sent {
    /// The recorded process is gone or is no longer that task's command: nothing was signalled.
    Gone,
    /// Members are alive but the kernel accepted no signal: nothing was stopped, try again.
    Refused,
    /// The kernel accepted the signal for at least one member seen alive.
    Accepted,
}

/// How a stop ended after an accepted signal, or what a host fixture is set up to do.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stop {
    Gone,
    /// The group was found empty after the signal.
    Ended,
    /// Part of the group is still running after the escalation.
    Survivors,
    Unsignalled,
}

/// Effects of the watchdog. The real host reads /proc and signals; tests fake it.
pub trait Host {
    /// The one live process of `session` that launched `task`: its output is the task's
    /// full output path and it started inside the task's launch window; none otherwise.
    fn resolve_owner(&self, session: &str, task: &TaskRef) -> Option<Owner>;
    /// The first signal of a stop to every live member of the owner's group, only while
    /// that very process (same start time) still writes the task's output file.
    fn signal(&self, owner: &Owner, task: &TaskRef) -> Sent;
    /// After an accepted signal: wait for the group to empty and escalate. `Ended` or
    /// `Survivors`; the caller has recorded the acceptance before it asks.
    fn finish_stop(&self, owner: &Owner, task: &TaskRef) -> Stop;
    /// True when nothing of the owner's group is left.
    fn group_gone(&self, owner: &Owner, task: &TaskRef) -> bool;
    /// Append the line saying why the command is ended to the owner's own output: what the
    /// writer reads. It is no receipt of anything.
    fn mark_stop(&self, owner: &Owner, task: &TaskRef, key: &str) -> bool;
    /// True while the line queued for `recipient` under `key` has not been delivered.
    fn pending(&self, recipient: &str, key: &str) -> bool;
    /// Queue one line for `recipient`; `key` makes a second queue of it a no-op.
    fn notify(&self, recipient: &str, key: &str, line: &str) -> io::Result<()>;
    /// End the live writer session of a BLOCKED round; true once it is stopped.
    fn end_session(&self, session: &str) -> bool;
}

/// What one tick did, for the log.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    Stopped {
        task_id: String,
        command: String,
    },
    StopPending {
        task_id: String,
    },
    /// The command finished by itself before the stop: no strike, no notice.
    Finished {
        task_id: String,
    },
    /// No recorded owner: nothing is signalled on a guess.
    OwnerUnknown {
        task_id: String,
    },
    Notified {
        task_id: String,
    },
    Blocked {
        round_key: String,
        command: String,
    },
    /// The live session of the BLOCKED round was stopped.
    RoundEnded {
        round_key: String,
    },
    /// The notices of a BLOCKED round stayed queued past the grace: the session was ended anyway.
    NoticesOverdue {
        round_key: String,
    },
}
