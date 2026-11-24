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
    /// Whether the process has live child processes (a running tool or shell);
    /// None when that cannot be read.
    fn has_children(&self, pid: u32) -> Option<bool>;
    /// Identity of the worktree directory itself (inode and git dir), so a replacement
    /// checkout at the same commit is told apart. None when it cannot be read.
    fn worktree_id(&self, cwd: &Path) -> Option<String>;
    /// A positive end-of-turn signal: Some(true) when the runtime wrote its turn-end marker
    /// after its last activity, Some(false) while a turn is open, None without a marker.
    fn turn_ended(&self, rec: &Record) -> Option<bool>;
    /// Length in bytes of the session's transcript, None when the runtime keeps none we can read.
    fn transcript_len(&self, rec: &Record) -> Option<u64>;
    /// Whether the transcript, from byte `from` on, holds `text` as one of its messages.
    fn transcript_contains(&self, rec: &Record, from: u64, text: &str) -> Option<bool>;
    /// Whether the live process `pid` (with this start time) was started with `text` as one
    /// of its arguments: the receipt of a prompt handed over on the command line.
    fn process_holds_prompt(&self, pid: u32, start: u64, text: &str) -> bool;
    /// A live process whose command line resumes `handle`: (pid, start time).
    fn find_process(&self, handle: &str) -> Option<(u32, u64)>;
    /// The start time of a process this tick just spawned, read even when it has already
    /// exited (an unreaped child keeps its pid, so the pair stays the launch's identity).
    fn launch_start(&self, pid: u32) -> Option<u64>;
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
