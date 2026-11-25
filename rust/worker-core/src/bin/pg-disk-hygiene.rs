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
        projected: Default::default(),
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
    let (cwds, building) = processes()?;
    guards.live.extend(
        cwds.into_iter()
            .filter(|path| available.keys().any(|root| path.starts_with(root))),
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
        if !building {
            let result = disk_hygiene::sweep_checked(
                &repo,
                root,
                &guards,
                SystemTime::now(),
                space.free,
                limits,
                apply,
                |tree| {
                    let (cwds, building) = processes()?;
                    Ok(!building
                        && !cwds.iter().any(|cwd| cwd.starts_with(tree))
                        && !Guards::load(&pins, state)?.protects(tree))
                },
            )?;
            report.freed_bytes = report.freed_bytes.saturating_add(result.freed_bytes);
            report.actions.extend(result.actions);
            report.skipped.extend(result.skipped);
        }
    }
    if building {
        report
            .skipped
            .push("build running: cache cleanup deferred".into());
    }
    if !building {
        let mut private_guards = Guards {
            pinned: guards.pinned.clone(),
            live: guards.live.clone(),
        };
        private_guards.pinned.extend(shared.iter().cloned());
        // Pool slots look like private Cargo targets; only the guarded pool trimmer may reach them.
        private_guards
            .pinned
            .extend(pool_roots(shared.first().map(PathBuf::as_path)));
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
        if let Err(error) = pg_worker_core::main_target::sweep_main_checkout_target(
            &repo,
            &private_guards,
            SystemTime::now(),
            apply,
            &mut report,
            &processes,
        ) {
            report.alarm = true;
            report.error = Some(error.to_string());
        }
        let legacy: Vec<PathBuf> = pg_worker_core::idle_targets::LEGACY_CONTAINERS
            .iter()
            .map(|relative| Path::new(&home).join(relative))
            .collect();
        if let Err(error) = pg_worker_core::idle_targets::sweep(
            &legacy,
            &private_guards,
            SystemTime::now(),
            apply,
            &mut report,
            &processes,
        ) {
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
                building,
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
        // another build runs; the primary target also waits for the process scan. Review slots
        // are one per seat run: they have their own fixed budget.
        if let Some(primary) = shared.first() {
            let (mut slots, review) =
                pg_worker_core::cargo_pool::pool_slot_sets(primary, &mut report);
            if building {
                slots.remove(0);
            }
            trim_pool(primary, &slots, &review, cap, apply, &mut report);
        }
    }
    // Review seats whose shared target sits under the system temp directory build in the default
    // pool; its review slots are trimmed to the same cap (the pool's other slots are not ours).
    if let (Some(primary), Some(home)) = (shared.first(), env::var_os("HOME")) {
        if let Some(fallback) =
            pg_worker_core::cargo_cache::review_fallback_primary(primary, Path::new(&home))
        {
            let (_, review) = pg_worker_core::cargo_pool::pool_slot_sets(&fallback, &mut report);
            trim_pool(&fallback, &[], &review, cap, apply, &mut report);
        }
    }
    Ok(report)
}
/// The pool root of the configured primary target and of the fallback pool review seats use.
fn pool_roots(primary: Option<&Path>) -> Vec<PathBuf> {
    let Some(primary) = primary else {
        return vec![];
    };
    let fallback = env::var_os("HOME").and_then(|home| {
        pg_worker_core::cargo_cache::review_fallback_primary(primary, Path::new(&home))
    });
    std::iter::once(primary.to_path_buf())
        .chain(fallback)
        .filter_map(|target| pg_worker_core::cargo_pool::pool_root(&target).ok())
        .collect()
}

/// Trims the ordinary and the review slots of the pool at `primary`; a failure is the run's alarm.
fn trim_pool(
    primary: &Path,
    ordinary: &[PathBuf],
    review: &[PathBuf],
    cap: u64,
    apply: bool,
    report: &mut Report,
) {
    let alarm = |report: &mut Report, result: io::Result<u64>| {
        if let Err(error) = result {
            report.alarm = true;
            report.error = Some(error.to_string());
        }
    };
    let result =
        pg_worker_core::cargo_pool::enforce_pool_cap(primary, ordinary, cap, apply, report);
    alarm(report, result);
    let result =
        pg_worker_core::cargo_pool::enforce_review_slots(primary, review, cap, apply, report);
    alarm(report, result);
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
    /// A cargo target slot of 802 bytes: a stale build unit and a newer one that stays.
    fn slot(path: &Path) {
        let old = SystemTime::now() - Duration::from_secs(2 * 86_400);
        fs::create_dir_all(path).unwrap();
        fs::write(path.join(".rustc_info.json"), "{}").unwrap();
        for (name, extra) in [("1111111111111111", 10_000), ("2222222222222222", 0)] {
            let unit = path.join("debug/build").join(format!("foo-{name}"));
            fs::create_dir_all(&unit).unwrap();
            fs::write(unit.join("output"), [0u8; 400]).unwrap();
            for file in [unit.join("output"), unit.clone()] {
                let at = old - Duration::from_secs(extra);
                fs::File::open(file).unwrap().set_modified(at).unwrap();
            }
        }
        for file in [path.join(".rustc_info.json"), path.to_path_buf()] {
            fs::File::open(file).unwrap().set_modified(old).unwrap();
        }
    }

    #[test]
    fn execute_with_trims_configured_and_fallback_review_pools() {
        // `HOME` places the fallback pool; no other test of this binary may read it meanwhile.
        static ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _env = ENV.lock().unwrap_or_else(|e| e.into_inner());
        let fixture = tempfile::tempdir().unwrap();
        let path = |name: &str| fixture.path().join(name);
        let repo = path("repo");
        fs::create_dir(&repo).unwrap();
        for args in [
            &["init", "-q"][..],
            &["commit", "-qm", "x", "--allow-empty"],
        ] {
            pg_worker_core::host_command::output(
                pg_worker_core::git_test_support::command(&repo)
                    .args(["-c", "user.name=F", "-c", "user.email=f@example.invalid"])
                    .args(args),
                Duration::from_secs(10),
            )
            .unwrap();
        }
        fs::write(path("pins.json"), "[]").unwrap();
        fs::create_dir(path("first")).unwrap();
        let home = path("home");
        let primary = path("cache/cargo-target");
        let fallback = home.join(".cache/plangraph/cargo-target");
        fs::create_dir_all(&primary).unwrap();
        fs::write(primary.join("file"), [0u8; 100]).unwrap();
        let pools: Vec<_> = [&primary, &fallback]
            .map(|target| pg_worker_core::cargo_pool::pool_root(target).unwrap())
            .into();
        for pool in &pools {
            for id in ["a", "b", "c", "d"] {
                slot(&pool.join(format!("slot-review-{id}")));
            }
            // An ordinary slot below its cap: only the review budget is this run's business.
            slot(&pool.join("slot-1"));
        }
        std::env::set_var("HOME", &home);
        let mut report = None;
        for flag in ["--dry-run", "--apply"] {
            let args = [
                "--repo",
                repo.to_str().unwrap(),
                "--pins",
                path("pins.json").to_str().unwrap(),
                "--root",
                path("first").to_str().unwrap(),
                "--alarm-free-bytes",
                "1",
                "--cleanup-free-bytes",
                "2",
                "--shared-target-cap-bytes",
                "1000",
                flag,
            ]
            .map(str::to_owned);
            let scratch = vec![path("cache")];
            let shared = vec![primary.clone()];
            let result = execute_with(
                &args,
                fixture.path(),
                vec![],
                || Ok((vec![], false)),
                || Ok((scratch, shared)),
            )
            .unwrap();
            let evictions = result
                .actions
                .iter()
                .filter(|a| a.contains("pool_evict"))
                .count();
            assert_eq!(evictions, 2, "one per review pool: {result:?}");
            report = Some(result);
        }
        let report = report.unwrap();
        assert!(report.freed_bytes >= 800, "{report:?}");
        for pool in &pools {
            let left = fs::read_dir(pool)
                .unwrap()
                .flatten()
                .filter(|e| e.file_name().to_string_lossy().starts_with("slot-review-"))
                .map(|e| pg_worker_core::disk_hygiene::scan(&e.path()).unwrap().0)
                .sum::<u64>();
            assert!(left <= 3000, "{left} bytes kept in {}", pool.display());
            assert!(pool.join("slot-1/debug").is_dir(), "ordinary slot kept");
            assert!(
                fs::read_dir(pool.join("slot-review-a")).is_ok(),
                "slot directories are the launch owner's"
            );
        }
    }
}
