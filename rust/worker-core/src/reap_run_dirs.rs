//! Terminal native run logs are eligible only after all recorded checkouts are gone.
use crate::{
    disk_hygiene,
    merged_sweep::Options,
    reap_journal::{self, Approval, Receipt, Row, RETENTION},
    reaper::Evidence,
    sweep_facts::{self, Protection, Pull},
    sweep_runs,
};
use std::{
    fmt::Write as _,
    fs, io,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::SystemTime,
};

pub(crate) fn report(
    options: &Options<'_>,
    quarantine: &Path,
    approval: Option<&Approval<'_>>,
    authority: &mut impl FnMut(&Options<'_>, &str) -> io::Result<Evidence>,
    report: &mut String,
) -> io::Result<()> {
    let root = options.state.join("runs");
    let checkouts = checkout_paths(options, quarantine)?;
    let mut candidates = entries(&root)?
        .into_iter()
        .filter(|p| {
            p.file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("run-"))
        })
        .map(|p| (p, None))
        .collect::<Vec<_>>();
    for slot in entries(quarantine)? {
        let path = slot.join("run-dir");
        if path.symlink_metadata().is_ok() {
            candidates.push((path, Some(slot)));
        }
    }
    for (path, slot) in candidates {
        let outcome = (|| -> io::Result<String> {
            let original = original(&path, slot.as_deref(), &root, options.repo)?;
            let row = inspect(options, &path, &original, &checkouts)?;
            let reference = row.branch.as_deref().unwrap_or(&row.head);
            if approval.is_some_and(|plan| !plan.rows.contains(&row)) {
                return Err(io::Error::other("outside approved plan"));
            }
            let (protection, pulls) = authority(options, reference)?;
            check_protection(&row, &protection, &pulls)?;
            let Some(approval) = approval else {
                return Ok(serde_json::to_string(&row)?);
            };
            let _creation =
                crate::worktree::mutation_lock(options.repo).map_err(io::Error::other)?;
            let _writer = lock_writer(&path)?;
            if inspect(
                options,
                &path,
                &original,
                &checkout_paths(options, quarantine)?,
            )? != row
            {
                return Err(io::Error::other("run directory changed after approval"));
            }
            let (protection, pulls) = authority(options, reference)?;
            check_protection(&row, &protection, &pulls)?;
            if let Some(slot) = &slot {
                reap_journal::delete(options.state, slot, approval.id, &row, || {
                    fs::remove_dir_all(&path)
                })
            } else {
                reap_journal::stage(options.state, quarantine, approval.id, &row, |dest| {
                    fs::rename(&path, dest)
                })
            }
        })();
        match outcome {
            Ok(line) => writeln!(report, "{line}").unwrap(),
            Err(error) => writeln!(
                report,
                "protected run-dir {}: {error}",
                path.to_string_lossy().escape_debug()
            )
            .unwrap(),
        }
    }
    Ok(())
}

fn entries(root: &Path) -> io::Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(error) => return Err(error),
    };
    reap_journal::directory(root)?;
    let mut paths = entries
        .map(|entry| entry.map(|e| e.path()))
        .collect::<io::Result<Vec<_>>>()?;
    paths.sort();
    Ok(paths)
}

fn original(path: &Path, slot: Option<&Path>, root: &Path, repo: &Path) -> io::Result<PathBuf> {
    let Some(slot) = slot else {
        return Ok(path.into());
    };
    reap_journal::private_directory(slot)?;
    let receipt = Receipt::read(slot)?;
    if receipt.row.repo != repo
        || receipt.row.kind != "run-dir"
        || receipt.row.original.parent() != Some(root)
        || !receipt.row.same_directory(path)
    {
        return Err(io::Error::other("run quarantine receipt mismatch"));
    }
    if !receipt.ready(reap_journal::timestamp()?) {
        return Err(io::Error::other("quarantine-hold"));
    }
    if !matches!(receipt.row.original.symlink_metadata(), Err(e) if e.kind() == io::ErrorKind::NotFound)
    {
        return Err(io::Error::other("original run directory is in use"));
    }
    Ok(receipt.row.original)
}

fn inspect(
    options: &Options<'_>,
    path: &Path,
    original: &Path,
    checkouts: &[PathBuf],
) -> io::Result<Row> {
    let meta = reap_journal::directory(path)?;
    let record = path.join("record.json");
    if !record.symlink_metadata()?.is_file() {
        return Err(io::Error::other("run record is not a regular file"));
    }
    let bytes = fs::read(&record)?;
    let run: serde_json::Value = serde_json::from_slice(&bytes)?;
    if !sweep_runs::finished(&run)? {
        return Err(io::Error::other("nonterminal run"));
    }
    if run["repo"]
        .as_str()
        .and_then(|p| Path::new(p).canonicalize().ok())
        .as_deref()
        != Some(options.repo)
    {
        return Err(io::Error::other("run repository ownership unavailable"));
    }
    let expected = original.join("worktree");
    let paths = sweep_runs::paths(&run)?;
    if !paths.iter().any(|p| p == &expected) || paths.iter().any(|p| p != &expected) {
        return Err(io::Error::other("run checkout ownership unavailable"));
    }
    if !matches!(expected.symlink_metadata(), Err(e) if e.kind() == io::ErrorKind::NotFound) {
        return Err(io::Error::other("checkout still present"));
    }
    if checkouts.iter().any(|p| p.starts_with(original)) {
        return Err(io::Error::other(
            "registered or quarantined checkout still present",
        ));
    }
    let head = run["final_head"]
        .as_str()
        .filter(|sha| sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| io::Error::other("run commit unavailable"))?;
    disk_hygiene::git(
        options.repo,
        &[
            "merge-base",
            "--is-ancestor",
            head,
            "refs/remotes/origin/main",
        ],
    )
    .map_err(|_| io::Error::other("run commit is not verified on origin/main"))?;
    reap_journal::check_mounts(path)?;
    reject_nested_checkouts(path, meta.dev())?;
    if SystemTime::now()
        .duration_since(disk_hygiene::scan(path)?.1)
        .unwrap_or_default()
        <= RETENTION
    {
        return Err(io::Error::other("run directory is younger than seven days"));
    }
    let mut row = Row::for_path(options.repo, path, original, "run-dir")?;
    row.head = head.to_string();
    if let Some(branch) = run["lane_branch"].as_str().filter(|b| {
        !matches!(
            *b,
            "" | "main" | "master" | "refs/heads/main" | "refs/heads/master"
        )
    }) {
        let reference = if branch.starts_with("refs/heads/") {
            branch.into()
        } else {
            format!("refs/heads/{branch}")
        };
        disk_hygiene::git(options.repo, &["check-ref-format", &reference])?;
        if disk_hygiene::Guards::protected_name(Path::new(&reference)) {
            return Err(io::Error::other("protected run branch"));
        }
        row.branch = Some(reference);
    }
    row.record_hash = Some(crate::hash::hash_bytes(&bytes));
    Ok(row)
}

fn checkout_paths(options: &Options<'_>, quarantine: &Path) -> io::Result<Vec<PathBuf>> {
    let mut paths = crate::merged_sweep::inventory(options.repo)?
        .into_iter()
        .map(|w| PathBuf::from(w.path))
        .collect::<Vec<_>>();
    for slot in entries(quarantine)? {
        if slot.join("worktree").symlink_metadata().is_ok() {
            reap_journal::private_directory(&slot)?;
            paths.push(Receipt::read(&slot)?.row.original);
        }
    }
    Ok(paths)
}

fn reject_nested_checkouts(path: &Path, device: u64) -> io::Result<()> {
    if path.join("HEAD").is_file() && path.join("objects").is_dir() && path.join("refs").is_dir() {
        return Err(io::Error::other("nested bare Git repository"));
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        if entry.file_name() == ".git" || meta.dev() != device || meta.file_type().is_symlink() {
            return Err(io::Error::other("nested checkout, mount or symlink"));
        }
        if meta.is_dir() {
            reject_nested_checkouts(&entry.path(), device)?;
        }
    }
    Ok(())
}

fn check_protection(row: &Row, p: &Protection, pulls: &[Pull]) -> io::Result<()> {
    for path in [&row.path, &row.original] {
        let overlaps = |other: &Path| other.starts_with(path) || path.starts_with(other);
        if p.guards.protects(path)
            || p.claims.paths.iter().any(|p| overlaps(p))
            || p.units.iter().any(|p| overlaps(p))
            || p.unit_refs.iter().any(|r| overlaps(&r.path))
            || p.cwds.iter().any(|p| p.starts_with(path))
        {
            return Err(io::Error::other("run directory is in use"));
        }
    }
    if row.branch.as_ref().is_some_and(|branch| {
        p.claims
            .branches
            .iter()
            .any(|b| b == branch || branch.strip_prefix("refs/heads/") == Some(b.as_str()))
    }) {
        return Err(io::Error::other("run branch is claimed"));
    }
    if sweep_facts::has_open_pull(row.branch.as_deref(), &row.head, pulls) {
        return Err(io::Error::other("open PR"));
    }
    Ok(())
}

fn lock_writer(path: &Path) -> io::Result<Vec<crate::lockfile::LockGuard>> {
    [
        "writer-control.lock",
        "writer-supervisor.lock",
        "record.json.lock",
    ]
    .into_iter()
    .filter_map(|name| {
        let lock = path.join(name);
        match lock.symlink_metadata() {
            Ok(meta) if meta.is_file() => Some(
                crate::lockfile::try_lock(&lock)
                    .map_err(io::Error::other)
                    .and_then(|guard| {
                        guard.ok_or_else(|| io::Error::other("run writer lock held"))
                    }),
            ),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            _ => Some(Err(io::Error::other("invalid run writer lock"))),
        }
    })
    .collect()
}
