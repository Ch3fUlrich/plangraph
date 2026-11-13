//! Bounded PR-aware cleanup. Planning is read-only; apply journals before mutation.
use crate::{
    disk_hygiene::{git, has_ignored_non_cache},
    prod_paths,
    run::record_io,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    process::Command,
};

const MAX_GH_CALLS: usize = 64;
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Pr {
    #[serde(default)]
    head_ref_name: String,
    head_ref_oid: String,
    state: String,
    merged_at: Option<String>,
    number: u64,
}
#[derive(Clone, Debug, Serialize)]
pub struct Action {
    pub branch: String,
    pub sha: String,
    pub pr: Option<u64>,
    pub worktrees: Vec<PathBuf>,
    pub delete_branch: bool,
    pub remote: bool,
}
#[derive(Debug, Serialize)]
pub struct Report {
    pub dry_run: bool,
    pub actions: Vec<Action>,
    pub skipped: Vec<String>,
    pub gh_calls: usize,
    pub restore: Option<PathBuf>,
}
pub struct Options {
    pub repo: PathBuf,
    pub root: PathBuf,
    pub units: PathBuf,
    pub home: PathBuf,
    pub state: PathBuf,
    pub apply: bool,
    pub remote: bool,
    pub max: usize,
}
#[derive(Clone)]
struct Tree {
    path: PathBuf,
    sha: String,
    branch: String,
    protected: bool,
}

fn trees(repo: &Path) -> io::Result<Vec<Tree>> {
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    let mut result = vec![];
    for (i, block) in listing.split("\0\0").filter(|b| !b.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let field = |prefix| {
            fields
                .iter()
                .find_map(|s| s.strip_prefix(prefix))
                .unwrap_or("")
                .to_string()
        };
        result.push(Tree {
            path: PathBuf::from(field("worktree ")),
            sha: field("HEAD "),
            branch: field("branch ").trim_start_matches("refs/heads/").into(),
            protected: i == 0
                || fields
                    .iter()
                    .any(|s| s.starts_with("locked") || s.starts_with("prunable")),
        });
    }
    Ok(result)
}
fn prs(repo: &Path, args: &[&str]) -> io::Result<Vec<Pr>> {
    let output = prod_paths::command_output(Command::new("gh").current_dir(repo).args(args))?;
    serde_json::from_str(&output).map_err(io::Error::other)
}
fn origin_contains(repo: &Path, sha: &str) -> io::Result<bool> {
    Ok(git(repo, &["rev-list", sha, "--not", "--remotes=origin"])?
        .trim()
        .is_empty())
}
fn main_contains(repo: &Path, sha: &str) -> io::Result<bool> {
    Ok(git(
        repo,
        &["rev-list", sha, "--not", "refs/remotes/origin/main"],
    )?
    .trim()
    .is_empty())
}
fn process_uses(path: &Path) -> io::Result<bool> {
    #[cfg(target_os = "linux")]
    {
        for entry in fs::read_dir("/proc")? {
            let entry = entry?;
            if !entry
                .file_name()
                .to_string_lossy()
                .bytes()
                .all(|b| b.is_ascii_digit())
            {
                continue;
            }
            match fs::read_link(entry.path().join("cwd")) {
                Ok(cwd) if cwd.starts_with(path) => return Ok(true),
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
        Ok(false)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        Err(io::Error::other("process cwd scan requires Linux"))
    }
}
fn safe_tree(tree: &Tree, opts: &Options, refs: &[prod_paths::Reference]) -> io::Result<()> {
    let path = tree.path.canonicalize()?;
    let root = opts.root.canonicalize()?;
    if tree.protected
        || path == root
        || path == opts.repo.canonicalize()?
        || !path.starts_with(root)
        || fs::symlink_metadata(&tree.path)?.file_type().is_symlink()
    {
        return Err(io::Error::other("protected or outside root"));
    }
    if refs.iter().any(|r| {
        let p = r.path.canonicalize().unwrap_or_else(|_| r.path.clone());
        p.starts_with(&path) || path.starts_with(&p)
    }) {
        return Err(io::Error::other("user unit reference"));
    }
    if process_uses(&path)? {
        return Err(io::Error::other("process cwd"));
    }
    if !git(
        &path,
        &["status", "--porcelain=v1", "--untracked-files=all"],
    )?
    .trim()
    .is_empty()
        || !git(&path, &["ls-files", "--", "target", "node_modules"])?
            .trim()
            .is_empty()
        || has_ignored_non_cache(&path)?
    {
        return Err(io::Error::other(
            "dirty, tracked cache or ignored non-cache files",
        ));
    }
    if !origin_contains(&opts.repo, &tree.sha)? {
        return Err(io::Error::other("commits missing from origin"));
    }
    Ok(())
}

pub fn plan(opts: &Options) -> io::Result<Report> {
    let mut report = Report {
        dry_run: !opts.apply,
        actions: vec![],
        skipped: vec![],
        gh_calls: 1,
        restore: None,
    };
    // Prove origin/main exists before trusting any PR state.
    git(
        &opts.repo,
        &["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
    )?;
    let merged = prs(
        &opts.repo,
        &[
            "pr",
            "list",
            "--state",
            "merged",
            "--limit",
            "1000",
            "--json",
            "state,headRefName,headRefOid,mergedAt,number",
        ],
    )?;
    let default = git(
        &opts.repo,
        &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
    )
    .unwrap_or_else(|_| "refs/remotes/origin/main".into());
    let default = default.trim().trim_start_matches("refs/remotes/origin/");
    let trees = trees(&opts.repo)?;
    let refs = prod_paths::unit_references(&opts.units, &opts.home)?;
    let branches = git(
        &opts.repo,
        &[
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname)",
            "refs/heads/",
        ],
    )?;
    let remotes = git(
        &opts.repo,
        &[
            "for-each-ref",
            "--format=%(refname)\t%(objectname)",
            "refs/remotes/origin/",
        ],
    )?;
    let remote_refs: BTreeMap<_, _> = remotes.lines().filter_map(|l| l.split_once('\t')).collect();
    for line in branches.lines() {
        if report.actions.len() >= opts.max {
            break;
        }
        let (branch, sha) = line
            .split_once('\t')
            .ok_or_else(|| io::Error::other("invalid branch listing"))?;
        if ["main", "master", default].contains(&branch) {
            continue;
        }
        if report.gh_calls >= MAX_GH_CALLS {
            report.skipped.push("gh call bound reached".into());
            break;
        }
        report.gh_calls += 1;
        // Query all states to catch reopened/new OPEN PRs, including phase/*.
        let all = prs(
            &opts.repo,
            &[
                "pr",
                "list",
                "--state",
                "all",
                "--head",
                branch,
                "--limit",
                "100",
                "--json",
                "state,headRefName,headRefOid,mergedAt,number",
            ],
        )?;
        if all.len() == 100 || all.iter().any(|p| p.state == "OPEN") {
            report
                .skipped
                .push(format!("{branch}: open PR or truncated history"));
            continue;
        }
        let mut matched = None;
        for pr in all
            .iter()
            .chain(merged.iter().filter(|p| p.head_ref_name == branch))
        {
            if pr.head_ref_oid == sha
                && ((pr.state == "MERGED" && pr.merged_at.is_some())
                    || (pr.state == "CLOSED" && main_contains(&opts.repo, &pr.head_ref_oid)?))
            {
                matched = Some(pr.number);
                break;
            }
        }
        let remote_name = format!("refs/remotes/origin/{branch}");
        let gone = !remote_refs.contains_key(remote_name.as_str());
        let checked: Vec<_> = trees.iter().filter(|t| t.branch == branch).collect();
        if matched.is_none() && (!gone || checked.is_empty() || !origin_contains(&opts.repo, sha)?)
        {
            report
                .skipped
                .push(format!("{branch}: no matching merged/contained closed PR"));
            continue;
        }
        let mut safe = true;
        for tree in &checked {
            if let Err(e) = safe_tree(tree, opts, &refs) {
                report
                    .skipped
                    .push(format!("{branch} {}: {e}", tree.path.display()));
                safe = false;
            }
        }
        if !safe {
            continue;
        }
        let remote = opts.remote
            && matched.is_some()
            && remote_refs
                .get(remote_name.as_str())
                .is_some_and(|s| *s == sha);
        report.actions.push(Action {
            branch: branch.into(),
            sha: sha.into(),
            pr: matched,
            worktrees: checked.iter().map(|t| t.path.clone()).collect(),
            delete_branch: matched.is_some(),
            remote,
        });
    }
    // Detached trees are removable only if their HEAD is already on origin.
    for tree in trees.iter().filter(|t| t.branch.is_empty() && !t.protected) {
        if report.actions.len() >= opts.max {
            break;
        }
        match safe_tree(tree, opts, &refs) {
            Ok(()) => report.actions.push(Action {
                branch: String::new(),
                sha: tree.sha.clone(),
                pr: None,
                worktrees: vec![tree.path.clone()],
                delete_branch: false,
                remote: false,
            }),
            Err(e) => report.skipped.push(format!("{}: {e}", tree.path.display())),
        }
    }
    Ok(report)
}

fn tsv_field(value: &str) -> io::Result<&str> {
    if value.contains(['\t', '\r', '\n']) {
        Err(io::Error::other("TSV field contains control character"))
    } else {
        Ok(value)
    }
}
fn journal(opts: &Options, actions: &[Action]) -> io::Result<PathBuf> {
    let dir = opts.state.join("sweep");
    record_io::ensure_private_dir(&dir)?;
    let path = dir.join(format!(
        "{}.tsv",
        chrono::Utc::now().format("%Y%m%dT%H%M%S%.9fZ")
    ));
    let mut text = String::from("branch\tsha\tpr\tworktree\n");
    for action in actions {
        let paths = if action.worktrees.is_empty() {
            vec![PathBuf::new()]
        } else {
            action.worktrees.clone()
        };
        for p in paths {
            text.push_str(&format!(
                "{}\t{}\t{}\t{}\n",
                tsv_field(&action.branch)?,
                tsv_field(&action.sha)?,
                action.pr.map(|n| n.to_string()).unwrap_or_default(),
                tsv_field(&p.to_string_lossy())?
            ));
        }
    }
    record_io::atomic_write_private(&path, text.as_bytes())?;
    Ok(path)
}

pub fn run(opts: &Options) -> io::Result<Report> {
    if opts.apply {
        git(&opts.repo, &["fetch", "--prune", "origin"])?;
    }
    let mut report = plan(opts)?;
    if !opts.apply || report.actions.is_empty() {
        return Ok(report);
    }
    report.restore = Some(journal(opts, &report.actions)?);
    for action in &report.actions {
        let refs = prod_paths::unit_references(&opts.units, &opts.home)?;
        let current = trees(&opts.repo)?;
        if action.delete_branch
            && git(
                &opts.repo,
                &["rev-parse", &format!("refs/heads/{}", action.branch)],
            )?
            .trim()
                != action.sha
        {
            return Err(io::Error::other("branch changed since journal"));
        }
        // A new checkout of this branch invalidates the whole branch action.
        if current.iter().any(|t| {
            !action.branch.is_empty()
                && t.branch == action.branch
                && !action.worktrees.contains(&t.path)
        }) {
            return Err(io::Error::other("branch acquired another worktree"));
        }
        for path in &action.worktrees {
            let tree = current
                .iter()
                .find(|t| &t.path == path && t.sha == action.sha && t.branch == action.branch)
                .ok_or_else(|| io::Error::other("worktree changed since journal"))?;
            safe_tree(tree, opts, &refs)?;
            git(
                &opts.repo,
                &[
                    "worktree",
                    "remove",
                    path.to_str()
                        .ok_or_else(|| io::Error::other("invalid path"))?,
                ],
            )?;
        }
        if action.delete_branch {
            git(&opts.repo, &["branch", "-D", "--", &action.branch])?;
        }
        if action.remote {
            let lease = format!(
                "--force-with-lease=refs/heads/{}:{}",
                action.branch, action.sha
            );
            git(
                &opts.repo,
                &["push", &lease, "--delete", "--", "origin", &action.branch],
            )?;
        }
    }
    Ok(report)
}
