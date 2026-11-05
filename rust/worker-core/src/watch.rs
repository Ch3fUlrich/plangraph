//! Event supervision primitives. Not connected to the harness until RL14.
use crate::{
    activity::{Ctx, Decision, Monitor, Signal},
    events::{BlindReason, ChannelHealth, EventParser},
    stall,
};
use std::{io, process::ExitStatus};

#[cfg(test)]
#[path = "watch_tests.rs"]
mod tests;
#[path = "watch_unix.rs"]
#[cfg(unix)]
#[allow(dead_code)] // RL12 supervisor integration follows in a separate slice.
mod unix;

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
    Survivors,
}

/// A backend owns the identity; callers cannot supply a pid to signal.
pub trait Child {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>>;
    fn terminate(&mut self) -> io::Result<()>;
    fn kill(&mut self) -> io::Result<()>;
    fn empty(&mut self) -> io::Result<bool>;
    fn reap(&mut self) -> io::Result<()>;
}

/// One clock-driven termination path, including cleanup after an I/O failure.
/// Signal errors do not skip escalation or become evidence of emptiness.
#[derive(Default)]
pub struct Stop {
    term_ms: Option<u64>,
    kill_ms: Option<u64>,
}
impl Stop {
    pub fn tick(&mut self, child: &mut dyn Child, now_ms: u64, grace_ms: u64) -> Option<KillProof> {
        if self.term_ms.is_none() {
            self.term_ms = Some(now_ms);
            let _ = child.terminate();
        }
        let _ = child.reap();
        if child.empty().unwrap_or(false) {
            return Some(KillProof::Empty);
        }
        if self.kill_ms.is_none() && now_ms.saturating_sub(self.term_ms?) >= grace_ms {
            self.kill_ms = Some(now_ms);
            let _ = child.kill();
        }
        if self
            .kill_ms
            .is_some_and(|t| now_ms.saturating_sub(t) >= 2000)
        {
            return Some(KillProof::Survivors);
        }
        None
    }
}
