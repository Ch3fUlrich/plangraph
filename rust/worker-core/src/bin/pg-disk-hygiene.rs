use pg_worker_core::{
    disk_hygiene::{self, Guards, Report, Thresholds},
    disk_pressure, disk_runtime, sweep_facts, work_disks,
};
use std::{
    env, fs, io,
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
    guards.pinned.extend(
        sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, Path::new(&home))?
            .into_iter()
            .map(|r| r.path),
    );
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
        let mut stray_error = shared
            .first()
            .and_then(|primary| primary.parent())
            .map(|cache| {
                pg_worker_core::private_targets::sweep_fast_check_targets(
                    cache,
                    &private_guards,
                    apply,
                    &mut report,
                )
            })
            .and_then(Result::err);
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
            let mut slots = pg_worker_core::cargo_pool::existing_pool_slots(primary);
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
        }
    }
    // Below the floor, idle Cargo targets go largest first; every kept one carries a reason,
    // and a run that frees nothing there fails with the largest kept paths named.
    let mut kept = Vec::new();
    let mut below_floor = false;
    let trees: Vec<PathBuf> = disk_hygiene::git(&repo, &["worktree", "list", "--porcelain"])?
        .lines()
        .filter_map(|line| line.strip_prefix("worktree "))
        .skip(1)
        .filter_map(|path| Path::new(path).canonicalize().ok())
        .collect();
    for (root, space) in &available {
        let floor = thresholds.for_filesystem(space.total).cleanup_free_bytes;
        if space.free >= floor {
            continue;
        }
        below_floor = true;
        let here = |path: &Path| same_device(path, root);
        let mut candidates = disk_pressure::worktree_targets(
            &trees
                .iter()
                .filter(|tree| tree.starts_with(root))
                .cloned()
                .collect::<Vec<_>>(),
        );
        for path in scratch.iter().filter(|path| here(path)) {
            candidates.extend(disk_pressure::scratch_targets(path));
        }
        if let Some(cache) = shared.first().and_then(|primary| primary.parent()) {
            if here(cache) {
                candidates.extend(disk_pressure::fast_check_targets(cache));
            }
        }
        let mut seen = std::collections::BTreeSet::new();
        candidates.retain(|c| seen.insert(c.target.clone()));
        kept.extend(disk_pressure::reclaim(
            &candidates,
            &guards,
            &refs,
            (space.free, floor),
            SystemTime::now(),
            apply,
            &mut report,
        ));
    }
    if below_floor && apply && report.freed_bytes == 0 {
        kept.sort_by(|a, b| b.bytes.cmp(&a.bytes).then_with(|| a.path.cmp(&b.path)));
        let alert = disk_pressure::stalled_alert(&kept);
        eprintln!("ALERT disk hygiene: {alert}");
        report.alarm = true;
        report.error.get_or_insert(alert);
    }
    Ok(report)
}
fn same_device(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    matches!((fs::metadata(a), fs::metadata(b)), (Ok(a), Ok(b)) if a.dev() == b.dev())
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
mod tests {
    use super::*;
    use std::{fs, time::Duration};

    #[test]
    fn cli_arguments_sweep_two_roots_even_with_an_offline_mount() {
        let fixture = tempfile::tempdir().unwrap();
        let path = |name| fixture.path().join(name);
        let repo = path("repo");
        fs::create_dir(&repo).unwrap();
        let git = |args: &[&str]| {
            pg_worker_core::host_command::output(
                pg_worker_core::git_test_support::command(&repo)
                    .args([
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=test@example.invalid",
                    ])
                    .args(args),
                Duration::from_secs(10),
            )
            .unwrap();
        };
        git(&["init", "-q"]);
        fs::write(repo.join(".gitignore"), "target/\n").unwrap();
        git(&["add", ".gitignore"]);
        git(&["commit", "-qm", "fixture"]);
        let mut expected = Vec::new();
        let roots = [path("first"), path("second")];
        let head = disk_hygiene::git(&repo, &["rev-parse", "HEAD"]).unwrap();
        let stamp = pg_worker_core::worktree::fast_check_stamp(&repo, head.trim()).unwrap();
        let stranded = stamp.with_file_name("check-stranded-fast");
        let siblings = [roots[0].join("lane"), roots[1].join("lane")];
        for tree in siblings.iter().chain([&stranded]) {
            fs::create_dir_all(tree.parent().unwrap()).unwrap();
            git(&["worktree", "add", "--detach", tree.to_str().unwrap()]);
            let target = tree.join("target");
            fs::create_dir(&target).unwrap();
            fs::write(target.join("cache"), "cached").unwrap();
            let past = SystemTime::now() - Duration::from_secs(25 * 3600);
            for file in [
                tree,
                &tree.join(".git"),
                &tree.join(".gitignore"),
                &target,
                &target.join("cache"),
            ] {
                fs::File::open(file).unwrap().set_modified(past).unwrap();
            }
            if tree != &stranded {
                expected.push(target);
            }
        }
        let pins = path("pins.json");
        fs::write(&pins, "[]").unwrap();
        let offline = path("offline");
        let args = [
            "--repo",
            repo.to_str().unwrap(),
            "--pins",
            pins.to_str().unwrap(),
            "--root",
            roots[0].to_str().unwrap(),
            "--root",
            roots[1].to_str().unwrap(),
            "--alarm-free-bytes",
            "1",
            "--cleanup-free-bytes",
            "18446744073709551615",
            "--dry-run",
        ]
        .map(str::to_owned);
        let report = execute_with(
            &args,
            fixture.path(),
            vec![offline.clone()],
            || Ok((vec![], false)),
            || Ok((vec![], vec![])),
        )
        .unwrap();
        assert!(report.alarm);
        assert!(report
            .skipped
            .iter()
            .any(|message| message.contains(offline.to_str().unwrap())));
        for target in expected {
            assert!(report
                .actions
                .iter()
                .any(|message| message.contains(target.to_str().unwrap())));
            assert!(target.is_dir());
        }
        fs::write(&stamp, &head).unwrap();
        let mut args = args;
        *args.last_mut().unwrap() = "--apply".into();
        let report = execute_with(
            &args,
            fixture.path(),
            vec![],
            || Ok((vec![], false)),
            || Ok((vec![], vec![])),
        )
        .unwrap();
        assert!(!stranded.exists(), "{report:?}");
        assert_eq!(fs::read_to_string(&stamp).unwrap(), head);
        let listing = disk_hygiene::git(&repo, &["worktree", "list", "--porcelain"]).unwrap();
        assert!(!listing.contains("check-stranded-fast"), "{listing}");
    }

    /// Two idle worktrees with a Cargo target each; `busy` names the trees a process works in.
    fn pressure_fixture() -> (tempfile::TempDir, PathBuf, PathBuf, Vec<String>) {
        let fixture = tempfile::tempdir().unwrap();
        let repo = fixture.path().join("repo");
        let root = fixture.path().join("trees");
        fs::create_dir_all(&repo).unwrap();
        fs::create_dir_all(&root).unwrap();
        let git = |args: &[&str]| {
            pg_worker_core::host_command::output(
                pg_worker_core::git_test_support::command(&repo)
                    .args([
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=test@example.invalid",
                    ])
                    .args(args),
                Duration::from_secs(10),
            )
            .unwrap();
        };
        git(&["init", "-qb", "main"]);
        fs::write(repo.join(".gitignore"), "target/\n").unwrap();
        git(&["add", ".gitignore"]);
        git(&["commit", "-qm", "fixture"]);
        git(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
        let past = SystemTime::now() - Duration::from_secs(3600);
        for name in ["tree-a", "tree-b"] {
            let tree = root.join(name);
            git(&["worktree", "add", "-qb", name, tree.to_str().unwrap()]);
            let target = tree.join("rust/target");
            fs::create_dir_all(target.join("debug")).unwrap();
            fs::write(target.join(".rustc_info.json"), "{}").unwrap();
            fs::write(target.join("debug/object"), vec![0u8; 4096]).unwrap();
            for file in [
                target.join("debug/object"),
                target.join("debug"),
                target.join(".rustc_info.json"),
                target.clone(),
            ] {
                fs::File::open(file).unwrap().set_modified(past).unwrap();
            }
        }
        let pins = fixture.path().join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let args = [
            "--repo",
            repo.to_str().unwrap(),
            "--pins",
            pins.to_str().unwrap(),
            "--root",
            root.to_str().unwrap(),
            "--alarm-free-bytes",
            "1",
            "--cleanup-free-bytes",
            "18446744073709551615",
            "--apply",
        ]
        .map(str::to_owned)
        .to_vec();
        (fixture, repo, root, args)
    }

    #[test]
    fn a_build_in_tree_a_does_not_defer_the_cleanup_of_idle_tree_b() {
        let (fixture, _repo, root, args) = pressure_fixture();
        let tree_a = root.canonicalize().unwrap().join("tree-a");
        let report = execute_with(
            &args,
            fixture.path(),
            vec![],
            // The host-wide "building" fact is true; only tree A has a process.
            || Ok((vec![tree_a.join("rust")], true)),
            || Ok((vec![], vec![])),
        )
        .unwrap();
        assert!(!root.join("tree-b/rust/target").exists(), "{report:?}");
        assert!(root.join("tree-a/rust/target").exists());
        assert!(
            report.freed_bytes > 0 && report.error.is_none(),
            "{report:?}"
        );
        assert!(
            report
                .skipped
                .iter()
                .any(|m| m.contains("tree-a") && m.contains("kept: busy")),
            "{:?}",
            report.skipped
        );
    }

    #[test]
    fn below_the_floor_with_freed_zero_the_run_fails_naming_kept_paths_and_reasons() {
        let (fixture, _repo, root, args) = pressure_fixture();
        let canonical = root.canonicalize().unwrap();
        let refs = vec![canonical.join("tree-a/rust"), canonical.join("tree-b")];
        let report = execute_with(
            &args,
            fixture.path(),
            vec![],
            || Ok((refs.clone(), true)),
            || Ok((vec![], vec![])),
        )
        .unwrap();
        assert_eq!(report.freed_bytes, 0);
        let error = report.error.expect("freed 0 below the floor is an error");
        assert!(
            error.contains("freed 0") && error.contains("tree-a") && error.contains("busy"),
            "{error}"
        );
        assert!(report.alarm);
    }
}
