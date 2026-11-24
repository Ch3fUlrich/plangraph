//! Git steps of `relane`: remote git inside its clone, the commits and patch ids of a lane, the per-clone push
//! lock, the rebase in a worktree of its own and the guard against merges a rebase would flatten.
use super::git;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

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

pub(super) fn plain_run(dir: &Path, args: &[&str]) -> Result<(), String> {
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

/// The lane's own commits of `range`, oldest first. A merge of main into the lane is not an own commit: it has
/// no `Writer` trailer, the rebase flattens it and `patch_ids` skips it.
pub(super) fn own_commits(dir: &Path, range: &str) -> Result<Vec<String>, String> {
    Ok(
        git::out(dir, &["rev-list", "--reverse", "--no-merges", range])?
            .lines()
            .map(str::to_owned)
            .collect(),
    )
}

/// One `patch-id` per non-merge commit of `range`, oldest first.
pub(super) fn patch_ids(dir: &Path, range: &str) -> Result<Vec<String>, String> {
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

pub(super) fn writers(dir: &Path, commit: &str) -> Result<String, String> {
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

/// The first commit without a `Writer` trailer that contains `writer`.
pub(super) fn foreign(
    repo: &Path,
    commits: &[String],
    writer: &str,
) -> Result<Option<String>, String> {
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
pub(super) fn rebase_in_worktree(
    repo: &Path,
    old: &str,
    onto: &str,
    upstream: Option<&str>,
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
    let result = rebase_here(wt, onto, upstream, branch);
    // The worktree is removed on every path; a failed removal must not hide the result.
    let removed = git::out(
        repo,
        &["worktree", "remove", "--force", &wt.to_string_lossy()],
    );
    let result = result?;
    removed?;
    Ok(result)
}

pub(super) fn rebase_here(
    wt: &Path,
    onto: &str,
    upstream: Option<&str>,
    branch: &str,
) -> Result<Result<String, Vec<String>>, String> {
    let mut args = vec![
        "-c",
        "rerere.enabled=false",
        "rebase",
        "--no-autostash",
        "--no-update-refs",
        "--no-fork-point",
        "--empty=drop",
    ];
    match upstream {
        Some(up) => args.extend(["--onto", onto, up]),
        None => args.push(onto),
    }
    let o = git::run(wt, None, &args)?;
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

/// The first merge of `range` whose tree is not what merging its parents gives: a rebase flattens a merge, so
/// an edit made only in the merge (a conflict resolution, a change beside it) would be lost.
pub(super) fn merge_with_own_edits(repo: &Path, range: &str) -> Result<Option<String>, String> {
    for m in git::out(repo, &["rev-list", "--merges", range])?.lines() {
        let line = git::out(repo, &["rev-list", "--parents", "-n", "1", m])?;
        let ids: Vec<&str> = line.split_whitespace().collect();
        let [_, p1, p2] = ids[..] else {
            return Ok(Some(m.to_owned()));
        };
        let o = git::run(
            repo,
            None,
            &["merge-tree", "--write-tree", "--no-messages", p1, p2],
        )?;
        let merged = String::from_utf8_lossy(&o.stdout);
        let tree = merged.lines().next().unwrap_or("").trim();
        if !o.status.success() || tree != git::out(repo, &["rev-parse", &format!("{m}^{{tree}}")])?
        {
            return Ok(Some(m.to_owned()));
        }
    }
    Ok(None)
}

/// The head of `branch` on the remote, fetched into `refs/po/target/<branch>`; `None` when it does not exist.
pub(super) fn remote_branch(
    repo: &Path,
    remote: &str,
    branch: &str,
) -> Result<Option<String>, String> {
    let spec = format!("refs/heads/{branch}");
    let o = plain(repo)?
        .args(["ls-remote", "--heads", remote, &spec])
        .output()
        .map_err(|e| e.to_string())?;
    if !o.status.success() {
        return Err(format!(
            "git ls-remote failed: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        ));
    }
    if o.stdout.is_empty() {
        return Ok(None);
    }
    let tracking = format!("refs/po/target/{branch}");
    plain_run(
        repo,
        &[
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            remote,
            &format!("+{spec}:{tracking}"),
        ],
    )?;
    Ok(Some(git::out(
        repo,
        &["rev-parse", "--verify", &format!("{tracking}^{{commit}}")],
    )?))
}
