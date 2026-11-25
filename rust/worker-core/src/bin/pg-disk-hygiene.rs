#[cfg(target_os = "linux")]
use pg_worker_core::disk_pressure;
use pg_worker_core::{
    disk_hygiene::{self, Guards, Report, Thresholds},
    disk_runtime, sweep_facts, work_disks,
};
use std::{
    env, io,
    path::{Path, PathBuf},
    time::SystemTime,
};

fn execute(args: &[String], state: &std::path::Path) -> io::Result<Report> {
    execute_with(
        args,
        state,
        work_disks::configured_mounts(),
        disk_runtime::process_references,
        || {
            let temp = env::temp_dir();
            let tmp = Path::new("/tmp");
            let scratch = std::iter::once(temp.as_path())
                .chain((temp != tmp).then_some(tmp))
                .map(Path::to_path_buf)
                .collect();
            Ok((scratch, pg_worker_core::cargo_cache::shared_cache_roots()?))
        },
    )
}

fn execute_with(
    args: &[String],
    state: &Path,
    mounts: Vec<PathBuf>,
    processes: impl Fn() -> io::Result<(Vec<PathBuf>, bool)>,
    caches: impl FnOnce() -> io::Result<(Vec<PathBuf>, Vec<PathBuf>)>,
) -> io::Result<Report> {
    let (mut repo, mut pins) = (None, None);
    let mut roots = Vec::new();
    let mut tmp_root = None;
    let (mut apply, mut dry) = (false, false);
    let mut thresholds = Thresholds::default();
    let mut cap = pg_worker_core::cargo_pool::configured_cap_bytes();
    let mut args = args.iter();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--alarm-free-bytes" | "--cleanup-free-bytes" | "--shared-target-cap-bytes" => {
                let value: u64 = args
                    .next()
                    .ok_or_else(|| io::Error::other("missing option value"))?
                    .parse()
                    .map_err(io::Error::other)?;
                match flag.as_str() {
                    "--alarm-free-bytes" => thresholds.alarm_free_bytes = value,
                    "--cleanup-free-bytes" => thresholds.cleanup_free_bytes = value,
                    _ => cap = value,
                }
            }
            "--apply" => apply = true,
            "--dry-run" => dry = true,
            "--repo" | "--root" | "--pins" | "--tmp-root" | "--state" => {
                let value = PathBuf::from(
                    args.next()
                        .filter(|value| !value.is_empty())
                        .ok_or_else(|| io::Error::other("missing option value"))?,
                );
                match flag.as_str() {
                    "--repo" => repo = Some(value),
                    "--root" => roots.push(value),
                    "--pins" => pins = Some(value),
                    "--tmp-root" => tmp_root = Some(value),
                    _ => {}
                }
            }
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    if apply && dry {
        return Err(io::Error::other("conflicting --apply --dry-run"));
    }
    let repo = repo.ok_or_else(|| io::Error::other("--repo required"))?;
    if roots.is_empty() {
        return Err(io::Error::other("--root required"));
    }
    let pins = pins.ok_or_else(|| io::Error::other("--pins required"))?;
    let thresholds = thresholds.validate()?;
    let mut guards = Guards::load(&pins, state)?;
    let configured: std::collections::BTreeMap<_, _> = mounts
        .into_iter()
        .map(|mount| (work_disks::worktree_root(&mount), mount))
        .collect();
    roots.extend(configured.keys().cloned());
    let fast_checks =
        pg_worker_core::worktree::fast_check_state_dir(&repo).map_err(io::Error::other)?;
    roots.extend(
        [state.join("runs"), state.join("worktrees"), fast_checks]
            .into_iter()
            .filter(|path| path.is_dir()),
    );
    let mut report = Report {
        free_bytes: u64::MAX,
        alarm: false,
        dry_run: !apply,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    let mut available = std::collections::BTreeMap::new();
    for root in roots.into_iter().collect::<std::collections::BTreeSet<_>>() {
        let sample = (|| -> io::Result<_> {
            if let Some(mount) = configured.get(&root) {
                return Ok((root.clone(), work_disks::mounted_space(mount)?));
            }
            let path = root.canonicalize()?;
            let space = disk_runtime::disk_space(&path)?;
            Ok((path, space))
        })();
        match sample {
            Ok((path, space)) => {
                available.insert(path, space);
            }
            Err(error) => {
                report.alarm = true;
                report
                    .skipped
                    .push(format!("{}: root unavailable: {error}", root.display()));
            }
        }
    }
    if available.is_empty() {
        return Err(io::Error::other(report.skipped.join("; ")));
    }
    let (mut scratch, shared) = caches()?;
    if let Some(root) = tmp_root {
        scratch = vec![root];
    }
    #[cfg(target_os = "linux")]
    pg_worker_core::disk_watermark::record_configured(
        state,
        available.keys().cloned(),
        &shared,
        &scratch,
        &mut report,
    );
    // A worktree a user unit runs from keeps its caches: the build the unit starts needs them.
    let home = std::env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?;
    let unit_pins: Vec<PathBuf> =
        sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, Path::new(&home))?
            .into_iter()
            .map(|r| r.path)
            .collect();
    guards.pinned.extend(unit_pins.iter().cloned());
    // Busy is decided per path from these references (working directory, open files,
    // executable, CARGO_TARGET_DIR); a build elsewhere on the host defers nothing.
    let (refs, _) = processes()?;
    guards.live.extend(
        refs.iter()
            .filter(|path| available.keys().any(|root| path.starts_with(root)))
            .cloned(),
    );
    for (root, space) in &available {
        let limits = thresholds.for_filesystem(space.total);
        report.free_bytes = report.free_bytes.min(space.free);
        if space.free < limits.alarm_free_bytes {
            report.alarm = true;
            eprintln!(
                "ALERT disk hygiene: {} free_bytes={} total_bytes={}",
                root.display(),
                space.free,
                space.total
            );
        }
        {
            let result = disk_hygiene::sweep_checked(
                &repo,
                root,
                &guards,
                SystemTime::now(),
                space.free,
                limits,
                apply,
                |tree| {
                    let (cwds, _) = processes()?;
                    Ok(!cwds.iter().any(|cwd| cwd.starts_with(tree))
                        && !Guards::load(&pins, state)?.protects(tree))
                },
            )?;
            report.freed_bytes = report.freed_bytes.saturating_add(result.freed_bytes);
            report.actions.extend(result.actions);
            report.skipped.extend(result.skipped);
        }
    }
    {
        let mut private_guards = Guards {
            pinned: guards.pinned.clone(),
            live: guards.live.clone(),
        };
        private_guards.pinned.extend(shared.iter().cloned());
        let worktrees = disk_hygiene::git(&repo, &["worktree", "list", "--porcelain"])?
            .lines()
            .filter_map(|line| line.strip_prefix("worktree "))
            .map(PathBuf::from)
            .filter(|path| available.keys().any(|root| path.starts_with(root)))
            .collect::<Vec<_>>();
        if let Err(error) = pg_worker_core::private_targets::sweep(
            None,
            &worktrees,
            &private_guards,
            apply,
            &mut report,
        ) {
            report.alarm = true;
            report.error = Some(error.to_string());
        }
        // Regenerable fast-check targets and finished job worktrees outside the roots.
        let mut stray_error =
            fast_check_caches(shared.first().map(PathBuf::as_path), Path::new(&home))
                .iter()
                .find_map(|cache| {
                    pg_worker_core::private_targets::sweep_fast_check_targets(
                        cache,
                        &private_guards,
                        apply,
                        &mut report,
                    )
                    .err()
                });
        if stray_error.is_none() {
            let covered: Vec<_> = available
                .keys()
                .cloned()
                .chain(configured.keys().cloned())
                .collect();
            stray_error = disk_hygiene::sweep_stray_worktrees(
                &repo,
                &covered,
                &guards,
                SystemTime::now(),
                apply,
                &mut report,
                |tree| {
                    let (cwds, _) = processes()?;
                    Ok(!cwds.iter().any(|cwd| cwd.starts_with(tree))
                        && !Guards::load(&pins, state)?.protects(tree))
                },
            )
            .err();
        }
        if let Some(error) = stray_error {
            report.alarm = true;
            report.error = Some(error.to_string());
        }
        private_guards.pinned.extend(available.keys().cloned());
        private_guards.pinned.extend(configured.keys().cloned());
        for path in &scratch {
            if let Err(error) = pg_worker_core::private_targets::sweep(
                Some(path),
                &[],
                &private_guards,
                apply,
                &mut report,
            ) {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
    }
    {
        for cache in &shared {
            let pressure = match disk_runtime::disk_space(cache) {
                Ok(space) => space.free < thresholds.for_filesystem(space.total).cleanup_free_bytes,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            if let Err(error) = disk_runtime::shared_cache_capped(
                cache,
                &guards,
                false,
                cap,
                pressure,
                apply,
                &mut report,
            ) {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
        // Pool slots are guarded by their own leases, so idle ones are trimmed even while
        // another build runs; the primary target also waits for the process scan.
        if let Some(primary) = shared.first() {
            // Review slots are one per seat run: they have their own, fixed budget below.
            let mut slots = pg_worker_core::cargo_pool::existing_pool_slots(primary);
            slots.retain(|slot| !pg_worker_core::cargo_pool::is_review_slot(slot));
            let primary_busy = refs.iter().any(|p| p.starts_with(primary));
            if primary_busy {
                slots.remove(0);
                report.skipped.push(format!(
                    "{}: kept: busy (process reference)",
                    primary.display()
                ));
            }
            if let Err(error) =
                pg_worker_core::cargo_pool::enforce_pool_cap(&slots, cap, apply, &mut report)
            {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
            let review = pg_worker_core::cargo_pool::existing_review_slots(primary);
            if let Err(error) =
                pg_worker_core::cargo_pool::enforce_review_slots(&review, cap, apply, &mut report)
            {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
    }
    // Lock files of review slots that no longer exist (a killed run leaves one) go after an hour.
    if let (true, Some(primary)) = (apply, shared.first()) {
        let mut primaries = vec![primary.clone()];
        if let Some(home) = env::var_os("HOME") {
            primaries.extend(pg_worker_core::cargo_cache::review_fallback_primary(
                primary,
                Path::new(&home),
            ));
        }
        for pool in primaries
            .iter()
            .filter_map(|p| pg_worker_core::cargo_pool::pool_root(p).ok())
        {
            pg_worker_core::cargo_pool::reap_orphan_review_locks(&pool);
        }
    }
    // Review seats whose shared target sits under the system temp directory build in the default
    // pool; its review slots are trimmed to the same cap (the pool's other slots are not ours).
    if let (Some(primary), Some(home)) = (shared.first(), env::var_os("HOME")) {
        if let Some(fallback) =
            pg_worker_core::cargo_cache::review_fallback_primary(primary, Path::new(&home))
        {
            let slots = pg_worker_core::cargo_pool::existing_review_slots(&fallback);
            if let Err(error) =
                pg_worker_core::cargo_pool::enforce_review_slots(&slots, cap, apply, &mut report)
            {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
    }
    // Below the floor, idle Cargo targets go largest first; every kept one carries a reason,
    // and a run that frees nothing there fails with the largest kept paths named.
    #[cfg(target_os = "linux")]
    {
        let roots: Vec<PathBuf> = available.keys().cloned().collect();
        let caches = fast_check_caches(shared.first().map(PathBuf::as_path), Path::new(&home));
        // The protection is read now and again under each target's lease, right before removal.
        let reload = || -> io::Result<disk_pressure::Protection> {
            let (refs, _) = processes()?;
            let mut guards = Guards::load(&pins, state)?;
            guards.pinned.extend(unit_pins.iter().cloned());
            guards.live.extend(
                refs.iter()
                    .filter(|path| roots.iter().any(|root| path.starts_with(root)))
                    .cloned(),
            );
            Ok(disk_pressure::Protection {
                guards,
                refs,
                mounts: disk_pressure::host_mounts()?,
            })
        };
        let protection = reload()?;
        reclaim_below_floor(
            &repo,
            &roots,
            thresholds,
            &scratch,
            &caches,
            (&protection, &reload),
            (
                &|path| disk_runtime::disk_space(path).map(|space| (space.free, space.total)),
                &device,
            ),
            apply,
            &mut report,
        )?;
    }
    Ok(report)
}
#[cfg(target_os = "linux")]
fn device(path: &Path) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(path).ok().map(|meta| meta.dev())
}

/// The cache directories that hold `fc-*` fast-check targets: beside the configured primary
/// target and the default `~/.cache/plangraph`, so moving the primary leaves none unswept.
fn fast_check_caches(primary: Option<&Path>, home: &Path) -> Vec<PathBuf> {
    let mut caches: Vec<PathBuf> = primary
        .and_then(Path::parent)
        .into_iter()
        .map(Path::to_path_buf)
        .collect();
    let default = home.join(".cache/plangraph");
    if !caches.contains(&default) {
        caches.push(default);
    }
    caches
}

/// Measures (free, total) bytes of the filesystem that holds a path.
#[cfg(target_os = "linux")]
type SpaceFn<'a> = dyn Fn(&Path) -> io::Result<(u64, u64)> + 'a;
/// The device a path is on.
#[cfg(target_os = "linux")]
type DeviceFn<'a> = dyn Fn(&Path) -> Option<u64> + 'a;

/// One reclaim per filesystem, grouped by the device each target is really on (a nested mount
/// under a root is its own group). `space` measures (free, total) of the filesystem holding a
/// path; it is called again after each removal, so cleanup that already restored the floor ends
/// the work for the group.
#[cfg(target_os = "linux")]
#[allow(clippy::too_many_arguments)]
fn reclaim_below_floor(
    repo: &Path,
    roots: &[PathBuf],
    thresholds: Thresholds,
    scratch: &[PathBuf],
    caches: &[PathBuf],
    (protection, reload): (&disk_pressure::Protection, &disk_pressure::Reload<'_>),
    (space, device): (&SpaceFn<'_>, &DeviceFn<'_>),
    apply: bool,
    report: &mut Report,
) -> io::Result<()> {
    let trees: Vec<PathBuf> = disk_hygiene::git(repo, &["worktree", "list", "--porcelain"])?
        .lines()
        .filter_map(|line| line.strip_prefix("worktree "))
        .skip(1)
        .filter_map(|path| Path::new(path).canonicalize().ok())
        .filter(|tree| roots.iter().any(|root| tree.starts_with(root)))
        .collect();
    let mut candidates = disk_pressure::worktree_targets(&trees);
    for path in scratch {
        candidates.extend(disk_pressure::scratch_targets(path));
    }
    for cache in caches {
        candidates.extend(disk_pressure::fast_check_targets(cache));
    }
    let mut seen = std::collections::BTreeSet::new();
    candidates.retain(|c| seen.insert(c.target.clone()));
    // One group per device: every configured root starts one (even with no candidate, so a full
    // disk with nothing to remove still reports), and each target joins the group of the device it
    // is really on. A group is measured through a directory that survives the removals.
    struct Group {
        probe: PathBuf,
        candidates: Vec<disk_pressure::Candidate>,
    }
    let mut devices = std::collections::BTreeMap::<u64, Group>::new();
    for root in roots {
        if let Some(dev) = device(root) {
            devices.entry(dev).or_insert_with(|| Group {
                probe: root.clone(),
                candidates: vec![],
            });
        }
    }
    for candidate in candidates {
        let Some(dev) = device(&candidate.target) else {
            continue;
        };
        // The parent survives the removal, but a target that is itself a mount point sits on
        // another device than its parent: then the target is what tells the group's free space.
        let probe = candidate
            .target
            .parent()
            .filter(|parent| device(parent) == Some(dev))
            .unwrap_or(&candidate.target)
            .to_path_buf();
        devices
            .entry(dev)
            .or_insert_with(|| Group {
                probe,
                candidates: vec![],
            })
            .candidates
            .push(candidate);
    }
    let mut kept = Vec::new();
    let mut below_floor = false;
    for group in devices.into_values() {
        let Ok((free, total)) = space(&group.probe) else {
            continue;
        };
        let floor = thresholds.for_filesystem(total).cleanup_free_bytes;
        if free >= floor {
            continue;
        }
        below_floor = true;
        kept.extend(disk_pressure::reclaim(
            &group.candidates,
            (protection, reload),
            (free, floor),
            &|| space(&group.probe).ok().map(|(free, _)| free),
            SystemTime::now(),
            apply,
            report,
        ));
    }
    if below_floor && apply && report.freed_bytes == 0 {
        kept.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
        let alert = disk_pressure::stalled_alert(&kept);
        eprintln!("ALERT disk hygiene: {alert}");
        report.alarm = true;
        report.error.get_or_insert(alert);
    }
    Ok(())
}
fn run() -> io::Result<()> {
    let args: Vec<_> = env::args().skip(1).collect();
    // Resolve the report destination before parsing/collecting any fallible facts.
    let state = args
        .windows(2)
        .rfind(|a| a[0] == "--state")
        .filter(|a| !a[1].is_empty())
        .map(|a| PathBuf::from(&a[1]))
        .or_else(|| {
            env::var_os("PLANGRAPH_SPAWN_STATE")
                .filter(|path| !path.is_empty())
                .map(PathBuf::from)
        })
        .or_else(|| env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state/plangraph")))
        .ok_or_else(|| io::Error::other("HOME or --state required to write report"))?;
    let result = execute(&args, &state);
    let report = match &result {
        Ok(report) => serde_json::to_value(report)?,
        Err(error) => {
            serde_json::json!({"alarm":true,"error":error.to_string(),"freed_bytes":0,"actions":[],"skipped":[],"dry_run":!args.iter().any(|a| a == "--apply"),"free_bytes":null})
        }
    };
    let bytes = disk_runtime::write_hygiene_report(&state, &report)?;
    println!("{}", String::from_utf8_lossy(&bytes));
    result.and_then(|r| match r.error {
        Some(e) => Err(io::Error::other(e)),
        None => Ok(()),
    })
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ALERT disk hygiene: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
#[path = "pg_disk_hygiene_tests/mod.rs"]
mod tests;
