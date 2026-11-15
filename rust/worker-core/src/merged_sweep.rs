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
    pub roots: &'a [PathBuf],
    pub remote_targets: &'a [String],
    pub targets: &'a [String],
    pub idle_minutes: u64,
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
        || git(
            Path::new(&tree.path),
            &["rev-parse", "--symbolic-full-name", "HEAD"],
        )?
        .trim()
            != tree.branch.as_deref().unwrap_or("HEAD")
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
    if options.repo.as_os_str().is_empty()
        || options.state.as_os_str().is_empty()
        || options.roots.iter().any(|root| root.as_os_str().is_empty())
    {
        return Err(io::Error::other("empty sweep path"));
    }
    let repo = options.repo.canonicalize()?;
    record_io::ensure_private_dir(options.state)?;
    let state = options.state.canonicalize()?;
    let options = &Options {
        repo: &repo,
        state: &state,
        ..*options
    };
    let _lock = crate::lockfile::try_lock(&options.state.join("merged-sweep.lock"))
        .map_err(io::Error::other)?
        .ok_or_else(|| io::Error::other("another sweep is running"))?;
    // A successful refresh is mandatory even in dry run: stale refs cannot grant removal.
    git(options.repo, &["fetch", "--prune", "origin"])?;
    let mut report = String::new();
    let mut roots = Vec::new();
    for root in options.roots {
        match root.canonicalize() {
            Ok(root) => roots.push(root),
            Err(error) if error.kind() == io::ErrorKind::NotFound => report.push_str(&format!(
                "protected root {}: root not found\n",
                root.display()
            )),
            Err(error) => return Err(error),
        }
    }
    for name in ["runs", "worktrees"] {
        if options.state.join(name).exists() {
            roots.push(options.state.join(name).canonicalize()?);
        }
    }
    let trees = inventory(options.repo)?;
    let mut restore = None;
    let mut blocked = std::collections::BTreeSet::new();
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
        if !roots.iter().any(|r| path.starts_with(r) && path != r) || tree.is_main {
            continue;
        }
        if !path
            .canonicalize()
            .is_ok_and(|p| roots.iter().any(|r| p.starts_with(r) && &p != r))
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
            if !options.targets.is_empty()
                && !options.targets.iter().any(|target| {
                    target
                        == &format!(
                            "{}:{}:{}",
                            current.path,
                            current.branch.as_deref().unwrap_or("-"),
                            current.head
                        )
                })
            {
                return Err(io::Error::other("outside exact targets"));
            }
            sweep_facts::terminal_run(options.state, path)?;
            let branch = current.branch.as_deref().unwrap_or(&current.head);
            let (protection, prs) = authority(options, branch)?;
            let facts =
                sweep_facts::facts(options.repo, current, &protection, &prs, SystemTime::now())?;
            let classification = pg_reap::classify_with_clean_age(
                current,
                &facts,
                &fresh,
                sweep_facts::idle_minutes(current, SystemTime::now())? >= options.idle_minutes,
            );
            if !facts.clean || !matches!(classification, Classification::Eligible { .. }) {
                blocked.insert(current.branch.clone().unwrap_or_default());
                report.push_str(&format!("protected {}: {:?}\n", tree.path, classification));
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
                if options.delete_remote
                    && options.remote_targets.is_empty()
                    && merged_pr.is_some()
                    && current.branch.is_some()
                {
                    // Lease guards a remote branch that advanced after the PR merged.
                    let remote_ref = branch.to_string();
                    remote_restore(options.repo, &remote_ref, &tree.head, file)?;
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
            blocked.insert(tree.branch.clone().unwrap_or_default());
            report.push_str(&format!("protected {}: {error}\n", tree.path));
        }
    }
    for target in options.remote_targets {
        let result = (|| -> io::Result<()> {
            let (branch, sha) = target
                .rsplit_once(':')
                .ok_or_else(|| io::Error::other("invalid remote target"))?;
            let reference = format!("refs/heads/{branch}");
            if branch == "main"
                || branch == "master"
                || blocked.contains(&reference)
                || sha.len() != 40
                || !sha.bytes().all(|b| b.is_ascii_hexdigit())
            {
                return Err(io::Error::other("protected remote target"));
            }
            git(options.repo, &["check-ref-format", &reference])?;
            if branch
                .split('/')
                .any(|b| b == "plangraph-md-sync" || b.starts_with("live-"))
            {
                return Err(io::Error::other("protected branch name"));
            }
            let (protection, prs) = authority(options, &reference)?;
            if protection
                .claims
                .branches
                .iter()
                .any(|b| b == branch || b == &reference)
                || prs.iter().any(|p| p.state == "OPEN")
                || git(
                    options.repo,
                    &[
                        "rev-list",
                        "--count",
                        sha,
                        "--not",
                        "refs/remotes/origin/main",
                    ],
                )?
                .trim()
                    != "0"
            {
                return Err(io::Error::other(
                    "remote claim, open PR or unmerged commits",
                ));
            }
            for tree in inventory(options.repo)?
                .iter()
                .filter(|t| t.branch.as_deref() == Some(&reference))
            {
                let facts =
                    sweep_facts::facts(options.repo, tree, &protection, &prs, SystemTime::now())?;
                sweep_facts::terminal_run(options.state, Path::new(&tree.path))?;
                if tree.head != sha
                    || !facts.clean
                    || !matches!(
                        pg_reap::classify_with_clean_age(
                            tree,
                            &facts,
                            &inventory(options.repo)?,
                            sweep_facts::idle_minutes(tree, SystemTime::now())?
                                >= options.idle_minutes
                        ),
                        Classification::Eligible { .. }
                    )
                {
                    return Err(io::Error::other("local branch worktree protected"));
                }
            }
            // Explicit landed targets allow batch-closed PRs; containment and lease remain mandatory.
            if let Some(file) = &mut restore {
                remote_restore(options.repo, &reference, sha, file)?;
                // Only delete a local ref without a checkout, with the same SHA lease.
                if !inventory(options.repo)?
                    .iter()
                    .any(|t| t.branch.as_deref() == Some(&reference))
                {
                    let refs = git(
                        options.repo,
                        &["for-each-ref", "--format=%(refname)", &reference],
                    )?;
                    if refs.lines().any(|r| r == reference) {
                        git(options.repo, &["update-ref", "-d", &reference, sha])?;
                    }
                }
                git(
                    options.repo,
                    &[
                        "push",
                        &format!("--force-with-lease={reference}:{sha}"),
                        "origin",
                        &format!(":{reference}"),
                    ],
                )?;
            }
            report.push_str(&format!(
                "{} remote {branch}\n",
                if options.apply {
                    "removed"
                } else {
                    "would-remove"
                }
            ));
            Ok(())
        })();
        if let Err(error) = result {
            report.push_str(&format!("protected remote {target}: {error}\n"));
        }
    }
    Ok(report)
}

fn remote_restore(repo: &Path, reference: &str, sha: &str, file: &mut fs::File) -> io::Result<()> {
    if crate::disk_hygiene::Guards::protected_name(Path::new(reference)) {
        return Err(io::Error::other("protected remote branch name"));
    }
    let tree = Worktree {
        path: "-".into(),
        branch: Some(reference.into()),
        head: sha.into(),
        ..Worktree::default()
    };
    file.write_all(restore_row(repo, &tree, None).as_bytes())?;
    file.sync_all()
}
