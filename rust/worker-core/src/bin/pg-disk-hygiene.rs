#[cfg(target_os = "linux")]
use pg_worker_core::{
    disk_hygiene::{
        self, Budget, Candidate, Guards, HygieneInventory, HygieneReport, ReclaimChecks, Thresholds,
    },
    disk_runtime, sweep_facts, work_disks,
};
#[cfg(target_os = "linux")]
use std::{
    env, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(target_os = "linux")]
fn execute(
    args: &[String],
    state: &Path,
    budget: &Budget<'_>,
    started: Duration,
) -> io::Result<HygieneReport> {
    execute_with(
        args,
        state,
        work_disks::configured_mounts(),
        budget,
        started,
        disk_runtime::hygiene_references,
        disk_runtime::disk_space,
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

#[cfg(target_os = "linux")]
#[allow(clippy::too_many_arguments)]
fn execute_with(
    args: &[String],
    state: &Path,
    mounts: Vec<PathBuf>,
    budget: &Budget<'_>,
    started: Duration,
    inventory: impl Fn(Instant) -> io::Result<HygieneInventory>,
    sample: impl Fn(&Path) -> io::Result<disk_runtime::DiskSpace>,
    caches: impl FnOnce() -> io::Result<(Vec<PathBuf>, Vec<PathBuf>)>,
) -> io::Result<HygieneReport> {
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
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME unset"))?;
    let mut report = HygieneReport::new(started, !apply);
    for mount in mounts {
        let root = work_disks::worktree_root(&mount);
        if work_disks::mounted_space(&mount).is_ok() {
            roots.push(root);
        } else {
            report.keep_boundary(root, "root_unavailable");
        }
    }
    if !repo.is_dir() {
        Guards::load(&pins, state)?;
    }
    let fast_checks =
        pg_worker_core::worktree::fast_check_state_dir(&repo).map_err(io::Error::other)?;
    roots.extend(
        [state.join("runs"), state.join("worktrees"), fast_checks]
            .into_iter()
            .filter(|path| path.is_dir()),
    );
    roots.retain_mut(|root| {
        let Ok(path) = root.canonicalize() else {
            report.keep_boundary(root.clone(), "root_unavailable");
            return false;
        };
        *root = path;
        true
    });
    let (mut scratch, shared) = caches()?;
    if let Some(root) = tmp_root {
        scratch = vec![root];
    }
    let repo = repo.canonicalize()?;
    let layout = disk_hygiene::Layout {
        repo: &repo,
        roots: &roots,
        scratch: &scratch,
        shared: &shared,
        home: &home,
        cap,
    };
    let tick = (budget.clock)();
    let initial = inventory(budget.start + budget.rank_for);
    let inventory_seconds = (budget.clock)().duration_since(tick).as_secs_f64();
    let candidates = layout.discover(
        &mut report,
        budget,
        thresholds,
        &sample,
        initial.as_ref().ok(),
    )?;
    let initial = std::cell::RefCell::new(Some(initial));
    let inventory = |end| {
        initial
            .borrow_mut()
            .take()
            .unwrap_or_else(|| inventory(end))
    };
    let protection = |item: &Candidate, end| {
        let mut guards = Guards::load(&pins, state)?;
        guards.pinned.extend(
            sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, &home)?
                .into_iter()
                .filter(|r| item.boundary != repo || r.path != repo)
                .map(|r| r.path),
        );
        if guards.pin_protects(&item.scope) {
            return Ok(Some("pinned"));
        }
        if guards.protects(&item.scope) {
            return Ok(Some("busy"));
        }
        layout.protection(item, state, end)
    };
    let free = |path: &Path| sample(path).map(|space| space.free);
    let checks = ReclaimChecks {
        protection: &protection,
        inventory: &inventory,
        space: &free,
    };
    let usable = std::cell::Cell::new(disk_hygiene::reclaim(
        &mut report,
        candidates,
        budget,
        &checks,
    ));
    report
        .stage_durations
        .insert("inventory", inventory_seconds);
    report.sample_devices(free);
    let end = budget.start + budget.pass_for;
    if usable.get() {
        for root in &roots {
            use std::os::unix::fs::MetadataExt;
            let id = root.metadata()?.dev();
            let Some(device) = report.devices.get_mut(&id) else {
                continue;
            };
            let floor = Some(device.cleanup_floor_bytes);
            let at_floor = device.start_free_bytes < floor && device.end_free_bytes >= floor;
            if at_floor || device.end_free_bytes.is_none() || (budget.clock)() >= end {
                continue;
            }
            let maintenance = disk_hygiene::sweep_checked(
                &repo,
                root,
                &Guards::default(),
                SystemTime::now(),
                u64::MAX,
                thresholds,
                apply,
                |tree| {
                    let item = Candidate::new(tree.to_path_buf(), root.clone(), SystemTime::now())?;
                    if !usable.get() || (budget.clock)() >= end || protection(&item, end)?.is_some()
                    {
                        return Ok(false);
                    }
                    Ok(!inventory(end)
                        .inspect_err(|_| usable.set(false))?
                        .blocks(tree))
                },
            )?;
            device.freed_bytes = device.freed_bytes.saturating_add(maintenance.freed_bytes);
            report.summary.actions.extend(maintenance.actions);
            report.summary.skipped.extend(maintenance.skipped);
            if maintenance.freed_bytes > 0 {
                report.sample_devices(free);
            }
        }
    }
    let diagnostic_start = (budget.clock)();
    if diagnostic_start < end {
        pg_worker_core::disk_watermark::record_configured(
            state,
            roots,
            &shared,
            &scratch,
            &mut report,
            || (budget.clock)() < end,
        );
    }
    let elapsed = (budget.clock)().duration_since(diagnostic_start);
    report
        .stage_durations
        .insert("diagnostics", elapsed.as_secs_f64());
    Ok(report)
}
#[cfg(target_os = "linux")]
fn run() -> io::Result<()> {
    let budget = Budget::new(Instant::now(), &Instant::now);
    let started = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?;
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
    let mut report = execute(&args, &state, &budget, started).unwrap_or_else(|error| {
        let mut report = HygieneReport::new(started, !args.iter().any(|a| a == "--apply"));
        report.summary.alarm = true;
        report.summary.error = Some(error.to_string());
        report
    });
    let (bytes, queued) =
        report.persist(&state, pg_worker_core::host_link::disk_hygiene_alert_host);
    if let Err(error) = &queued {
        eprintln!("ALERT disk hygiene enqueue failed: {error}");
    }
    println!("{}", String::from_utf8_lossy(&bytes?));
    queued?;
    report
        .summary
        .error
        .map_or(Ok(()), |error| Err(io::Error::other(error)))
}
fn main() -> std::process::ExitCode {
    #[cfg(not(target_os = "linux"))]
    fn run() -> std::io::Result<()> {
        Err(std::io::Error::other("disk hygiene requires Linux"))
    }
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ALERT disk hygiene: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
#[path = "pg_disk_hygiene/tests.rs"]
mod pressure_tests;

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
            &Budget::new(Instant::now(), &Instant::now),
            Duration::ZERO,
            |_| Ok(HygieneInventory::default()),
            disk_runtime::disk_space,
            || Ok((vec![], vec![])),
        )
        .unwrap();
        assert!(report.summary.alarm);
        assert!(report.kept.iter().any(|row| row.path.starts_with(&offline)));
        for target in expected {
            assert!(report
                .summary
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
            &Budget::new(Instant::now(), &Instant::now),
            Duration::ZERO,
            |_| Ok(HygieneInventory::default()),
            disk_runtime::disk_space,
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
        fs::write(path.join(".rustc_info.json"), r#"{"rustc_fingerprint":0}"#).unwrap();
        for (name, extra) in [("1111111111111111", 10_000), ("2222222222222222", 0)] {
            let unit = path.join("debug/build").join(format!("foo-{name}"));
            fs::create_dir_all(&unit).unwrap();
            fs::write(unit.join("output"), [0u8; 400]).unwrap();
            for file in [unit.join("output"), unit.clone()] {
                let at = old - Duration::from_secs(extra);
                fs::File::open(file).unwrap().set_modified(at).unwrap();
            }
        }
        fs::write(path.join("debug/.cargo-lock"), "").unwrap();
        for file in [
            ".rustc_info.json",
            "debug/.cargo-lock",
            "debug/build",
            "debug",
            "",
        ] {
            let file = path.join(file);
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
                &Budget::new(Instant::now(), &Instant::now),
                Duration::ZERO,
                |_| Ok(HygieneInventory::default()),
                |_| {
                    Ok(disk_runtime::DiskSpace {
                        free: 100,
                        total: 100,
                    })
                },
                || Ok((scratch, shared)),
            )
            .unwrap();
            let evictions = result
                .summary
                .actions
                .iter()
                .filter(|a| a.contains("pool_evict"))
                .count();
            assert_eq!(evictions, 2, "one per review pool: {result:?}");
            report = Some(result);
        }
        let report = report.unwrap();
        assert!(
            report
                .devices
                .values()
                .map(|row| row.freed_bytes)
                .sum::<u64>()
                >= 800,
            "{report:?}"
        );
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
