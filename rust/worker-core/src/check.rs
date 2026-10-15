//! Run a card's check command and keep the tail of its output.

use crate::hash::hash_bytes;
use crate::stall::{supervise, Opts};
use serde::{Deserialize, Serialize};
use std::io;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckResult {
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub timed_out: bool,
    /// Last N lines of combined stdout+stderr.
    pub tail: String,
    /// Hash of `tail`, for "identical failure" comparisons.
    pub output_hash: String,
}

impl CheckResult {
    pub fn passed(&self) -> bool {
        self.exit_code == Some(0) && !self.timed_out
    }
}

/// Last `n` lines of `text`.
pub fn tail_lines(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

/// Run `cmd` via `sh -c` in `dir`; kill it after `timeout`.
pub fn run_check(
    cmd: &str,
    dir: &Path,
    timeout: Duration,
    tail_n: usize,
) -> io::Result<CheckResult> {
    let mut c = Command::new("sh");
    c.arg("-c").arg(cmd).current_dir(dir);
    let opts = Opts {
        timeout: Some(timeout),
        grace: Duration::from_secs(2),
        ..Opts::default()
    };
    let r = supervise(c, &opts)?;
    let tail = tail_lines(&String::from_utf8_lossy(&r.output), tail_n);
    Ok(CheckResult {
        exit_code: r.code,
        signal: r.signal,
        timed_out: r.timed_out,
        output_hash: hash_bytes(tail.as_bytes()),
        tail,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const T: Duration = Duration::from_secs(20);

    #[test]
    fn pass_and_fail_codes() {
        let d = tempfile::tempdir().unwrap();
        let ok = run_check("echo fine", d.path(), T, 50).unwrap();
        assert!(ok.passed());
        assert_eq!(ok.tail, "fine");
        let bad = run_check("echo nope; exit 7", d.path(), T, 50).unwrap();
        assert!(!bad.passed());
        assert_eq!(bad.exit_code, Some(7));
    }

    #[test]
    fn runs_in_dir_and_combines_streams() {
        let d = tempfile::tempdir().unwrap();
        std::fs::write(d.path().join("marker"), "").unwrap();
        let r = run_check("ls marker; echo e >&2", d.path(), T, 50).unwrap();
        assert!(r.tail.contains("marker") && r.tail.contains('e'));
    }

    #[test]
    fn keeps_only_last_n_lines() {
        let d = tempfile::tempdir().unwrap();
        let r = run_check("seq 1 200", d.path(), T, 50).unwrap();
        let lines: Vec<&str> = r.tail.lines().collect();
        assert_eq!(lines.len(), 50);
        assert_eq!(lines[0], "151");
        assert_eq!(lines[49], "200");
        assert_eq!(tail_lines("a\nb", 5), "a\nb");
    }

    #[test]
    fn timeout_is_reported() {
        let d = tempfile::tempdir().unwrap();
        let r = run_check(
            "echo start; sleep 30",
            d.path(),
            Duration::from_millis(300),
            50,
        )
        .unwrap();
        assert!(r.timed_out && !r.passed());
        assert!(r.tail.contains("start"));
    }

    #[test]
    fn same_output_same_hash() {
        let d = tempfile::tempdir().unwrap();
        let a = run_check("echo boom; exit 1", d.path(), T, 50).unwrap();
        let b = run_check("echo boom; exit 1", d.path(), T, 50).unwrap();
        let c = run_check("echo other; exit 1", d.path(), T, 50).unwrap();
        assert_eq!(a.output_hash, b.output_hash);
        assert_ne!(a.output_hash, c.output_hash);
    }
}
