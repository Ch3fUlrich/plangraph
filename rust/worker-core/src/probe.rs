//! Outcome probe: run before retrying an attempt to see what it left behind.

use crate::check::run_check_scoped;
use crate::git::{check_rev, git, GitError};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::Duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeOutcome {
    /// Commits beyond base and the check passes: nothing left to do.
    AlreadyDone,
    /// Commits beyond base but the check fails: resume from them.
    Partial,
    /// No commits beyond base.
    Nothing,
}

#[derive(Debug, thiserror::Error)]
pub enum ProbeError {
    #[error(transparent)]
    Git(#[from] GitError),
    #[error("check could not run: {0}")]
    Check(#[from] std::io::Error),
}

/// Last output line of a jailed check whose launcher and jail started it. The
/// wrapper prints it after the card's command, whatever that command did to its
/// own tmp directory or exit code, so the card cannot erase the evidence that it ran.
const DONE: &str = "PG-CHECK-DONE:";

/// Run `cmd` in a nested shell, then report that it ran and keep its exit code.
fn marked(cmd: &str) -> String {
    let quoted = format!("'{}'", cmd.replace('\'', "'\\''"));
    format!("sh -c {quoted}; rc=$?; printf '\\n{DONE}%s\\n' \"$rc\"; exit \"$rc\"")
}

/// A jailed check never started when no end marker is in its output, unless it was
/// killed (a timeout or cancel only happens to something that ran). A launcher or
/// jail-setup failure prints no marker: it is a probe error, not a failed check.
fn never_started(r: &crate::check::CheckResult) -> bool {
    let ended = r
        .tail
        .lines()
        .rev()
        .find(|l| !l.trim().is_empty())
        .is_some_and(|l| l.starts_with(DONE));
    !ended && !r.timed_out && !r.cancelled
}

/// Number of commits on HEAD of `dir` beyond `base`.
pub fn commits_beyond(dir: &Path, base: &str) -> Result<u32, GitError> {
    check_rev(base)?;
    let n = git(dir, &["rev-list", "--count", &format!("{base}..HEAD")])?;
    Ok(n.parse().unwrap_or(0))
}

/// Probe an attempt worktree. `dir` should be the attempt's worktree with its
/// branch checked out; the check runs against what is committed and present there.
pub fn probe(
    dir: &Path,
    base: &str,
    check_cmd: &str,
    timeout: Duration,
    tail_n: usize,
) -> Result<ProbeOutcome, ProbeError> {
    probe_scoped(dir, base, check_cmd, timeout, tail_n, None)
}

/// [`probe`] with the attempt's jail (helper, limits, private tmp) around the
/// check. A configured jail whose helper is missing is an error: the check never
/// runs unjailed.
pub fn probe_scoped(
    dir: &Path,
    base: &str,
    check_cmd: &str,
    timeout: Duration,
    tail_n: usize,
    jail: Option<(&Path, &crate::scope::ScopeLimits, &Path)>,
) -> Result<ProbeOutcome, ProbeError> {
    if commits_beyond(dir, base)? == 0 {
        return Ok(ProbeOutcome::Nothing);
    }
    let cmd = if jail.is_some() {
        marked(check_cmd)
    } else {
        check_cmd.to_string()
    };
    let r = run_check_scoped(&cmd, dir, timeout, tail_n, jail, &|| false)?;
    if jail.is_some() && never_started(&r) {
        return Err(ProbeError::Check(std::io::Error::other(
            "the jailed check never started (launcher or jail setup failed)",
        )));
    }
    Ok(if r.passed() {
        ProbeOutcome::AlreadyDone
    } else {
        ProbeOutcome::Partial
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::{commit_all, repo, write};

    const T: Duration = Duration::from_secs(20);

    fn result(code: i32, tail: &str) -> crate::check::CheckResult {
        crate::check::CheckResult {
            exit_code: Some(code),
            signal: None,
            timed_out: false,
            cancelled: false,
            tail: tail.into(),
            output_hash: String::new(),
        }
    }

    #[test]
    fn only_an_output_without_the_end_marker_never_started() {
        assert!(!never_started(&result(1, "boom\nPG-CHECK-DONE:1\n")));
        assert!(!never_started(&result(0, "PG-CHECK-DONE:0")));
        assert!(never_started(&result(78, "pg-jail: unsupported")));
        assert!(never_started(&result(1, "Failed to connect to bus")));
        // The marker must be the last line: a card printing it early proves nothing.
        assert!(never_started(&result(1, "PG-CHECK-DONE:0\nlater output")));
        // A killed check ran.
        let mut killed = result(1, "");
        killed.timed_out = true;
        assert!(!never_started(&killed));
    }

    #[cfg(unix)]
    #[test]
    fn the_wrapper_keeps_the_exit_code_and_marks_a_check_that_wiped_its_tmp() {
        let tmp = tempfile::tempdir().unwrap();
        for (cmd, code) in [
            ("exit 3", 3),
            ("true", 0),
            ("echo it\\'s; rm -rf \"$TMPDIR\"/* ; exit 1", 1),
        ] {
            let out = std::process::Command::new("sh")
                .arg("-c")
                .arg(marked(cmd))
                .env("TMPDIR", tmp.path())
                .output()
                .unwrap();
            assert_eq!(out.status.code(), Some(code), "{cmd}");
            let text = String::from_utf8_lossy(&out.stdout);
            assert!(
                text.trim_end().ends_with(&format!("{DONE}{code}")),
                "{cmd}: {text:?}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_jailed_check_that_never_starts_is_an_error_not_a_partial() {
        let (d, base) = repo();
        write(d.path(), "work.txt", "x");
        commit_all(d.path(), "work");
        let tmp = tempfile::tempdir().unwrap();
        // The helper exists but refuses (as an unsupported kernel does); whether the
        // launcher in front of it is missing, broken or working, the check never runs.
        let bin = tmp.path().join("refusing-jail");
        std::fs::write(
            &bin,
            "#!/bin/sh\necho 'pg-jail: unsupported' >&2\nexit 78\n",
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .unwrap();
        let limits = crate::scope::ScopeLimits::default();
        let r = probe_scoped(
            d.path(),
            &base,
            "test -f work.txt",
            T,
            10,
            Some((&bin, &limits, tmp.path())),
        );
        assert!(matches!(r, Err(ProbeError::Check(_))), "{r:?}");
    }

    #[test]
    fn nothing_without_commits_even_if_dirty() {
        let (d, base) = repo();
        write(d.path(), "wip.txt", "x");
        assert_eq!(
            probe(d.path(), &base, "true", T, 10).unwrap(),
            ProbeOutcome::Nothing
        );
    }

    #[test]
    fn already_done_when_check_passes() {
        let (d, base) = repo();
        write(d.path(), "done.txt", "x");
        commit_all(d.path(), "work");
        assert_eq!(commits_beyond(d.path(), &base).unwrap(), 1);
        let out = probe(d.path(), &base, "test -f done.txt", T, 10).unwrap();
        assert_eq!(out, ProbeOutcome::AlreadyDone);
    }

    #[test]
    fn partial_when_check_fails() {
        let (d, base) = repo();
        write(d.path(), "half.txt", "x");
        commit_all(d.path(), "work");
        let out = probe(d.path(), &base, "test -f other.txt", T, 10).unwrap();
        assert_eq!(out, ProbeOutcome::Partial);
    }

    #[test]
    fn bad_base_is_an_error() {
        let (d, _) = repo();
        assert!(probe(d.path(), "deadbeef", "true", T, 10).is_err());
    }
}
