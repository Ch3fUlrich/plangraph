//! Applying adapter for pg-reap's pure policy; all authority is collected live.
use crate::{
    disk_hygiene::git,
    run::record_io,
    sweep_facts::{self, Protection},
};
use pg_reap::{classify, parse_porcelain, Classification, Worktree};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy)]
pub struct Options<'a> {
    pub repo: &'a Path,
    pub root: &'a Path,
    pub pins: &'a Path,
    pub state: &'a Path,
    pub claims_reader: Option<&'a Path>,
    pub apply: bool,
    pub delete_remote: bool,
}
pub fn eligible(tree: &Worktree, facts: &pg_reap::Facts, inventory: &[Worktree]) -> bool {
    matches!(
        classify(tree, facts, inventory),
        Classification::Eligible {
            rule: "merged-clean-24h"
        }
    )
}
fn inventory(repo: &Path) -> io::Result<Vec<Worktree>> {
    parse_porcelain(&git(repo, &["worktree", "list", "--porcelain"])?).map_err(io::Error::other)
}
fn escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('\t', "\\t")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}
pub fn restore_row(repo: &Path, tree: &Worktree, pr: Option<u64>) -> String {
    format!(
        "{}\t{}\t{}\t{}\t{}\n",
        escape(&repo.to_string_lossy()),
        escape(&tree.path),
        escape(tree.branch.as_deref().unwrap_or("-")),
        escape(&tree.head),
        pr.map(|p| p.to_string()).unwrap_or_else(|| "-".into())
    )
}
/// No --force, and branch deletion compares the recorded SHA atomically.
pub fn remove(repo: &Path, tree: &Worktree) -> io::Result<()> {
    if git(Path::new(&tree.path), &["rev-parse", "HEAD"])?.trim() != tree.head
        || git(Path::new(&tree.path), &["symbolic-ref", "HEAD"])?.trim()
            != tree.branch.as_deref().unwrap_or("")
        || !git(
            Path::new(&tree.path),
            &[
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored",
            ],
        )?
        .trim()
        .is_empty()
    {
        return Err(io::Error::other("worktree changed before removal"));
    }
    git(repo, &["worktree", "remove", "--", &tree.path])?;
    if let Some(branch) = &tree.branch {
        git(repo, &["update-ref", "-d", branch, &tree.head])?;
    }
    Ok(())
}
pub fn run(options: &Options<'_>) -> io::Result<String> {
    run_using(options, |options, branch| {
        if options.claims_reader.is_none() {
            return Err(io::Error::other("claims reader unavailable: protected"));
        }
        let prs = sweep_facts::pulls(options.repo, branch)?;
        let protection = Protection::load(options.pins, options.state, options.claims_reader)?;
        Ok((protection, prs))
    })
}
/// Inject authorities only in hermetic tests; the mutation and restore path is shared.
#[cfg(feature = "test-support")]
pub fn run_with_authority(
    options: &Options<'_>,
    authority: impl FnMut(&Options<'_>, &str) -> io::Result<(Protection, Vec<sweep_facts::Pull>)>,
) -> io::Result<String> {
    run_using(options, authority)
}
fn run_using(
    options: &Options<'_>,
    mut authority: impl FnMut(&Options<'_>, &str) -> io::Result<(Protection, Vec<sweep_facts::Pull>)>,
) -> io::Result<String> {
    let repo = options.repo.canonicalize()?;
    let options = &Options {
        repo: &repo,
        ..*options
    };
    record_io::ensure_private_dir(options.state)?;
    let _lock = crate::lockfile::try_lock(&options.state.join("merged-sweep.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another sweep is running"))?;
    // A successful refresh is mandatory even in dry run: stale refs cannot grant removal.
    git(options.repo, &["fetch", "--prune", "origin"])?;
    let root = options.root.canonicalize()?;
    let trees = inventory(options.repo)?;
    let mut report = String::new();
    let mut restore = None;
    if options.apply {
        let dir = options.state.join("sweep");
        record_io::ensure_private_dir(&dir)?;
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let path: PathBuf = dir.join(format!("{ts}.tsv"));
        record_io::atomic_write_private(&path, b"repo\tpath\tbranch\tsha\tPR\n")?;
        fs::File::open(&dir)?.sync_all()?;
        restore = Some(fs::OpenOptions::new().append(true).open(path)?);
    }
    for tree in &trees {
        let path = Path::new(&tree.path);
        if !path.starts_with(&root) || path == root || tree.is_main {
            continue;
        }
        if !path
            .canonicalize()
            .is_ok_and(|p| p.starts_with(&root) && p != root)
        {
            report.push_str(&format!(
                "protected {}: outside canonical root or unavailable\n",
                tree.path
            ));
            continue;
        }
        let result = (|| -> io::Result<()> {
            // Fresh authority, inventory, HEAD, status, age, cwds and PR state for each mutation.
            let fresh = inventory(options.repo)?;
            let Some(current) = fresh
                .iter()
                .find(|w| w.path == tree.path && w.head == tree.head && w.branch == tree.branch)
            else {
                return Err(io::Error::other("worktree changed"));
            };
            let branch = current
                .branch
                .as_deref()
                .ok_or_else(|| io::Error::other("detached: protected"))?;
            let (protection, prs) = authority(options, branch)?;
            let facts =
                sweep_facts::facts(options.repo, current, &protection, &prs, SystemTime::now())?;
            if !eligible(current, &facts, &fresh) {
                report.push_str(&format!(
                    "protected {}: {:?}\n",
                    tree.path,
                    classify(current, &facts, &fresh)
                ));
                return Ok(());
            }
            let merged_pr = prs
                .iter()
                .find(|p| p.state == "MERGED" && p.sha == tree.head);
            let restore_pr = merged_pr.or_else(|| prs.iter().find(|p| p.sha == tree.head));
            if let Some(file) = &mut restore {
                file.write_all(
                    restore_row(options.repo, tree, restore_pr.map(|p| p.number)).as_bytes(),
                )?;
                file.sync_all()?;
                remove(options.repo, current)?;
                if options.delete_remote && merged_pr.is_some() {
                    // Lease guards a remote branch that advanced after the PR merged.
                    let remote_ref = branch.to_string();
                    let lease = format!("--force-with-lease={remote_ref}:{}", tree.head);
                    git(
                        options.repo,
                        &["push", &lease, "origin", &format!(":{remote_ref}")],
                    )?;
                }
            }
            report.push_str(&format!(
                "{} {}\n",
                if options.apply {
                    "removed"
                } else {
                    "would-remove"
                },
                tree.path
            ));
            Ok(())
        })();
        if let Err(error) = result {
            report.push_str(&format!("protected {}: {error}\n", tree.path));
        }
    }
    Ok(report)
}
