//! Everything the engine needs from the machine, behind one trait so tests run
//! the whole stop/resume loop against a fake.

use crate::record::Record;
use std::path::Path;

pub trait Host {
    fn now(&self) -> u64;
    /// Run to completion; Ok(stdout) or Err(reason).
    fn run(&mut self, cwd: Option<&Path>, argv: &[String]) -> Result<String, String>;
    /// Start and do not wait (Codex / OpenCode resume run for the whole fix round).
    fn spawn(&mut self, cwd: &Path, argv: &[String]) -> Result<(), String>;
    fn mem_available_mb(&self) -> Option<u64>;
    fn git_head(&self, cwd: &Path) -> Option<String>;
    /// None when the path is no git worktree.
    fn git_dirty(&self, cwd: &Path) -> Option<bool>;
    /// Last sign of life: the transcript mtime, else None.
    fn activity_ts(&self, rec: &Record) -> Option<u64>;
    fn log(&mut self, line: &str);
}
