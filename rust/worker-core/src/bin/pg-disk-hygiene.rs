use pg_worker_core::{
    disk_hygiene::{self, Guards, Report, Thresholds},
    disk_runtime,
    run::record_io,
};
use std::{
    env, io,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

fn execute(args: &[String], state: &std::path::Path) -> io::Result<Report> {
    let (mut repo, mut root, mut pins) = (None, None, None);
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
    {
        let home = env::var_os("HOME")
            .ok_or_else(|| io::Error::other("HOME required for shared caches"))?;
        for name in [".cache/plangraph/cargo-target", ".cache/pg-target-po"] {
            if let Err(error) = disk_runtime::shared_cache_capped(
                &PathBuf::from(&home).join(name),
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
