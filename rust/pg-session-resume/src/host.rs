//! Everything the engine needs from the machine, behind one trait so tests run
//! the whole stop/resume loop against a fake.

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
    /// Start and do not wait (Codex / OpenCode resume run for the whole fix round).
    /// Returns the pid of the started process.
    fn spawn(&mut self, cwd: &Path, argv: &[String]) -> Result<u32, String>;
    /// The start time of a live process (its identity together with the pid), None when gone.
    fn proc_start(&self, pid: u32) -> Option<u64>;
    fn mem_available_mb(&self) -> Option<u64>;
    fn git_head(&self, cwd: &Path) -> Option<String>;
    /// None when the path is no git worktree.
    fn git_dirty(&self, cwd: &Path) -> Option<bool>;
    /// Last sign of life of the session (unix seconds), None when the runtime offers none.
    fn activity_ts(&self, rec: &Record) -> Option<u64>;
    /// The runtime's own state of a Claude job, None when unreadable.
    fn job_state(&self, rec: &Record) -> Option<JobState>;
    fn log(&mut self, line: &str);
}
