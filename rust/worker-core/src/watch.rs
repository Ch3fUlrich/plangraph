//! Event supervision primitives. Not connected to the harness until RL14.
use crate::{
    activity::{Ctx, Decision, Monitor, Signal},
    events::{BlindReason, ChannelHealth, EventParser},
    stall,
};
use std::{io, process::ExitStatus};
#[cfg(any(windows, test))]
#[path = "watch_windows.rs"]
pub mod windows;

#[cfg(all(test, unix))]
#[path = "watch_blind_tests.rs"]
mod blind_tests;
#[path = "watch_supervise.rs"]
#[cfg(unix)]
mod supervise;
#[cfg(all(test, unix))]
#[path = "watch_tests.rs"]
mod tests;
#[path = "watch_unix.rs"]
#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use supervise::supervise_events;

pub struct Hooks<'a> {
    pub parser: &'a mut dyn EventParser,
    pub monitor: &'a mut Monitor,
    pub cancelled: &'a dyn Fn() -> bool,
    pub tree_sample: &'a dyn Fn() -> Ctx,
    pub cpu: Option<&'a mut dyn FnMut(u64) -> Option<Signal>>,
    pub stdin: &'a [u8],
    pub channel: &'a mut ChannelHealth,
}

pub struct Watched {
    pub io_error: Option<io::Error>,
    pub run: stall::Supervised,
    pub decisions: Vec<Decision>,
    pub activity: Option<u64>,
    pub channel: ChannelSummary,
    pub proof: KillProof,
    pub stall_bytes: bool,
    pub late_signal_ms: Option<u64>,
}

pub struct ChannelSummary {
    pub blind: Option<BlindReason>,
    pub lines: u64,
    pub bad: u64,
    pub blind_events: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KillProof {
    NotNeeded,
    Empty,
    Survivors { cause: Option<io::ErrorKind> },
}

/// A backend owns the identity; callers cannot supply a pid to signal.
pub trait Child {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>>;
    fn terminate(&mut self) -> io::Result<()>;
    fn kill(&mut self) -> io::Result<()>;
    fn empty(&mut self) -> io::Result<bool>;
    fn reap(&mut self) -> io::Result<Option<ExitStatus>>;
}

/// One clock-driven termination path, including cleanup after an I/O failure.
/// Signal errors do not skip escalation or become evidence of emptiness.
#[derive(Default)]
pub struct Stop {
    status: Option<ExitStatus>,
    term_ms: Option<u64>,
    kill_ms: Option<u64>,
    cause: Option<io::ErrorKind>,
}
impl Stop {
    pub fn tick(
        &mut self,
        child: &mut dyn Child,
        now_ms: u64,
        grace_ms: u64,
        prove_ms: u64,
    ) -> Option<KillProof> {
        match child.reap() {
            Ok(status) => {
                let exited = self.status.is_none() && status.is_some();
                self.status = self.status.or(status);
                if exited && self.term_ms.is_some() && self.empty(child) {
                    return Some(KillProof::Empty);
                }
            }
            Err(error) => self.record(Err(error)),
        }
        if self.term_ms.is_none() {
            self.term_ms = Some(now_ms);
            if self.empty(child) {
                return Some(KillProof::Empty);
            }
            self.record(child.terminate());
        }
        if self.kill_ms.is_none() && now_ms.saturating_sub(self.term_ms?) >= grace_ms {
            if self.empty(child) {
                return Some(KillProof::Empty);
            }
            self.kill_ms = Some(now_ms);
            self.record(child.kill());
        }
        if self
            .kill_ms
            .is_some_and(|t| now_ms.saturating_sub(t) >= prove_ms)
        {
            match child.empty() {
                Ok(true) => return Some(KillProof::Empty),
                Ok(false) => (),
                Err(error) => self.record(Err(error)),
            }
            return Some(KillProof::Survivors { cause: self.cause });
        }
        None
    }
    fn record(&mut self, result: io::Result<()>) {
        if let Err(error) = result {
            self.cause.get_or_insert(error.kind());
        }
    }
    fn empty(&mut self, child: &mut dyn Child) -> bool {
        match child.empty() {
            Ok(empty) => empty,
            Err(error) => {
                self.record(Err(error));
                false
            }
        }
    }
}
