use pg_worker_core::{
    disk_hygiene::{self, Guards},
    fleet_collect,
    run::record_io,
};
use std::{
    env, fs,
    io::{self, Write},
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
fn run() -> io::Result<()> {
    let mut args = env::args().skip(1);
    let (mut repo, mut root, mut pins) = (None, None, None);
    let mut state = env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state/plangraph"));
    let mut apply = false;
    while let Some(flag) = args.next() {
        if flag == "--apply" {
            apply = true;
            continue;
        }
        if flag == "--dry-run" {
            continue;
        }
        let value = args
            .next()
            .ok_or_else(|| io::Error::other("missing option value"))?;
        match flag.as_str() {
            "--repo" => repo = Some(PathBuf::from(value)),
            "--root" => root = Some(PathBuf::from(value)),
            "--pins" => pins = Some(PathBuf::from(value)),
            "--state" => state = Some(PathBuf::from(value)),
            _ => return Err(io::Error::other("unknown option")),
        }
    }
    let repo = repo.ok_or_else(|| io::Error::other("--repo required"))?;
    let root = root.ok_or_else(|| io::Error::other("--root required"))?;
    let pins = pins.ok_or_else(|| io::Error::other("--pins required"))?;
    let state = state.ok_or_else(|| io::Error::other("HOME or --state required"))?;
    let guards = Guards::load(&pins, &state)?;
    let disk =
        fleet_collect::disk_pct(&root).ok_or_else(|| io::Error::other("disk usage unavailable"))?;
    if apply && disk >= 80.0 {
        disk_hygiene::git(&repo, &["fetch", "--prune", "origin"])?;
    }
    let report = disk_hygiene::sweep(&repo, &root, &guards, SystemTime::now(), disk, apply)?;
    let mut value = serde_json::to_value(&report)?;
    value["ts"] = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_secs()
        .into();
    let bytes = serde_json::to_vec(&value)?;
    record_io::atomic_write_private(&state.join("disk-hygiene.json"), &bytes)?;
    let mut log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.join("disk-hygiene.ndjson"))?;
    writeln!(log, "{}", String::from_utf8_lossy(&bytes))?;
    println!("{}", String::from_utf8_lossy(&bytes));
    Ok(())
}
fn main() -> std::process::ExitCode {
    if std::env::args().nth(1).as_deref() == Some("sweep-merged") {
        return pg_worker_core::host_cleanup_cli::sweep_merged();
    }
    if std::env::args().nth(1).as_deref() == Some("prod-paths") {
        return pg_worker_core::host_cleanup_cli::prod_paths();
    }
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ALERT disk hygiene: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
