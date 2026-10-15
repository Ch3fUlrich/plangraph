//! Serialise merges into the shared repo with an advisory lock and a base recheck.
//!
//! Two workers can finish attempts at the same time and each tries to
//! fast-forward the main branch onto its own attempt. The merge itself is safe
//! to do in a throwaway worktree, but the branch ref must move atomically and
//! the base the caller rebased onto must still be current, so the whole
//! operation runs under a file lock shared through `<git-common-dir>/pg-merge.lock`.

use pg_worker_core::exit_class::ExitClass;
use pg_worker_core::git::{check_rev, git, GitError};
use std::fs::{File, OpenOptions};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Lock file name, inside the repository's common git dir.
const LOCK_NAME: &str = "pg-merge.lock";

/// How long to wait between `flock` attempts.
const RETRY: Duration = Duration::from_millis(50);

#[derive(Debug, thiserror::Error)]
pub enum MergeError {
    #[error("git error: {0}")]
    Git(#[from] GitError),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("timed out waiting for the merge lock")]
    LockTimeout,
}

/// Exclusive advisory lock over merges into one repository.
///
/// Held for the duration of a merge and released on drop, which matters because
/// a worker may return early on `BaseMoved` or an error and must never leak the
/// lock to the next worker.
#[derive(Debug)]
pub struct MergeLock {
    file: File,
}

impl MergeLock {
    /// Take the merge lock, waiting up to `timeout` for a competing worker.
    pub fn acquire(repo: &Path, timeout: Duration) -> Result<MergeLock, MergeError> {
        let path = git_common_dir(repo)?.join(LOCK_NAME);
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        let fd = file.as_raw_fd();
        let start = Instant::now();
        loop {
            // SAFETY: `fd` is a valid open descriptor owned by `file`, which
            // outlives the call; flock keeps no pointer past the call.
            let rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
            if rc == 0 {
                return Ok(MergeLock { file });
            }
            let err = std::io::Error::last_os_error();
            let busy = err.raw_os_error() == Some(libc::EWOULDBLOCK)
                || err.raw_os_error() == Some(libc::EAGAIN);
            if !busy {
                return Err(MergeError::Io(err));
            }
            if start.elapsed() >= timeout {
                return Err(MergeError::LockTimeout);
            }
            std::thread::sleep(RETRY);
        }
    }
}

impl Drop for MergeLock {
    fn drop(&mut self) {
        // Explicit unlock: closing the fd would release it too, but saying so
        // ties the release to `Drop` rather than to fd cleanup order.
        // SAFETY: `fd` is a valid descriptor owned by `self.file`.
        unsafe { libc::flock(self.file.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// What a merge attempt did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MergeOutcome {
    /// Main now points at this commit.
    Merged { sha: String },
    /// Main moved away from the expected base; the caller rebases and retries.
    BaseMoved,
    /// `--ff-only` could not apply the attempt; the caller rebases and retries.
    Conflict,
}

/// Merge `attempt_branch` into `main_branch`, but only if main is still at
/// `base_sha`. The lock serialises competing workers; the recheck makes a stale
/// base a no-op instead of a lost update.
pub fn merge_attempt(
    repo: &Path,
    main_branch: &str,
    attempt_branch: &str,
    base_sha: &str,
    lock_timeout: Duration,
) -> Result<MergeOutcome, MergeError> {
    check_rev(main_branch)?;
    check_rev(attempt_branch)?;
    check_rev(base_sha)?;

    let _lock = MergeLock::acquire(repo, lock_timeout)?;

    let main_ref = format!("refs/heads/{main_branch}");
    let head = git(repo, &["rev-parse", &main_ref])?;
    if head != base_sha {
        return Ok(MergeOutcome::BaseMoved);
    }

    // Merge in a throwaway detached worktree so the caller's checkout and index
    // are never disturbed; the branch ref moves only after the merge succeeds.
    let wt = unique_worktree_path();
    let wt_s = wt.to_string_lossy().into_owned();
    git(
        repo,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            "--",
            wt_s.as_str(),
            main_branch,
        ],
    )?;

    let result: Result<MergeOutcome, MergeError> = (|| {
        if git(&wt, &["merge", "--ff-only", attempt_branch]).is_err() {
            return Ok(MergeOutcome::Conflict);
        }
        let new = git(&wt, &["rev-parse", "HEAD"])?;
        // Compare-and-swap on the old value: if main moved under us the update
        // is refused instead of clobbering the other worker's merge.
        git(repo, &["update-ref", &main_ref, &new, &head])?;
        Ok(MergeOutcome::Merged { sha: new })
    })();

    remove_worktree(repo, &wt);
    result
}

/// Map a merge outcome to the exit class the orchestrator records.
pub fn merge_class(o: &MergeOutcome) -> Option<ExitClass> {
    match o {
        MergeOutcome::Merged { .. } => None,
        MergeOutcome::BaseMoved | MergeOutcome::Conflict => Some(ExitClass::RebaseConflict),
    }
}

/// The repo's common git dir, resolved against `repo` because `--git-common-dir`
/// may answer with a path relative to it (`.git` for a normal checkout).
fn git_common_dir(repo: &Path) -> Result<PathBuf, MergeError> {
    let raw = git(repo, &["rev-parse", "--git-common-dir"])?;
    let p = Path::new(&raw);
    Ok(if p.is_absolute() {
        p.to_path_buf()
    } else {
        repo.join(p)
    })
}

/// A path no other merge, in this process or another, will pick.
fn unique_worktree_path() -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    std::env::temp_dir().join(format!("pg-merge-wt-{}-{n}-{nanos}", std::process::id()))
}

/// Remove the scratch worktree, falling back to deleting the directory and
/// pruning git's metadata if `worktree remove` itself fails.
fn remove_worktree(repo: &Path, path: &Path) {
    let s = path.to_string_lossy().into_owned();
    if git(repo, &["worktree", "remove", "--force", s.as_str()]).is_err() {
        let _ = std::fs::remove_dir_all(path);
        let _ = git(repo, &["worktree", "prune"]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use tempfile::TempDir;

    /// Local git runner: worker-core's helper is `pub(crate)` there and its
    /// hermetic env only applies inside worker-core's own tests, so pin the
    /// identity here with `-c`.
    fn git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .args([
                "-c",
                "user.name=tester",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .current_dir(dir)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TERMINAL_PROMPT", "0")
            .stdin(Stdio::null())
            .output()
            .expect("git runs");
        assert!(
            out.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn write(dir: &Path, rel: &str, body: &str) {
        std::fs::write(dir.join(rel), body).unwrap();
    }

    fn commit_all(dir: &Path, msg: &str) -> String {
        git(dir, &["add", "-A"]);
        git(dir, &["commit", "-q", "-m", msg]);
        git(dir, &["rev-parse", "HEAD"])
    }

    fn repo() -> (TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        git(p, &["init", "-q", "-b", "main"]);
        write(p, "README.md", "hello\n");
        let sha = commit_all(p, "init");
        (dir, sha)
    }

    /// A branch named `name`, one commit ahead of `base`, built in a scratch
    /// worktree so the caller's checkout stays on `main` and untouched.
    fn attempt_branch(p: &Path, name: &str, file: &str, body: &str, base: &str) -> String {
        let scratch = tempfile::tempdir().unwrap();
        let wt = scratch.path().join("wt");
        let wt_s = wt.to_string_lossy().into_owned();
        git(p, &["worktree", "add", "-q", "-b", name, "--", wt_s.as_str(), base]);
        write(&wt, file, body);
        let head = commit_all(&wt, name);
        git(p, &["worktree", "remove", "--force", wt_s.as_str()]);
        head
    }

    #[test]
    fn ff_merge_moves_main() {
        let (r, base) = repo();
        let p = r.path();
        let head = attempt_branch(p, "attempt/x/1", "x.txt", "x", &base);
        let out = merge_attempt(p, "main", "attempt/x/1", &base, Duration::from_secs(5)).unwrap();
        assert_eq!(out, MergeOutcome::Merged { sha: head.clone() });
        assert_eq!(git(p, &["rev-parse", "refs/heads/main"]), head);
    }

    #[test]
    fn base_moved_after_base_is_not_merged() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/x/1", "x.txt", "x", &base);
        // main advances after the attempt recorded its base
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");
        let out = merge_attempt(p, "main", "attempt/x/1", &base, Duration::from_secs(5)).unwrap();
        assert_eq!(out, MergeOutcome::BaseMoved);
        assert_eq!(git(p, &["rev-parse", "refs/heads/main"]), moved);
        assert_eq!(merge_class(&out), Some(ExitClass::RebaseConflict));
    }

    #[test]
    fn diverged_history_is_not_merged() {
        let (r, base) = repo();
        let p = r.path();
        // An unrelated root commit shares no ancestry with main, so `--ff-only`
        // must refuse rather than produce a merge commit.
        git(p, &["checkout", "-q", "--orphan", "attempt/orphan/1"]);
        commit_all(p, "orphan");
        git(p, &["checkout", "-q", "main"]);
        let out =
            merge_attempt(p, "main", "attempt/orphan/1", &base, Duration::from_secs(5)).unwrap();
        assert!(!matches!(out, MergeOutcome::Merged { .. }), "{out:?}");
        assert!(
            matches!(out, MergeOutcome::Conflict | MergeOutcome::BaseMoved),
            "{out:?}"
        );
        assert_eq!(git(p, &["rev-parse", "refs/heads/main"]), base);
        assert_eq!(merge_class(&out), Some(ExitClass::RebaseConflict));
    }

    #[test]
    fn lock_is_exclusive_and_released_on_drop() {
        let (r, _base) = repo();
        let held = MergeLock::acquire(r.path(), Duration::from_secs(5)).unwrap();
        let err = MergeLock::acquire(r.path(), Duration::from_millis(150)).unwrap_err();
        assert!(matches!(err, MergeError::LockTimeout));
        drop(held);
        assert!(MergeLock::acquire(r.path(), Duration::from_secs(5)).is_ok());
    }

    #[test]
    fn concurrent_merges_serialise_to_one_winner() {
        let (r, base) = repo();
        let p = r.path().to_path_buf();
        attempt_branch(r.path(), "attempt/a/1", "a.txt", "a", &base);
        attempt_branch(r.path(), "attempt/b/1", "b.txt", "b", &base);

        let mut handles = Vec::new();
        for br in ["attempt/a/1", "attempt/b/1"] {
            let p = p.clone();
            let base = base.clone();
            handles.push(std::thread::spawn(move || {
                merge_attempt(&p, "main", br, &base, Duration::from_secs(10)).unwrap()
            }));
        }
        let outs: Vec<MergeOutcome> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        let merged = outs
            .iter()
            .filter(|o| matches!(o, MergeOutcome::Merged { .. }))
            .count();
        let moved = outs
            .iter()
            .filter(|o| matches!(o, MergeOutcome::BaseMoved))
            .count();
        assert_eq!((merged, moved), (1, 1), "{outs:?}");
        assert_ne!(git(r.path(), &["rev-parse", "refs/heads/main"]), base);
    }
}
