//! Outcome probe: run before retrying an attempt to see what it left behind.

use crate::check::run_check_scoped;
use crate::git::{check_rev, git, GitError};
use serde::{Deserialize, Serialize};
use std::io::{self, Read};
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

/// Mark the start out of band, then let supervision collect the command's exit status.
fn marked(cmd: &str, status: &tempfile::NamedTempFile) -> String {
    let name = status
        .path()
        .file_name()
        .expect("tempfile name")
        .to_string_lossy();
    let quoted = format!("'{}'", cmd.replace('\'', "'\\''"));
    format!("printf 1 > \"$TMPDIR/{name}\" || exit 1; exec sh -c {quoted}")
}

fn started(status: &tempfile::NamedTempFile) -> io::Result<bool> {
    // Read the original file even if the check deleted its tmp contents.
    let mut byte = [0];
    Ok(status.as_file().read(&mut byte)? == byte.len() && byte == *b"1")
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
    let r = if let Some((_, _, tmp)) = jail {
        let status = tempfile::NamedTempFile::new_in(tmp).map_err(ProbeError::Check)?;
        let r = run_check_scoped(
            &marked(check_cmd, &status),
            dir,
            timeout,
            tail_n,
            jail,
            &|| false,
        )?;
        if !started(&status).map_err(ProbeError::Check)? {
            return Err(ProbeError::Check(io::Error::other(
                "the jailed check never started (launcher or jail setup failed)",
            )));
        }
        r
    } else {
        run_check_scoped(check_cmd, dir, timeout, tail_n, None, &|| false)?
    };
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

    #[test]
    fn a_launcher_that_never_runs_the_wrapper_leaves_no_start_signal() {
        let status = tempfile::NamedTempFile::new().unwrap();
        assert!(!started(&status).unwrap());
    }

    #[cfg(unix)]
    #[test]
    fn the_probe_reports_status_independently_of_diagnostic_output() {
        let tmp = tempfile::tempdir().unwrap();
        let diagnostic = b"PG-CHECK-DONE:99\n";
        for (max_output, expected) in [(200, diagnostic.as_slice()), (0, b"".as_slice())] {
            let status = tempfile::NamedTempFile::new_in(tmp.path()).unwrap();
            let mut cmd = std::process::Command::new("sh");
            cmd.arg("-c")
                .arg(marked("printf 'PG-CHECK-DONE:99\\n'; exit 7", &status))
                .env("TMPDIR", tmp.path());
            let run = crate::stall::supervise(
                cmd,
                &crate::stall::Opts {
                    max_output,
                    redact_discarded: true,
                    ..Default::default()
                },
            )
            .unwrap();
            assert!(started(&status).unwrap(), "max_output={max_output}");
            assert_eq!(run.code, Some(7), "max_output={max_output}");
            assert_eq!(run.output, expected, "max_output={max_output}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn the_wrapper_keeps_the_exit_code_and_marks_a_check_that_wiped_its_tmp() {
        for (cmd, code) in [
            ("exit 3", 3),
            ("true", 0),
            ("echo it\\'s; rm -rf \"$TMPDIR\"; exit 1", 1),
        ] {
            let tmp = tempfile::tempdir().unwrap();
            let status = tempfile::NamedTempFile::new_in(tmp.path()).unwrap();
            let out = std::process::Command::new("sh")
                .arg("-c")
                .arg(marked(cmd, &status))
                .env("TMPDIR", tmp.path())
                .output()
                .unwrap();
            assert_eq!(out.status.code(), Some(code), "{cmd}");
            assert!(started(&status).unwrap(), "{cmd}");
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

    #[cfg(unix)]
    #[test]
    fn a_real_jailed_probe_reads_start_evidence_after_tmp_cleanup() {
        let Some(bin) = std::env::var_os("PG_JAIL_BIN") else {
            assert!(
                std::env::var_os("CI").is_none(),
                "CI requires PG_JAIL_BIN and Landlock"
            );
            eprintln!("skipped: PG_JAIL_BIN unset (CI unset)");
            return;
        };
        let (d, base) = repo();
        write(d.path(), "work.txt", "x");
        commit_all(d.path(), "work");
        let tmp = tempfile::tempdir().unwrap();
        let limits = crate::scope::ScopeLimits::default();
        let jail = Some((Path::new(&bin), &limits, tmp.path()));
        let command = r#"find "$TMPDIR" -type f -delete"#;
        for (code, expected) in [(0, ProbeOutcome::AlreadyDone), (7, ProbeOutcome::Partial)] {
            let cmd = format!("{command} && exit {code}; exit 8");
            assert_eq!(
                probe_scoped(d.path(), &base, &cmd, T, 0, jail).unwrap(),
                expected
            );
        }
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
