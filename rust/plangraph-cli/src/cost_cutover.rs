//! Operator-invoked state import and minimum overlap guard. Never changes units.
use crate::atomic;
use chrono::{DateTime, Utc};
use std::fs;
use std::io;
use std::path::Path;
pub fn import(old: &Path, new: &Path, dry_run: bool) -> io::Result<usize> {
    if !old.exists() {
        return Ok(0);
    }
    if crate::migrate::relocated(old, new)? {
        return Ok(0);
    }
    if fs::symlink_metadata(old)?.file_type().is_symlink() || !old.is_dir() {
        return Err(io::Error::other("invalid legacy root"));
    }
    if dry_run {
        return Ok(["daily-gate.json", "daily-gate.status"]
            .iter()
            .filter(|name| old.join(name).is_file())
            .count());
    }
    let _migration_lock = crate::migrate::migration_lock(new)?;
    fs::create_dir_all(new)?;
    let marker = new.join(".daily-gate-imported");
    if marker.exists() {
        return Ok(0);
    }
    let _lock = atomic::lock(&marker)?;
    if marker.exists() {
        return Ok(0);
    }
    let _gate_lock = atomic::lock(&new.join("daily-gate.json"))?;
    let mut count = 0;
    for name in ["daily-gate.json", "daily-gate.status"] {
        let source = old.join(name);
        if !source.exists() {
            continue;
        }
        atomic::regular(&source)?;
        let target = new.join(name);
        if target.exists() {
            continue;
        }
        let bytes = fs::read(source)?;
        if name.ends_with("json") {
            let _: serde_json::Value = serde_json::from_slice(&bytes)?;
        }
        atomic::write(&target, &bytes)?;
        count += 1;
    }
    atomic::write(
        &marker,
        b"legacy gate imported; admission recomputes, never trusts imported state\n",
    )?;
    Ok(count)
}
pub fn overlap(state: &Path, start: bool, dry_run: bool, now: DateTime<Utc>) -> io::Result<bool> {
    let path = state.join("cost-gate-overlap-start");
    if start && !dry_run {
        fs::create_dir_all(state)?;
        let _lock = atomic::lock(&path)?;
        if !path.exists() {
            atomic::write(&path, now.to_rfc3339().as_bytes())?;
        }
    }
    atomic::regular(&path)?;
    let stamp = fs::read_to_string(path)
        .ok()
        .and_then(|s| DateTime::parse_from_rfc3339(&s).ok());
    Ok(stamp.is_some_and(|stamp| now.signed_duration_since(stamp).num_seconds() >= 86400))
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    let state = crate::paths::state(home);
    let dry_run = args.last().is_some_and(|s| s == "--dry-run");
    let arguments: Vec<_> = args.iter().map(String::as_str).collect();
    match arguments.as_slice() {
        ["migrate"] | ["migrate", "--dry-run"] => {
            match import(&state.parent().unwrap().join("autoos"), &state, dry_run) {
                Ok(count) => {
                    println!("cost-gate migration files={count}");
                    0
                }
                Err(_) => 2,
            }
        }
        ["overlap-start"] | ["overlap-start", "--dry-run"] | ["overlap-check"] => {
            let start = args[0] == "overlap-start";
            match overlap(&state, start, dry_run, Utc::now()) {
                Ok(ready) => {
                    println!(
                        "cost-gate overlap {}",
                        if ready { "ready" } else { "pending" }
                    );
                    if start || ready {
                        0
                    } else {
                        1
                    }
                }
                Err(_) => 2,
            }
        }
        _ => 2,
    }
}
