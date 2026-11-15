use pg_worker_core::{
    merged_sweep::{self, Options},
    run::record_io,
};
use std::{
    env, fs, io,
    path::PathBuf,
    time::{Duration, SystemTime},
};
fn run() -> io::Result<()> {
    let (mut repo, mut roots, mut pins) = (None, Vec::new(), None);
    let mut state = env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state/plangraph"));
    let mut reader = env::var_os("PG_SWEEP_CLAIMS_READER").map(PathBuf::from);
    let (mut apply, mut dry, mut daily, mut remote) = (false, false, false, false);
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
