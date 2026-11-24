//! Everything the engine needs from the machine, behind one trait so tests run
//! the whole stop loop against a fake.

use crate::record::Record;
use std::path::Path;

/// What a Claude Code background job reports in `~/.claude/jobs/<job id>/state.json`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobState {
    /// `done`, `working`, `blocked`, `stopped`, ...
    pub state: String,
    /// Tasks and monitors still running inside the job.
    pub tasks: u64,
}

pub trait Host {
    fn now(&self) -> u64;
    /// Run to completion; Ok(stdout) or Err(reason).
    fn run(&mut self, cwd: Option<&Path>, argv: &[String]) -> Result<String, String>;
    /// The start time of a live process (its identity together with the pid), None when gone.
    fn proc_start(&self, pid: u32) -> Option<u64>;
    /// Whether the process has live child processes (a running tool or shell);
    /// None when that cannot be read.
    fn has_children(&self, pid: u32) -> Option<bool>;
    /// Identity of the worktree directory itself (inode and git dir), so a replacement
    /// checkout at the same commit is told apart. None when it cannot be read.
    fn worktree_id(&self, cwd: &Path) -> Option<String>;
    /// A positive end-of-turn signal: Some(true) when the runtime wrote its turn-end marker
    /// strictly after its last activity, Some(false) while a turn may be open, None without a marker.
    fn turn_ended(&self, rec: &Record) -> Option<bool>;
    fn mem_available_mb(&self) -> Option<u64>;
    fn git_head(&self, cwd: &Path) -> Option<String>;
    /// Last sign of life of the session (unix seconds), None when the runtime offers none.
    fn activity_ts(&self, rec: &Record) -> Option<u64>;
    /// The runtime's own state of a Claude job, None when unreadable.
    fn job_state(&self, rec: &Record) -> Option<JobState>;
    fn log(&mut self, line: &str);
}
