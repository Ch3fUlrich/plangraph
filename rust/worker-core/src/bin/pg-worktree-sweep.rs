use pg_worker_core::{
    merged_sweep::{self, Options},
    run::record_io,
    sweep_facts,
};
use std::{
    env, fs, io,
    path::PathBuf,
    time::{Duration, SystemTime},
};
/// Fleet check: list every user unit whose worktree path no longer exists, naming the unit.
fn check_units() -> io::Result<bool> {
    let home = env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?;
    let refs =
        sweep_facts::unit_file_refs(&sweep_facts::systemd_user_dir()?, &PathBuf::from(home))?;
    let missing = sweep_facts::missing_worktree_refs(&refs);
    for r in &missing {
        println!("unit-worktree-missing {}: {}", r.unit, r.path.display());
    }
    Ok(missing.is_empty())
}
fn run() -> io::Result<()> {
    if env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--report-claude-jobs")) {
        let mut args = env::args_os().skip(2);
        let path = args
            .next()
            .filter(|path| !path.is_empty() && args.next().is_none())
            .ok_or_else(|| {
                io::Error::other("usage: pg-worktree-sweep --report-claude-jobs PATH")
            })?;
        let rows = pg_worker_core::claude_job_reports::candidates(
            &PathBuf::from(path),
            SystemTime::now(),
        )?;
        println!("{}", serde_json::to_string(&rows)?);
        return Ok(());
    }
    if env::args().nth(1).as_deref() == Some("--check-units") {
        return if check_units()? {
            Ok(())
        } else {
            Err(io::Error::other("a user unit names a missing worktree"))
        };
    }
    let (mut repo, mut roots, mut pins) = (None, Vec::new(), None);
    let mut state = env::var_os("PLANGRAPH_SPAWN_STATE")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state/plangraph")));
    let mut reader = env::var_os("PG_SWEEP_CLAIMS_READER").map(PathBuf::from);
    let (mut apply, mut dry, mut daily, mut remote) = (false, false, false, false);
    let mut include_merged = false;
    let mut remote_targets = Vec::new();
    let (mut targets, mut idle_minutes) = (Vec::new(), 60);
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--idle-minutes" => {
                idle_minutes = args
                    .next()
                    .ok_or_else(|| io::Error::other("missing idle minutes"))?
                    .parse::<u64>()
                    .map_err(io::Error::other)?
            }
            "--remote-target" => remote_targets.push(
                args.next()
                    .ok_or_else(|| io::Error::other("missing remote target"))?,
            ),
            "--target" => targets.push(
                args.next()
                    .ok_or_else(|| io::Error::other("missing target"))?,
            ),
            "--apply" => apply = true,
            "--dry-run" => dry = true,
            "--daily" => daily = true,
            "--delete-remote" => remote = true,
            "--include-merged" => include_merged = true,
            "--repo" | "--root" | "--pins" | "--state" | "--claims-reader" => {
                let path = PathBuf::from(
                    args.next()
                        .ok_or_else(|| io::Error::other("missing value"))?,
                );
                if path.as_os_str().is_empty() {
                    return Err(io::Error::other("empty path value"));
                }
                match flag.as_str() {
                    "--repo" => repo = Some(path),
                    "--root" => roots.push(path),
                    "--pins" => pins = Some(path),
                    "--state" => state = Some(path),
                    _ => reader = Some(path),
                }
            }
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    if apply && dry {
        return Err(io::Error::other("conflicting --apply --dry-run"));
    }
    if (!remote_targets.is_empty() && !remote) || (remote && !apply) {
        return Err(io::Error::other("--delete-remote requires --apply"));
    }
    let repo = repo.ok_or_else(|| io::Error::other("--repo required"))?;
    if roots.is_empty() {
        return Err(io::Error::other("--root required"));
    }
    roots.extend(pg_worker_core::work_disks::configured_roots());
    let pins = pins.ok_or_else(|| io::Error::other("--pins required"))?;
    let state = state.ok_or_else(|| io::Error::other("HOME or --state required"))?;
    let marker = state.join("merged-sweep.last");
    if daily && apply {
        match fs::metadata(&marker) {
            Ok(m)
                if SystemTime::now()
                    .duration_since(m.modified()?)
                    .unwrap_or_default()
                    < Duration::from_secs(86400) =>
            {
                return Ok(())
            }
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
    }
    print!(
        "{}",
        merged_sweep::run(&Options {
            repo: &repo,
            roots: &roots,
            targets: &targets,
            remote_targets: &remote_targets,
            idle_minutes,
            pins: &pins,
            state: &state,
            claims_reader: reader.as_deref(),
            apply,
            include_merged,
            delete_remote: remote
        })?
    );
    if daily && apply {
        record_io::atomic_write_private(&marker, b"completed\n")?;
    }
    Ok(())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("sweep: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
