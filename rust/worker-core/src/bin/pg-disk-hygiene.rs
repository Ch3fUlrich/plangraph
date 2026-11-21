use pg_worker_core::{
    disk_hygiene::{self, Guards, Report, Thresholds},
    disk_runtime, sweep_facts,
};
use std::{
    env, io,
    path::{Path, PathBuf},
    time::SystemTime,
};

fn execute(args: &[String], state: &std::path::Path) -> io::Result<Report> {
    let (mut repo, mut root, mut pins) = (None, None, None);
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
            "--repo" | "--root" | "--pins" | "--state" => {
                let value = PathBuf::from(
                    args.next()
                        .ok_or_else(|| io::Error::other("missing option value"))?,
                );
                match flag.as_str() {
                    "--repo" => repo = Some(value),
                    "--root" => root = Some(value),
                    "--pins" => pins = Some(value),
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
    let root = root.ok_or_else(|| io::Error::other("--root required"))?;
    let pins = pins.ok_or_else(|| io::Error::other("--pins required"))?;
    let thresholds = thresholds.validate()?;
    let free = disk_runtime::disk_usage(&root)?;
    let mut guards = Guards::load(&pins, state)?;
    // A worktree a user unit runs from keeps its caches: the build the unit starts needs them.
    let home = std::env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?;
    guards.pinned.extend(
        sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, Path::new(&home))?
            .into_iter()
            .map(|r| r.path),
    );
    let (cwds, building) = disk_runtime::processes()?;
    let canonical_root = root.canonicalize()?;
    guards
        .live
        .extend(cwds.into_iter().filter(|p| p.starts_with(&canonical_root)));
    let mut report = disk_hygiene::sweep(
        &repo,
        &root,
        &guards,
        SystemTime::now(),
        if building { u64::MAX } else { free },
        thresholds,
        apply && !building,
    )?;
    report.dry_run = !apply;
    report.free_bytes = free;
    report.alarm = free < thresholds.alarm_free_bytes;
    if building {
        report
            .skipped
            .push("build running: cache cleanup deferred".into());
    }
    if !building {
        let worktrees = disk_hygiene::git(&repo, &["worktree", "list", "--porcelain"])?
            .lines()
            .filter_map(|line| line.strip_prefix("worktree "))
            .map(PathBuf::from)
            .filter(|path| path.starts_with(&canonical_root))
            .collect::<Vec<_>>();
        let mut private_guards = Guards {
            pinned: guards.pinned.clone(),
            live: guards.live.clone(),
        };
        private_guards
            .pinned
            .extend(pg_worker_core::cargo_cache::shared_cache_roots()?);
        if let Err(error) = pg_worker_core::private_targets::sweep(
            &PathBuf::from("/tmp"),
            &worktrees,
            &private_guards,
            apply,
            &mut report,
        ) {
            report.alarm = true;
            report.error = Some(error.to_string());
        }
    }
    {
        for cache in pg_worker_core::cargo_cache::shared_cache_roots()? {
            if let Err(error) = disk_runtime::shared_cache_capped(
                &cache,
                &guards,
                building,
                cap,
                free < thresholds.cleanup_free_bytes,
                apply,
                &mut report,
            ) {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
        // Pool slots are guarded by their own leases, so idle ones are trimmed even while
        // another build runs; the primary target also waits for the process scan.
        if let Some(primary) = pg_worker_core::cargo_cache::shared_cache_roots()?.first() {
            let mut slots = pg_worker_core::cargo_pool::existing_pool_slots(primary);
            if building {
                slots.remove(0);
            }
            if let Err(error) =
                pg_worker_core::cargo_pool::enforce_pool_cap(&slots, cap, apply, &mut report)
            {
                report.alarm = true;
                report.error = Some(error.to_string());
            }
        }
    }
    Ok(report)
}
fn run() -> io::Result<()> {
    let args: Vec<_> = env::args().skip(1).collect();
    // Resolve the report destination before parsing/collecting any fallible facts.
    let state = args
        .windows(2)
        .rfind(|a| a[0] == "--state")
        .map(|a| PathBuf::from(&a[1]))
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
