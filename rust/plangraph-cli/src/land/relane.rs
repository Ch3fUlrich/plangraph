//! `land-train relane`: after a train merge, one open lane at a time is fetched into its own ref,
//! rebased onto the new main in its own worktree, checked and (on request) pushed as the next `-rN` branch.
//! A lane is never read through FETCH_HEAD, which parallel fetches overwrite. A patch already on main is
//! dropped, not a conflict; a conflict leaves the lane ref and the remote untouched.
use super::git;
use super::relane_git::{
    foreign, merge_with_own_edits, own_commits, patch_ids, plain_run, push_lock,
    rebase_in_worktree, remote_branch,
};
use serde::Serialize;
use std::path::Path;

/// What one lane needs: the branch, and what the registry recorded for it.
pub struct Lane<'a> {
    pub repo: &'a Path,
    pub remote: &'a str,
    /// `refs/heads/<branch>` on the remote, e.g. `lane/batch1-r1`.
    pub branch: &'a str,
    /// The new main, or the new head of the lane's open parent; every own commit ends up on top of it.
    pub onto: &'a str,
    /// The head the registry recorded for the lane: a different fetched head is a mixed-up fetch.
    pub expected_head: &'a str,
    /// Text every own commit's `Writer` trailer must contain; a commit of another writer is foreign.
    pub writer: &'a str,
    /// The recorded head of the open parent of a stacked lane: the lane's own commits start after it.
    pub parent_head: Option<&'a str>,
    pub push: bool,
}

/// The record a later step (seat evidence, probe PR) needs: old head, new head and patch-id equality.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Report {
    pub lane: String,
    pub old_head: String,
    pub new_head: String,
    /// The own commits keep the same patches in the same order (nothing dropped, nothing changed).
    pub patch_ids_equal: bool,
    pub own_before: usize,
    pub own_after: usize,
    /// The branch the new head was pushed to, when it was.
    pub pushed: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The lane already contains the new main: nothing to rebase.
    Current(Report),
    Rebased(Report),
    /// The rebase stopped on these unmerged paths; the lane is untouched.
    Conflict {
        lane: String,
        files: Vec<String>,
    },
    Refused {
        lane: String,
        reason: String,
    },
    /// An environment failure (fetch, git): the lane may work on a later run, so the settlement stays pending.
    Retry {
        lane: String,
        reason: String,
    },
}

impl Outcome {
    pub fn line(&self) -> String {
        match self {
            Outcome::Current(r) => format!("CURRENT {} {}", r.lane, r.new_head),
            Outcome::Rebased(r) => format!(
                "REBASED {} {} -> {} patch_ids_equal={} own {}->{}{}",
                r.lane,
                r.old_head,
                r.new_head,
                r.patch_ids_equal,
                r.own_before,
                r.own_after,
                r.pushed
                    .as_ref()
                    .map(|p| format!(" pushed {p}"))
                    .unwrap_or_default()
            ),
            Outcome::Conflict { lane, files } => {
                format!("CONFLICT {lane} {}", files.join(" "))
            }
            Outcome::Refused { lane, reason } => format!("REFUSED {lane} {reason}"),
            Outcome::Retry { lane, reason } => format!("RETRY {lane} {reason}"),
        }
    }
    pub fn is_ok(&self) -> bool {
        matches!(self, Outcome::Current(_) | Outcome::Rebased(_))
    }
}

/// `lane/x-r4` -> `lane/x-r5`; a name without a counter gets `-r2`.
pub fn next_branch(branch: &str) -> String {
    if let Some((stem, n)) = branch.rsplit_once("-r") {
        if let Ok(n) = n.parse::<u64>() {
            return format!("{stem}-r{}", n + 1);
        }
    }
    format!("{branch}-r2")
}

/// Fetch and result refs live in disjoint namespaces: `lane/x-r1` writes the result `lane/x-r2` while
/// `lane/x-r2` is fetched, and the two must never be the same ref.
fn fetch_ref(branch: &str) -> String {
    format!("refs/po/fetch/{branch}")
}

fn result_ref(branch: &str) -> String {
    format!("refs/po/result/{branch}")
}

fn refuse(lane: &str, reason: impl Into<String>) -> Result<Outcome, String> {
    Ok(Outcome::Refused {
        lane: lane.into(),
        reason: reason.into(),
    })
}

/// How many successor names (`-r2`, `-r3` ...) are passed over before a lane is given up on.
const MAX_SUCCESSORS: usize = 20;

/// Rebases one lane. `Err` is an environment failure (fetch, git); a lane that cannot be relaned is an
/// `Outcome` and stays as it was.
pub fn relane(l: &Lane) -> Result<Outcome, String> {
    let repo = l.repo;
    git::out(
        repo,
        &["check-ref-format", &format!("refs/heads/{}", l.branch)],
    )?;
    let onto_spec = format!("{}^{{commit}}", l.onto);
    let onto = match git::out(repo, &["rev-parse", "--verify", &onto_spec]) {
        Ok(sha) => sha,
        Err(_) => {
            // The new main may not be fetched yet; only the commit is needed: no ref and no FETCH_HEAD is written.
            plain_run(
                repo,
                &[
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    l.remote,
                    l.onto,
                ],
            )?;
            git::out(repo, &["rev-parse", "--verify", &onto_spec])?
        }
    };
    if l.expected_head.is_empty() || l.writer.is_empty() {
        return refuse(l.branch, "no recorded head or writer identity for the lane (its Writer trailer must name the lane)");
    }
    // Each lane has its own ref, so a parallel fetch of another lane cannot change what is read here.
    let tracking = fetch_ref(l.branch);
    plain_run(
        repo,
        &[
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            l.remote,
            &format!("+refs/heads/{}:{tracking}", l.branch),
        ],
    )?;
    let old = git::out(
        repo,
        &["rev-parse", "--verify", &format!("{tracking}^{{commit}}")],
    )?;
    if l.expected_head != old {
        return refuse(
            l.branch,
            format!(
                "fetched head {old} is not the recorded head {}",
                l.expected_head
            ),
        );
    }
    // A stacked lane replays only what follows its open parent's recorded head, never the parent's commits.
    let boundary = match l.parent_head {
        Some(p) => {
            let p = git::out(repo, &["rev-parse", "--verify", &format!("{p}^{{commit}}")])?;
            if !git::is_ancestor(repo, &p, &old)? {
                return refuse(
                    l.branch,
                    "the lane does not contain its parent's recorded head",
                );
            }
            Some(p)
        }
        None => None,
    };
    let from = boundary.as_deref().unwrap_or(&onto);
    let own_range = format!("{from}..{old}");
    if let Some(bad) = merge_with_own_edits(repo, &own_range)? {
        return refuse(
            l.branch,
            format!("merge {bad} has edits of its own that a rebase would drop"),
        );
    }
    let before = own_commits(repo, &own_range)?;
    if let Some(bad) = foreign(repo, &before, l.writer)? {
        return refuse(
            l.branch,
            format!("commit {bad} has a foreign Writer trailer"),
        );
    }
    if git::is_ancestor(repo, &onto, &old)? {
        if before.is_empty() {
            return refuse(l.branch, "no own commit");
        }
        return Ok(Outcome::Current(report(
            l, &old, &old, true, &before, &before, None,
        )));
    }
    let new = match rebase_in_worktree(repo, &old, &onto, boundary.as_deref(), l.branch)? {
        Ok(new) => new,
        Err(files) => {
            return Ok(Outcome::Conflict {
                lane: l.branch.into(),
                files,
            })
        }
    };
    let after = own_commits(repo, &format!("{onto}..{new}"))?;
    if after.is_empty() {
        return refuse(l.branch, "no own commit is left after the rebase");
    }
    if let Some(bad) = foreign(repo, &after, l.writer)? {
        return refuse(
            l.branch,
            format!("commit {bad} has a foreign Writer trailer"),
        );
    }
    let equal = patch_ids(repo, &own_range)? == patch_ids(repo, &format!("{onto}..{new}"))?;
    // The first free successor name: a successor an earlier run pushed is adopted when it is that run's result
    // on this main; one that predates the new main is passed over, never overwritten.
    let mut target = next_branch(l.branch);
    for _ in 0..MAX_SUCCESSORS {
        if !l.push {
            break;
        }
        let Some(tip) = remote_branch(repo, l.remote, &target)? else {
            break;
        };
        if git::is_ancestor(repo, &onto, &tip)? {
            let theirs = own_commits(repo, &format!("{onto}..{tip}"))?;
            let same = !theirs.is_empty()
                && foreign(repo, &theirs, l.writer)?.is_none()
                && patch_ids(repo, &format!("{onto}..{tip}"))?
                    == patch_ids(repo, &format!("{onto}..{new}"))?;
            if !same {
                return refuse(
                    l.branch,
                    format!("{target} holds none of this lane's own patches on the new main"),
                );
            }
            let r = report(l, &old, &tip, equal, &before, &theirs, Some(target));
            return Ok(Outcome::Current(r));
        }
        target = next_branch(&target);
    }
    if l.push && remote_branch(repo, l.remote, &target)?.is_some() {
        return refuse(l.branch, format!("no free successor name up to {target}"));
    }
    git::out(repo, &["update-ref", &result_ref(&target), &new])?;
    let pushed = if l.push {
        let _lock = push_lock(repo)?;
        plain_run(
            repo,
            &["push", l.remote, &format!("{new}:refs/heads/{target}")],
        )?;
        Some(target)
    } else {
        None
    };
    Ok(Outcome::Rebased(report(
        l, &old, &new, equal, &before, &after, pushed,
    )))
}

fn report(
    l: &Lane,
    old: &str,
    new: &str,
    equal: bool,
    before: &[String],
    after: &[String],
    pushed: Option<String>,
) -> Report {
    Report {
        lane: l.branch.into(),
        old_head: old.into(),
        new_head: new.into(),
        patch_ids_equal: equal,
        own_before: before.len(),
        own_after: after.len(),
        pushed,
    }
}
