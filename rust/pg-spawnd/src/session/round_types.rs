//! The vocabulary of the round watchdog: its limits, its host effects and its log events.

use super::round_wait::TaskRef;
use std::io;

/// Longest wait on one background command before the watchdog ends it.
pub const WAIT_LIMIT_MS: i64 = 15 * 60_000;
/// Waits ended in one round before the round is BLOCKED.
pub const STRIKE_LIMIT: usize = 2;
/// The exit code of a command ended by SIGHUP, the first signal of a stop: the harness's
/// report of it is the receipt of our own signal. Independent ends are almost always 143 or 137.
pub const OUR_SIGNAL_EXIT: i64 = 129;
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

/// What a stop found.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stop {
    /// The recorded process is gone or is no longer that task's command: nothing was signalled.
    Gone,
    /// The watchdog signalled it and nothing of it is left.
    Ended,
    /// The watchdog signalled it and part of it is still running.
    Survivors,
    /// Members are alive but the kernel accepted no signal: nothing was stopped, try again.
    Unsignalled,
}

/// Effects of the watchdog. The real host reads /proc and signals; tests fake it.
pub trait Host {
    /// The one live process of `session` that launched `task`: its output is the task's
    /// full output path and it started inside the task's launch window; none otherwise.
    fn resolve_owner(&self, session: &str, task: &TaskRef) -> Option<Owner>;
    /// End the recorded owner's process group, only while that very process (same start
    /// time) still writes the task's output file. `signalled` says an earlier stop left
    /// survivors. `Ended` needs evidence: live members were signalled and the group was
    /// then found empty, or the group was empty after an earlier signal.
    fn stop(&self, owner: &Owner, task: &TaskRef, signalled: bool) -> Stop;
    /// Append the line saying why the command is ended to the task's own output file, the
    /// owner's durable acknowledgement that this stop reached its signal stage.
    fn mark_stop(&self, owner: &Owner, task: &TaskRef, key: &str) -> bool;
    /// True when the output file of the task carries the line `mark_stop` wrote for `key`.
    fn has_mark(&self, owner: &Owner, task: &TaskRef, key: &str) -> bool;
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
