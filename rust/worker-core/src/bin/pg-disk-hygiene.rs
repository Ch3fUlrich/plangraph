use pg_worker_core::{
    disk_hygiene::{self, Guards, Report, Thresholds},
    disk_runtime,
    run::record_io,
    sweep_facts,
};
use std::{
    env, io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

fn execute(args: &[String], state: &std::path::Path) -> io::Result<Report> {
    let (mut repo, mut pins) = (None, None);
    let mut roots = Vec::new();
    let (mut apply, mut dry) = (false, false);
    let mut thresholds = Thresholds::default();
    let mut cap = 10_000_000_000u64;
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
                        .filter(|value| !value.is_empty())
                        .ok_or_else(|| io::Error::other("missing option value"))?,
                );
                match flag.as_str() {
                    "--repo" => repo = Some(value),
                    "--root" => roots.push(value),
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
    if roots.is_empty() {
        return Err(io::Error::other("--root required"));
    }
    let pins = pins.ok_or_else(|| io::Error::other("--pins required"))?;
    let thresholds = thresholds.validate()?;
    roots.extend(pg_worker_core::work_disks::configured_roots());
    roots.extend(
        ["runs", "worktrees"]
            .map(|name| state.join(name))
            .into_iter()
            .filter(|path| path.is_dir()),
    );
    let roots = roots
        .into_iter()
        .map(|root| {
            let path = root.canonicalize()?;
            let space = disk_runtime::disk_space(&path)?;
            Ok((path, space))
        })
        .collect::<io::Result<std::collections::BTreeMap<_, _>>>()?;
    let mut guards = Guards::load(&pins, state)?;
    // A worktree a user unit runs from keeps its caches: the build the unit starts needs them.
    let home = std::env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?;
    guards.pinned.extend(
        sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, Path::new(&home))?
            .into_iter()
            .map(|r| r.path),
    );
    let (cwds, building) = disk_runtime::processes()?;
    guards.live.extend(
        cwds.into_iter()
            .filter(|path| roots.keys().any(|root| path.starts_with(root))),
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
    for (root, space) in &roots {
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
                    let (cwds, building) = disk_runtime::processes()?;
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
        private_guards
            .pinned
            .extend(pg_worker_core::cargo_cache::shared_cache_roots()?);
        let temp = env::temp_dir();
        let tmp = Path::new("/tmp");
        for path in std::iter::once(temp.as_path()).chain((temp != tmp).then_some(tmp)) {
            if let Err(error) = pg_worker_core::private_targets::sweep(
                path,
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
        for cache in pg_worker_core::cargo_cache::shared_cache_roots()? {
            let pressure = match disk_runtime::disk_space(&cache) {
                Ok(space) => space.free < thresholds.for_filesystem(space.total).cleanup_free_bytes,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            if let Err(error) = disk_runtime::shared_cache_capped(
                &cache,
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
    }
    Ok(report)
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
    let mut report = report;
    report["ts"] = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs()
        .into();
    let bytes = serde_json::to_vec(&report)?;
    record_io::atomic_write_private(&state.join("disk-hygiene.json"), &bytes)?;
    let mut line = bytes.clone();
    line.push(b'\n');
    record_io::append_private(&state.join("disk-hygiene.ndjson"), &line)?;
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
