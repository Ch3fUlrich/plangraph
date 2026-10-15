//! Janitor: remove worktrees and branches of dead attempts, after probing them.
//!
//! Only branches that parse as `attempt/<card>/<n>` are ever touched.

use crate::git::{git, GitError};
use crate::probe::{probe, ProbeOutcome};
use crate::worktree::{list, remove, AttemptId};
use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

pub struct ProbeSpec<'a> {
    pub check_cmd: &'a str,
    pub timeout: Duration,
    pub tail_lines: usize,
    /// Base sha an attempt was started from.
    pub base_for: &'a dyn Fn(&AttemptId) -> String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Removed {
    pub id: AttemptId,
    /// Branch head at removal, so the work stays recoverable from the object store.
    pub head: String,
    pub outcome: ProbeOutcome,
}

#[derive(Debug, Default)]
pub struct Report {
    pub removed: Vec<Removed>,
    /// Dead attempts left in place because probing or removal failed.
    pub failed: Vec<(AttemptId, String)>,
}

fn attempt_branches(repo: &Path) -> Result<Vec<(AttemptId, String)>, GitError> {
    let out = git(
        repo,
        &[
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/attempt/",
        ],
    )?;
    Ok(out
        .lines()
        .filter_map(|l| {
            let (name, sha) = l.split_once(' ')?;
            Some((AttemptId::parse_branch(name)?, sha.to_string()))
        })
        .collect())
}

/// Probe and remove every attempt not in `live`. Attempts whose probe fails are
/// kept and reported, never removed blindly. Branches without a worktree are
/// classified by commit count only (no check can run there).
pub fn sweep(repo: &Path, live: &HashSet<AttemptId>, spec: &ProbeSpec) -> Result<Report, GitError> {
    let worktrees = list(repo)?;
    let mut report = Report::default();
    for (id, head) in attempt_branches(repo)? {
        if live.contains(&id) {
            continue;
        }
        let base = (spec.base_for)(&id);
        let wt = worktrees.iter().find(|w| w.id == id);
        let outcome = match wt {
            Some(w) => probe(
                &w.path,
                &base,
                spec.check_cmd,
                spec.timeout,
                spec.tail_lines,
            )
            .map_err(|e| e.to_string()),
            None => commits_beyond_branch(repo, &base, &id.branch()).map_err(|e| e.to_string()),
        };
        let outcome = match outcome {
            Ok(o) => o,
            Err(e) => {
                report.failed.push((id, e));
                continue;
            }
        };
        let res = match wt {
            Some(w) => remove(repo, &w.path),
            None => Ok(()),
        }
        .and_then(|_| git(repo, &["branch", "-D", &id.branch()]).map(|_| ()));
        match res {
            Ok(()) => report.removed.push(Removed { id, head, outcome }),
            Err(e) => report.failed.push((id, e.to_string())),
        }
    }
    git(repo, &["worktree", "prune"])?;
    Ok(report)
}

fn commits_beyond_branch(repo: &Path, base: &str, branch: &str) -> Result<ProbeOutcome, GitError> {
    let n = git(repo, &["rev-list", "--count", &format!("{base}..{branch}")])?;
    Ok(if n.parse::<u32>().unwrap_or(0) == 0 {
        ProbeOutcome::Nothing
    } else {
        ProbeOutcome::Partial
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::{commit_all, repo, write};
    use crate::worktree::create;

    fn branches(repo: &Path) -> Vec<String> {
        let s = git(repo, &["branch", "--format=%(refname:short)"]).unwrap();
        let mut v: Vec<String> = s.lines().map(String::from).collect();
        v.sort();
        v
    }

    #[test]
    fn removes_dead_keeps_live_and_foreign_branches() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let p = r.path();
        let (live, dead_done, dead_nothing) = (
            AttemptId::new("c", 1),
            AttemptId::new("c", 2),
            AttemptId::new("d", 1),
        );
        let wl = create(p, &base, &root.path().join("live"), &live).unwrap();
        let wd = create(p, &base, &root.path().join("done"), &dead_done).unwrap();
        create(p, &base, &root.path().join("none"), &dead_nothing).unwrap();
        write(&wd.path, "f.txt", "x");
        commit_all(&wd.path, "work");
        write(&wl.path, "g.txt", "x"); // live work must be untouched
                                       // branches that are not attempts must survive
        git(p, &["branch", "feature/x"]).unwrap();
        git(p, &["branch", "attempt/malformed"]).unwrap();
        git(p, &["branch", "attempt-ish/c/9"]).unwrap();
        // orphan attempt branch without worktree
        git(p, &["branch", "attempt/orphan/3"]).unwrap();

        let live_set: HashSet<_> = [live.clone()].into();
        let b = base.clone();
        let base_for = move |_: &AttemptId| b.clone();
        let spec = ProbeSpec {
            check_cmd: "test -f f.txt",
            timeout: Duration::from_secs(20),
            tail_lines: 10,
            base_for: &base_for,
        };
        let rep = sweep(p, &live_set, &spec).unwrap();
        assert!(rep.failed.is_empty(), "{:?}", rep.failed);
        let mut got: Vec<(String, ProbeOutcome)> = rep
            .removed
            .iter()
            .map(|r| (r.id.to_string(), r.outcome))
            .collect();
        got.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(
            got,
            vec![
                ("c#2".to_string(), ProbeOutcome::AlreadyDone),
                ("d#1".to_string(), ProbeOutcome::Nothing),
                ("orphan#3".to_string(), ProbeOutcome::Nothing),
            ]
        );
        assert!(rep.removed.iter().find(|r| r.id == dead_done).unwrap().head != base);

        assert!(wl.path.join("g.txt").exists());
        assert!(!wd.path.exists());
        assert_eq!(
            branches(p),
            [
                "attempt-ish/c/9",
                "attempt/c/1",
                "attempt/malformed",
                "feature/x",
                "main"
            ]
        );
        // idempotent
        assert!(sweep(p, &live_set, &spec).unwrap().removed.is_empty());
    }

    #[test]
    fn probe_failure_keeps_attempt() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c", 1);
        let w = create(r.path(), &base, &root.path().join("w"), &id).unwrap();
        let bad = |_: &AttemptId| "0000000000000000000000000000000000000000".to_string();
        let spec = ProbeSpec {
            check_cmd: "true",
            timeout: Duration::from_secs(5),
            tail_lines: 5,
            base_for: &bad,
        };
        let rep = sweep(r.path(), &HashSet::new(), &spec).unwrap();
        assert!(rep.removed.is_empty());
        assert_eq!(rep.failed.len(), 1);
        assert!(w.path.exists());
    }
}
