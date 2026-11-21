//! Outcome probe: run before retrying an attempt to see what it left behind.

use crate::check::{run_check_scoped, END_MARKER};
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

/// Written by the wrapped check's own shell before the card's command runs. It
/// proves a start when the end marker is lost (a killed check prints none).
const STARTED: &str = ".pg-check-started";

/// Run `cmd` in a nested shell, record that it started, and after it print the end
/// marker ([`END_MARKER`]) as the last line and keep its exit code, whatever the
/// command did to its own tmp directory.
fn marked(cmd: &str) -> String {
    let quoted = format!("'{}'", cmd.replace('\'', "'\\''"));
    format!(
        ": > \"$TMPDIR/{STARTED}\"; sh -c {quoted}; rc=$?; printf '\\n{END_MARKER}%s\\n' \"$rc\"; exit \"$rc\""
    )
}

/// Whether the launcher and jail actually started the card's command. A launcher or
/// jail-setup failure (also a launcher that hangs until the timeout) leaves neither
/// the end marker nor the start file: a probe error, not a failed check.
fn started(tmp: &Path, r: &crate::check::CheckResult) -> bool {
    r.ended.is_some() || tmp.join(STARTED).exists()
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
    if let Some((_, _, tmp)) = jail {
        let _ = std::fs::remove_file(tmp.join(STARTED));
    }
    let r = run_check_scoped(&cmd, dir, timeout, tail_n, jail, &|| false)?;
    if let Some((_, _, tmp)) = jail {
        if !started(tmp, &r) {
            return Err(ProbeError::Check(std::io::Error::other(
                "the jailed check never started (launcher or jail setup failed)",
            )));
        }
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

    fn result(ended: Option<i32>, timed_out: bool) -> crate::check::CheckResult {
        crate::check::CheckResult {
            exit_code: Some(1),
            signal: None,
            timed_out,
            cancelled: false,
            tail: String::new(),
            output_hash: String::new(),
            ended,
        }
    }

    #[test]
    fn a_check_started_when_it_ended_or_left_its_start_file() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(started(tmp.path(), &result(Some(1), false)), "end marker");
        assert!(
            !started(tmp.path(), &result(None, false)),
            "launcher failure"
        );
        // A launcher that hangs until the timeout started nothing either.
        assert!(!started(tmp.path(), &result(None, true)));
        // A killed check that did start left its start file.
        std::fs::write(tmp.path().join(STARTED), "").unwrap();
        assert!(started(tmp.path(), &result(None, true)));
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
                text.trim_end().ends_with(&format!("{END_MARKER}{code}")),
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
