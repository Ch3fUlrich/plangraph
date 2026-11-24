//! `land-train relane`: after a train merge, one open lane at a time is fetched into its own ref,
//! rebased onto the new main in its own worktree, checked and (on request) pushed as the next `-rN` branch.
//! A lane is never read through FETCH_HEAD, which parallel fetches overwrite. A patch already on main is
//! dropped, not a conflict; a conflict leaves the lane ref and the remote untouched.
use super::git;
use serde::Serialize;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

/// What one lane needs: the branch, and what the registry recorded for it.
pub struct Lane<'a> {
    pub repo: &'a Path,
    pub remote: &'a str,
    /// `refs/heads/<branch>` on the remote, e.g. `lane/batch1-r1`.
    pub branch: &'a str,
    /// The new main (a commit id); every own commit ends up on top of it.
    pub onto: &'a str,
    /// The head the registry recorded for the lane: a different fetched head is a mixed-up fetch.
    pub expected_head: Option<&'a str>,
    /// Text every own commit's `Writer` trailer must contain; a commit of another writer is foreign.
    pub writer: Option<&'a str>,
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

fn po_ref(branch: &str) -> String {
    format!("refs/po/{branch}")
}

/// Git that talks to the remote: the caller's own environment (credentials, hook configuration), no prompts,
/// but never the caller's repository variables (GIT_DIR, GIT_INDEX_FILE ...): it works in `dir` only.
pub(super) fn plain(dir: &Path) -> Result<Command, String> {
    let mut c = Command::new("git");
    c.current_dir(dir)
        .env("LC_ALL", "C")
        .env("GIT_TERMINAL_PROMPT", "0");
    for variable in git::out(dir, &["rev-parse", "--local-env-vars"])?.lines() {
        c.env_remove(variable);
    }
    Ok(c)
}

fn plain_run(dir: &Path, args: &[&str]) -> Result<(), String> {
    let o = plain(dir)?
        .args(args)
        .output()
        .map_err(|e| format!("git {args:?}: {e}"))?;
    if o.status.success() {
        Ok(())
    } else {
        Err(format!(
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        ))
    }
}

fn refuse(lane: &str, reason: impl Into<String>) -> Result<Outcome, String> {
    Ok(Outcome::Refused {
        lane: lane.into(),
        reason: reason.into(),
    })
}

/// The lane's own commits of `range`, oldest first. A merge of main into the lane is not an own commit: it has
/// no `Writer` trailer, the rebase flattens it and `patch_ids` skips it.
fn own_commits(dir: &Path, range: &str) -> Result<Vec<String>, String> {
    Ok(
        git::out(dir, &["rev-list", "--reverse", "--no-merges", range])?
            .lines()
            .map(str::to_owned)
            .collect(),
    )
}

/// One `patch-id` per non-merge commit of `range`, oldest first.
fn patch_ids(dir: &Path, range: &str) -> Result<Vec<String>, String> {
    let log = git::bytes(dir, &["log", "-p", "--reverse", "--no-merges", range])?;
    let mut child = git::command(dir, None)
        .args(["patch-id", "--stable"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let mut stdin = child.stdin.take().ok_or("patch-id has no stdin")?;
    // The writer runs beside the reader so a long log cannot fill the pipe and block.
    let feed = std::thread::spawn(move || stdin.write_all(&log));
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    feed.join()
        .map_err(|_| "patch-id feeder panicked".to_owned())?
        .map_err(|e| e.to_string())?;
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| l.split_whitespace().next().map(str::to_owned))
        .collect())
}

fn writers(dir: &Path, commit: &str) -> Result<String, String> {
    git::out(
        dir,
        &[
            "log",
            "-1",
            "--format=%(trailers:key=Writer,valueonly,separator=%x1f)",
            commit,
        ],
    )
}

/// Held while a ref of this clone is pushed: two pushes of one clone never run at the same time.
pub(super) fn push_lock(repo: &Path) -> Result<std::fs::File, String> {
    let common = git::out(
        repo,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    let dir = Path::new(&common).join("plangraph");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let file = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(dir.join("relane-push.lock"))
        .map_err(|e| e.to_string())?;
    file.lock().map_err(|e| e.to_string())?;
    Ok(file)
}

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
    // Each lane has its own ref, so a parallel fetch of another lane cannot change what is read here.
    let tracking = po_ref(l.branch);
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
    if let Some(want) = l.expected_head {
        if want != old {
            return refuse(
                l.branch,
                format!("fetched head {old} is not the recorded head {want}"),
            );
        }
    }
    let own_range = format!("{onto}..{old}");
    let before = own_commits(repo, &own_range)?;
    if let Some(w) = l.writer {
        if let Some(bad) = foreign(repo, &before, w)? {
            return refuse(
                l.branch,
                format!("commit {bad} has a foreign Writer trailer"),
            );
        }
    }
    if git::is_ancestor(repo, &onto, &old)? {
        if before.is_empty() {
            return refuse(l.branch, "no own commit");
        }
        return Ok(Outcome::Current(report(
            l, &old, &old, true, &before, &before, None,
        )));
    }
    let new = match rebase_in_worktree(repo, &old, &onto, l.branch)? {
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
    if let Some(w) = l.writer {
        if let Some(bad) = foreign(repo, &after, w)? {
            return refuse(
                l.branch,
                format!("commit {bad} has a foreign Writer trailer"),
            );
        }
    }
    let equal = patch_ids(repo, &own_range)? == patch_ids(repo, &format!("{onto}..{new}"))?;
    let target = next_branch(l.branch);
    git::out(repo, &["update-ref", &po_ref(&target), &new])?;
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

/// The first commit without a `Writer` trailer that contains `writer`.
fn foreign(repo: &Path, commits: &[String], writer: &str) -> Result<Option<String>, String> {
    for c in commits {
        if !writers(repo, c)?
            .split('\u{1f}')
            .any(|w| w.contains(writer))
        {
            return Ok(Some(c.clone()));
        }
    }
    Ok(None)
}

/// Rebases `old` onto `onto` in a worktree of its own, dropping commits whose patch is already on `onto`.
/// `Ok(Err(files))` is a conflict with its unmerged paths; the worktree is gone either way.
fn rebase_in_worktree(
    repo: &Path,
    old: &str,
    onto: &str,
    branch: &str,
) -> Result<Result<String, Vec<String>>, String> {
    let common = git::out(
        repo,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
    )?;
    let parent = Path::new(&common).join("plangraph/relane-worktrees");
    std::fs::create_dir_all(&parent).map_err(|e| e.to_string())?;
    let tmp = tempfile::Builder::new()
        .prefix("lane-")
        .tempdir_in(&parent)
        .map_err(|e| e.to_string())?;
    let wt = tmp.path();
    git::out(
        repo,
        &["worktree", "add", "--detach", &wt.to_string_lossy(), old],
    )?;
    let result = rebase_here(wt, onto, branch);
    // The worktree is removed on every path; a failed removal must not hide the result.
    let removed = git::out(
        repo,
        &["worktree", "remove", "--force", &wt.to_string_lossy()],
    );
    let result = result?;
    removed?;
    Ok(result)
}

fn rebase_here(wt: &Path, onto: &str, branch: &str) -> Result<Result<String, Vec<String>>, String> {
    let o = git::run(
        wt,
        None,
        &[
            "-c",
            "rerere.enabled=false",
            "rebase",
            "--no-autostash",
            "--no-update-refs",
            "--no-fork-point",
            "--empty=drop",
            onto,
        ],
    )?;
    if o.status.success() {
        return Ok(Ok(git::out(wt, &["rev-parse", "HEAD"])?));
    }
    // A conflict has at least one unmerged path; a failure without one is an error, not a conflict.
    let files: Vec<String> = git::out(wt, &["diff", "--name-only", "--diff-filter=U"])?
        .lines()
        .map(str::to_owned)
        .collect();
    let stderr = String::from_utf8_lossy(&o.stderr).trim().to_owned();
    let _ = git::run(wt, None, &["rebase", "--abort"]);
    if files.is_empty() {
        return Err(format!(
            "rebase of {branch} failed without a conflict: {stderr}"
        ));
    }
    Ok(Err(files))
}

/// `relane --repo DIR --remote NAME --onto SHA --lanes A,B [--expect A=SHA,B=SHA] [--writer TEXT] [--push true]`
pub fn run_cli(flags: &std::collections::BTreeMap<String, String>) -> Result<Vec<String>, String> {
    let need = |k: &str| {
        flags
            .get(k)
            .cloned()
            .ok_or_else(|| format!("relane: missing --{k}"))
    };
    let repo = std::path::PathBuf::from(need("repo")?);
    let (remote, onto) = (need("remote")?, need("onto")?);
    let expected: std::collections::BTreeMap<&str, &str> = flags
        .get("expect")
        .map(|s| s.split(',').filter_map(|p| p.split_once('=')).collect())
        .unwrap_or_default();
    let push = flags.get("push").is_some_and(|v| v == "true");
    let mut lines = Vec::new();
    let mut failed = false;
    // One lane after the other: one clone never fetches into or pushes two lanes at the same time.
    for branch in need("lanes")?.split(',').filter(|b| !b.is_empty()) {
        // An environment failure of one lane is that lane's refusal; the others are still relaned.
        let outcome = relane(&Lane {
            repo: &repo,
            remote: &remote,
            branch,
            onto: &onto,
            expected_head: expected.get(branch).copied(),
            writer: flags.get("writer").map(String::as_str),
            push,
        })
        .unwrap_or_else(|reason| Outcome::Refused {
            lane: branch.into(),
            reason,
        });
        failed |= !outcome.is_ok();
        lines.push(outcome.line());
    }
    if failed {
        Err(lines.join("\n"))
    } else {
        Ok(lines)
    }
}
