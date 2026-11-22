//! Approval-gated worktree reaping uses the live sweep's evidence and policy.
use crate::{
    disk_hygiene::git,
    merged_sweep::{self, Options},
    reap_journal::{self, private_directory, Approval, Receipt, Row, RETENTION},
    sweep_facts::{self, Protection, Pull},
};
use pg_reap::{Classification, Worktree};
use std::{
    collections::BTreeMap,
    fmt::Write as _,
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    rc::Rc,
    time::{Duration, SystemTime},
};

/// The approved plan contains only the JSON rows printed for eligible entries.
pub fn run(options: &Options<'_>, approval: Option<(&str, &str)>) -> io::Result<String> {
    run_with_readers(
        options,
        approval,
        |options| {
            Protection::load_with_processes(
                options.pins,
                options.state,
                options.claims_reader,
                crate::disk_runtime::process_references,
            )
        },
        sweep_facts::pull_inventory,
    )
}

pub(crate) type Evidence = (Rc<Protection>, Rc<Vec<Pull>>);

#[cfg(feature = "test-support")]
pub fn reap_with_authority(
    options: &Options<'_>,
    approval: Option<(&str, &str)>,
    mut authority: impl FnMut(&Options<'_>, &str) -> io::Result<(Protection, Vec<Pull>)>,
) -> io::Result<String> {
    run_using(options, approval, |options, branch| {
        authority(options, branch).map(|(protection, pulls)| (Rc::new(protection), Rc::new(pulls)))
    })
}

#[cfg(feature = "test-support")]
pub fn reap_with_readers(
    options: &Options<'_>,
    approval: Option<(&str, &str)>,
    protection: impl FnMut(&Options<'_>) -> io::Result<Protection>,
    pulls: impl FnMut(&Path, Option<&str>) -> io::Result<Vec<Pull>>,
) -> io::Result<String> {
    run_with_readers(options, approval, protection, pulls)
}

fn run_with_readers(
    options: &Options<'_>,
    approval: Option<(&str, &str)>,
    mut protection: impl FnMut(&Options<'_>) -> io::Result<Protection>,
    mut pulls: impl FnMut(&Path, Option<&str>) -> io::Result<Vec<Pull>>,
) -> io::Result<String> {
    let mut snapshot = None;
    let mut pull_cache = BTreeMap::new();
    run_using(options, approval, |options, reference| {
        let branch = reference.strip_prefix("refs/heads/");
        if approval.is_some() {
            return Ok((
                Rc::new(protection(options)?),
                Rc::new(pulls(options.repo, branch)?),
            ));
        }
        let protection = cached(snapshot.get_or_insert_with(|| protection(options).map(Rc::new)))?;
        // All detached heads share one complete open-PR inventory.
        let pulls = cached(
            pull_cache
                .entry(branch.map(str::to_owned))
                .or_insert_with(|| pulls(options.repo, branch).map(Rc::new)),
        )?;
        Ok((protection, pulls))
    })
}

fn cached<T>(result: &io::Result<Rc<T>>) -> io::Result<Rc<T>> {
    result
        .as_ref()
        .map(Rc::clone)
        .map_err(|error| io::Error::new(error.kind(), error.to_string()))
}

fn within(path: &Path, roots: &[PathBuf]) -> bool {
    roots
        .iter()
        .any(|root| path != root && path.starts_with(root))
}

fn roots(options: &Options<'_>) -> io::Result<Vec<PathBuf>> {
    let mut roots = Vec::new();
    for root in options
        .roots
        .iter()
        .cloned()
        .chain(["runs", "worktrees"].map(|name| options.state.join(name)))
    {
        match root.canonicalize() {
            Ok(root) if root.parent().is_some() => roots.push(root),
            Ok(_) => return Err(io::Error::other("refusing filesystem root")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(roots)
}

fn run_using(
    options: &Options<'_>,
    approval: Option<(&str, &str)>,
    mut authority: impl FnMut(&Options<'_>, &str) -> io::Result<Evidence>,
) -> io::Result<String> {
    let approval = approval
        .map(|(id, plan)| Approval::parse(id, plan))
        .transpose()?;
    let repo = options.repo.canonicalize()?;
    let state = options.state.canonicalize()?;
    let options = &Options {
        repo: &repo,
        state: &state,
        ..*options
    };
    let _sweep = if approval.is_some() {
        Some(
            crate::lockfile::try_lock(&state.join("merged-sweep.lock"))
                .map_err(io::Error::other)?
                .ok_or_else(|| io::Error::other("another sweep is running"))?,
        )
    } else {
        None
    };
    git(&repo, &["fetch", "--prune", "origin"])?;
    let roots = roots(options)?;
    let quarantine = state.join("reap-quarantine");
    if quarantine.symlink_metadata().is_ok() {
        private_directory(&quarantine)?;
    }
    let inventory = merged_sweep::inventory(&repo)?;
    let mut report = protected_storage(&roots, &inventory);
    for tree in &inventory {
        let path = Path::new(&tree.path);
        if tree.is_main || (!path.starts_with(&quarantine) && !within(path, &roots)) {
            continue;
        }
        let outcome = (|| -> io::Result<String> {
            let staged = receipt(path, &quarantine, &repo, tree, &roots)?;
            let original = staged
                .as_ref()
                .map_or(path, |(_, r)| r.row.original.as_path());
            crate::sweep_runs::check_original(options.state, original)?;
            let branch = tree.branch.as_deref().unwrap_or(&tree.head);
            reap_journal::directory(path)?;
            reap_journal::check_mounts(path)?;
            if !sweep_facts::clean_status(path)? {
                return Err(io::Error::other("dirty"));
            }
            if staged.is_none() && !old_enough(tree, SystemTime::now())? {
                return Err(io::Error::other("idle-below-clean-threshold"));
            }
            if approval.is_none() {
                let (protection, pulls) = authority(options, branch)?;
                check(options, tree, &inventory, original, &protection, &pulls)?;
                return Ok(serde_json::to_string(&Row::new(
                    &repo, path, original, tree,
                )?)?);
            }
            let row = Row::new(&repo, path, original, tree)?;
            let approval = approval
                .as_ref()
                .filter(|plan| plan.rows.contains(&row))
                .ok_or_else(|| io::Error::other("outside approved plan"))?;
            let (protection, pulls) = authority(options, branch)?;
            check(options, tree, &inventory, original, &protection, &pulls)?;
            let _legacy = legacy_attempt_lock(original)?;
            let _creation = crate::worktree::mutation_lock(&repo).map_err(io::Error::other)?;
            let fresh = merged_sweep::inventory(&repo)?;
            let current = fresh
                .iter()
                .find(|w| w.path == tree.path && row.same_worktree(w))
                .ok_or_else(|| io::Error::other("worktree changed after approval"))?;
            crate::sweep_runs::check_original(options.state, original)?;
            let (protection, pulls) = authority(options, branch)?;
            check(options, current, &fresh, original, &protection, &pulls)?;
            merged_sweep::verify_checkout(current)?;
            if let Some((slot, _)) = staged {
                reap_journal::delete(options.state, &slot, approval.id, &row, || {
                    mutate_git(options.repo, &["worktree", "remove", "--", &current.path])
                })
            } else {
                reap_journal::stage(options.state, &quarantine, approval.id, &row, |dest| {
                    let dest = dest
                        .to_str()
                        .ok_or_else(|| io::Error::other("non-UTF-8 quarantine path"))?;
                    mutate_git(
                        options.repo,
                        &["worktree", "move", "--", &current.path, dest],
                    )
                })
            }
        })();
        match outcome {
            Ok(line) => writeln!(report, "{line}").unwrap(),
            Err(error) => {
                writeln!(report, "protected {}: {error}", tree.path.escape_debug()).unwrap()
            }
        }
    }
    crate::reap_run_dirs::report(
        options,
        &quarantine,
        approval.as_ref(),
        &mut authority,
        &mut report,
    )?;
    Ok(report)
}

fn mutate_git(repo: &Path, args: &[&str]) -> io::Result<()> {
    let result = crate::host_command::capture(
        Command::new("git").current_dir(repo).args(args),
        Duration::from_secs(30),
    )?;
    if !result.status.success() {
        return Err(io::Error::other(
            String::from_utf8_lossy(&result.stderr).trim().to_string(),
        ));
    }
    Ok(())
}

fn legacy_attempt_lock(path: &Path) -> io::Result<Option<crate::lockfile::LockGuard>> {
    if !path
        .file_name()
        .is_some_and(|n| n.to_string_lossy().starts_with("attempt-"))
    {
        return Ok(None);
    }
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("attempt root unavailable"))?;
    reap_journal::directory(parent)?;
    let lock = parent.join(".lock");
    if lock.symlink_metadata().is_ok_and(|m| !m.is_file()) {
        return Err(io::Error::other("unsafe attempt creation lock"));
    }
    crate::lockfile::try_lock(&lock)
        .map_err(io::Error::other)?
        .map(Some)
        .ok_or_else(|| io::Error::other("attempt creation in progress"))
}

fn protected_storage(roots: &[PathBuf], inventory: &[Worktree]) -> String {
    let mut report = String::new();
    let mut targets = crate::cargo_cache::shared_cache_roots().unwrap_or_default();
    if let Some(target) = std::env::var_os("CARGO_TARGET_DIR") {
        targets.push(target.into());
    }
    targets.sort();
    targets.dedup();
    for target in targets {
        if target.exists() {
            writeln!(
                report,
                "protected cargo-target {}: managed by disk hygiene",
                target.to_string_lossy().escape_debug()
            )
            .unwrap();
        }
    }
    let mut clones = std::collections::BTreeSet::new();
    clones.extend(
        inventory
            .iter()
            .filter(|tree| tree.is_main)
            .map(|tree| PathBuf::from(&tree.path)),
    );
    for root in roots {
        if let Ok(entries) = fs::read_dir(root) {
            clones.extend(
                entries
                    .flatten()
                    .map(|e| e.path())
                    .filter(|p| p.join(".git").symlink_metadata().is_ok_and(|m| m.is_dir())),
            );
        }
    }
    for path in clones {
        writeln!(
            report,
            "protected push-clone {}: repository clone",
            path.to_string_lossy().escape_debug()
        )
        .unwrap();
    }
    report
}

fn check(
    options: &Options<'_>,
    tree: &Worktree,
    inventory: &[Worktree],
    original: &Path,
    protection: &Protection,
    pulls: &[Pull],
) -> io::Result<()> {
    let path = Path::new(&tree.path);
    reap_journal::directory(path)?;
    reap_journal::check_mounts(path)?;
    let now = SystemTime::now();
    let facts = sweep_facts::facts(options.repo, tree, protection, pulls, now)?;
    if !facts.clean {
        return Err(io::Error::other("dirty"));
    }
    if Path::new(&tree.path) != original {
        let overlaps = |p: &Path| p.starts_with(original) || original.starts_with(p);
        if !matches!(original.symlink_metadata(), Err(e) if e.kind() == io::ErrorKind::NotFound)
            || protection.guards.protects(original)
            || protection.claims.paths.iter().any(|p| overlaps(p))
            || protection.units.iter().any(|p| overlaps(p))
            || protection.unit_refs.iter().any(|r| overlaps(&r.path))
            || protection.cwds.iter().any(|p| p.starts_with(original))
        {
            return Err(io::Error::other("original path is in use"));
        }
    }
    match pg_reap::classify_with_clean_age(
        tree,
        &facts,
        inventory,
        original != Path::new(&tree.path) || old_enough(tree, now)?,
    ) {
        Classification::Eligible { .. } => Ok(()),
        Classification::Protected { reason } => Err(io::Error::other(reason)),
    }
}

fn old_enough(tree: &Worktree, now: SystemTime) -> io::Result<bool> {
    Ok(sweep_facts::idle_minutes(tree, now)? > RETENTION.as_secs() / 60)
}

fn receipt(
    path: &Path,
    quarantine: &Path,
    repo: &Path,
    tree: &Worktree,
    roots: &[PathBuf],
) -> io::Result<Option<(PathBuf, Receipt)>> {
    if !path.starts_with(quarantine) {
        return Ok(None);
    }
    let slot = path
        .parent()
        .filter(|p| p.parent() == Some(quarantine))
        .filter(|_| path.file_name().is_some_and(|n| n == "worktree"))
        .ok_or_else(|| io::Error::other("unknown quarantine slot"))?;
    private_directory(slot)?;
    let receipt = Receipt::read(slot)?;
    if receipt.row.repo != repo
        || !within(&receipt.row.original, roots)
        || receipt.row.original.starts_with(quarantine)
        || !receipt.row.same_worktree(tree)
    {
        return Err(io::Error::other("quarantine receipt mismatch"));
    }
    if !receipt.ready(reap_journal::timestamp()?) {
        return Err(io::Error::other("quarantine-hold"));
    }
    Ok(Some((slot.into(), receipt)))
}
