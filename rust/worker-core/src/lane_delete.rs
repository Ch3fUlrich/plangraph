//! The one door that deletes a remote `lane/*` branch: pinned object, synced restore row,
//! exact-SHA lease, and a push that runs the installed pre-push hook (never `hooksPath=/dev/null`).
//! The hook accepts the delete only for the remote marked `plangraph-lane-delete=app` and only
//! when the record named in `PLANGRAPH_LANE_DELETE_RECORD` holds the exact ref and SHA.
use crate::{host_command, merged_sweep};
use pg_reap::Worktree;
use std::{
    fs,
    io::{self, Write},
    path::Path,
    process::Command,
    time::Duration,
};

pub const LANE_PREFIX: &str = "refs/heads/lane/";
pub const RECORD_ENV: &str = "PLANGRAPH_LANE_DELETE_RECORD";
/// Where a pinned object stays reachable for a restore push after its branch is gone.
pub const PIN_PREFIX: &str = "refs/plangraph/restore/";

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    Deleted,
    /// The remote has no such ref any more: nothing to do, nothing to retry.
    Absent,
    /// The remote ref points at another SHA: kept, the next run re-evaluates it.
    Moved(String),
}

pub fn is_lane_ref(reference: &str) -> bool {
    reference.strip_prefix(LANE_PREFIX).is_some_and(|rest| {
        !rest.is_empty()
            && !rest
                .split('/')
                .any(|c| c == "plangraph-md-sync" || c.starts_with("live-"))
    })
}

pub fn valid_sha(sha: &str) -> bool {
    sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit())
}

/// The remote that carries the land App identity; `origin` when unset.
pub fn remote_name() -> String {
    std::env::var("PG_LANE_DELETE_REMOTE")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "origin".into())
}

pub fn run_git(repo: &Path, args: &[&str], secs: u64) -> io::Result<std::process::Output> {
    host_command::capture(
        Command::new("git").arg("-C").arg(repo).args(args),
        Duration::from_secs(secs),
    )
}

/// The SHA of one remote ref, `None` when the remote does not have it.
pub fn remote_sha(repo: &Path, remote: &str, reference: &str) -> io::Result<Option<String>> {
    let out = run_git(repo, &["ls-remote", remote, reference], 120)?;
    if !out.status.success() {
        return Err(io::Error::other("ls-remote failed"));
    }
    let text = String::from_utf8_lossy(&out.stdout);
    Ok(text.lines().find_map(|line| {
        let (sha, name) = line.split_once('\t')?;
        (name == reference).then(|| sha.to_string())
    }))
}

fn pin(repo: &Path, remote: &str, reference: &str, sha: &str) -> io::Result<()> {
    let pin = format!("{PIN_PREFIX}{sha}");
    let commit = format!("{sha}^{{commit}}");
    if run_git(repo, &["cat-file", "-e", &commit], 30)?
        .status
        .success()
    {
        run_git(repo, &["update-ref", &pin, sha], 30)?;
    } else {
        run_git(
            repo,
            &[
                "fetch",
                "--no-tags",
                "--quiet",
                remote,
                &format!("+{reference}:{pin}"),
            ],
            600,
        )?;
    }
    let got = run_git(
        repo,
        &["rev-parse", "--verify", "-q", &format!("{pin}^{{commit}}")],
        30,
    )?;
    if String::from_utf8_lossy(&got.stdout).trim() != sha {
        let _ = run_git(repo, &["update-ref", "-d", &pin], 30);
        return Err(io::Error::other(
            "cannot pin the exact commit before deletion",
        ));
    }
    Ok(())
}

/// Append and sync one restore row (the same tsv the worktree sweep writes).
fn record(repo: &Path, file: &Path, reference: &str, sha: &str) -> io::Result<()> {
    let tree = Worktree {
        path: "-".into(),
        branch: Some(reference.into()),
        head: sha.into(),
        ..Worktree::default()
    };
    let mut handle = fs::OpenOptions::new().append(true).open(file)?;
    handle.write_all(merged_sweep::restore_row(repo, &tree, None).as_bytes())?;
    handle.sync_all()
}

/// One open pull request as the sweeps read it.
#[derive(serde::Deserialize)]
pub struct OpenPull {
    #[serde(rename = "headRefName")]
    pub head: String,
    #[serde(rename = "baseRefName")]
    pub base: String,
    #[serde(rename = "headRefOid")]
    pub sha: String,
}

/// Open pull requests of `gh_target` (`[HOST/]OWNER/NAME`, the checkout's repository when `None`),
/// optionally only those with `--head`/`--base` equal to a name. A truncated or unreadable
/// answer is an error: unresolved evidence never authorizes a delete.
pub fn open_pulls(
    repo: &Path,
    gh_target: Option<&str>,
    filter: Option<(&str, &str)>,
) -> io::Result<Vec<OpenPull>> {
    let mut command = Command::new("gh");
    command
        .current_dir(repo)
        .args(["pr", "list", "--state", "open", "--limit", "1000"]);
    if let Some(target) = gh_target {
        command.args(["--repo", target]);
    }
    if let Some((flag, name)) = filter {
        command.args([flag, name]);
    }
    command.args(["--json", "number,headRefName,baseRefName,headRefOid"]);
    let text = host_command::output(&mut command, Duration::from_secs(120))?;
    let rows: Vec<OpenPull> = serde_json::from_slice(&text).map_err(io::Error::other)?;
    if rows.len() >= 1000 {
        return Err(io::Error::other("open PR inventory truncated"));
    }
    Ok(rows)
}

/// The open pull request that uses `reference` as head or as base, if any (fresh, per ref).
pub fn open_pr_use(
    repo: &Path,
    gh_target: Option<&str>,
    reference: &str,
) -> io::Result<Option<&'static str>> {
    let short = reference.strip_prefix("refs/heads/").unwrap_or(reference);
    for (flag, why) in [("--head", "open-pr-head"), ("--base", "open-pr-base")] {
        if !open_pulls(repo, gh_target, Some((flag, short)))?.is_empty() {
            return Ok(Some(why));
        }
    }
    Ok(None)
}

/// Delete `reference` on `remote` only while it still points at `sha` and no open pull request
/// uses it as head or base (read right before the push). `record_file` is the restore tsv
/// (header already written); the row is synced before the push.
pub fn delete(
    repo: &Path,
    remote: &str,
    reference: &str,
    sha: &str,
    record_file: &Path,
    gh_target: Option<&str>,
) -> io::Result<Outcome> {
    if !is_lane_ref(reference) || !valid_sha(sha) {
        return Err(io::Error::other("not a lane ref or exact SHA"));
    }
    if !run_git(repo, &["check-ref-format", reference], 30)?
        .status
        .success()
    {
        return Err(io::Error::other("invalid ref name"));
    }
    match remote_sha(repo, remote, reference)? {
        None => return Ok(Outcome::Absent),
        Some(current) if current != sha => return Ok(Outcome::Moved(current)),
        Some(_) => {}
    }
    if let Some(why) = open_pr_use(repo, gh_target, reference)? {
        return Err(io::Error::other(format!("protected: {why}")));
    }
    pin(repo, remote, reference, sha)?;
    record(repo, record_file, reference, sha)?;
    let record_file = record_file.canonicalize()?;
    let lease = format!("--force-with-lease={reference}:{sha}");
    let out = host_command::capture(
        Command::new("git")
            .arg("-C")
            .arg(repo)
            .env(RECORD_ENV, &record_file)
            .args(["push", "--quiet", &lease, remote, &format!(":{reference}")]),
        Duration::from_secs(120),
    )?;
    if out.status.success() {
        return Ok(Outcome::Deleted);
    }
    match remote_sha(repo, remote, reference)? {
        None => Ok(Outcome::Absent),
        Some(current) if current != sha => Ok(Outcome::Moved(current)),
        Some(_) => Err(io::Error::other(format!(
            "delete refused: {}",
            String::from_utf8_lossy(&out.stderr)
                .trim()
                .replace('\n', " ")
        ))),
    }
}

/// Sweep callers: a moved remote ref is a refusal there, an absent one is done.
pub fn delete_or_err(
    repo: &Path,
    reference: &str,
    sha: &str,
    record_file: &Path,
) -> io::Result<()> {
    match delete(repo, &remote_name(), reference, sha, record_file, None)? {
        Outcome::Moved(_) => Err(io::Error::other("remote branch moved: lease refused")),
        Outcome::Deleted | Outcome::Absent => Ok(()),
    }
}
