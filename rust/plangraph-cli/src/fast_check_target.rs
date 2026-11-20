use super::Result;
use pg_worker_core::cargo_pool;
use pg_worker_core::cargo_pool::{LockWaitSample, LockWaitTracker};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

pub(super) fn jail_binary(target: &Path) -> PathBuf {
    target
        .join("debug")
        .join(format!("pg-jail{}", std::env::consts::EXE_SUFFIX))
}

fn state_dir() -> Option<PathBuf> {
    std::env::var_os("PLANGRAPH_STATE_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state/plangraph"))
        })
}

/// Run one step, echo its stderr unchanged, and for cargo steps record how long it
/// waited on Cargo file locks (real milliseconds) in the per-host telemetry log.
pub(super) fn run_step(
    command: &mut Command,
    program: &str,
    lease: &cargo_pool::SlotLease,
) -> std::io::Result<std::process::ExitStatus> {
    if Path::new(program).file_name().and_then(|n| n.to_str()) != Some("cargo") {
        return command.status();
    }
    let started = Instant::now();
    let mut child = command.stderr(Stdio::piped()).spawn()?;
    let stderr = child.stderr.take().expect("piped stderr");
    let reader = std::thread::spawn(move || {
        let mut tracker = LockWaitTracker::default();
        let mut reader = std::io::BufReader::new(stderr);
        let mut line = Vec::new();
        while reader.read_until(b'\n', &mut line).unwrap_or(0) > 0 {
            let _ = std::io::stderr().write_all(&line);
            tracker.observe(
                &String::from_utf8_lossy(&line),
                started.elapsed().as_millis() as u64,
            );
            line.clear();
        }
        tracker
    });
    let status = child.wait()?;
    let build_ms = (started.elapsed().as_millis() as u64).max(1);
    let (blocking_events, lock_wait_ms) = reader
        .join()
        .map(|t| t.finish(build_ms))
        .unwrap_or_default();
    if let Some(state) = state_dir() {
        let _ = cargo_pool::record_lock_wait(
            &state,
            &LockWaitSample {
                host: cargo_pool::host_name(),
                run_id: lease.run_id.clone(),
                slot: lease.slot_index,
                build_ms,
                lock_wait_ms,
                blocking_events,
                ts: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(0, |d| d.as_secs()),
            },
        );
    }
    Ok(status)
}

pub(super) fn resolve_cargo_target(
    root: &Path,
    primary: &Path,
    run: &str,
) -> Result<cargo_pool::SlotLease> {
    let worktree = root.canonicalize().map_err(|e| e.to_string())?;
    let count = cargo_pool::configured_slot_count(primary);
    let slots = cargo_pool::pool_slots(primary, count).map_err(|e| e.to_string())?;
    // Only the paths the pool creates must be plain directories; an ancestor such as
    // `~/.cache` may legitimately be a symlink onto a work drive.
    let pool_root = cargo_pool::pool_root(primary).map_err(|e| e.to_string())?;
    for path in slots.iter().chain(std::iter::once(&pool_root)) {
        if path.starts_with(&worktree)
            || worktree.starts_with(path)
            || path
                .components()
                .any(|c| c == std::path::Component::ParentDir)
            || path.is_symlink()
        {
            return Err("cargo pool must be outside the checkout and must not be a symlink".into());
        }
    }
    let mut report = pg_worker_core::disk_hygiene::Report::default();
    let _ = cargo_pool::enforce_pool(
        primary,
        cargo_pool::configured_cap_bytes(),
        true,
        &mut report,
    );
    cargo_pool::acquire_slot(primary, slots.len(), run, None)
        .map_err(|e| format!("cargo pool: {e}"))
}

#[cfg(unix)]
#[test]
fn resolve_cargo_target_uses_shared_pool_instead_of_worktree_target() {
    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let primary = tmp.path().join("shared/cargo-target");

    let lease = resolve_cargo_target(&worktree, &primary, "fc-test").unwrap();
    assert_eq!(lease.path, primary);
    let second = resolve_cargo_target(&worktree, &primary, "fc-test-2").unwrap();
    assert_ne!(second.path, primary);
    assert!(!worktree.join("rust/target").exists());
    assert_eq!(
        jail_binary(&lease.path),
        primary.join(format!("debug/pg-jail{}", std::env::consts::EXE_SUFFIX))
    );
}

#[cfg(unix)]
#[test]
fn symlinked_ancestor_of_the_cache_is_allowed_but_a_symlinked_slot_is_not() {
    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let real = tmp.path().join("drive/cache");
    std::fs::create_dir_all(&real).unwrap();
    let linked = tmp.path().join("cache-link");
    std::os::unix::fs::symlink(&real, &linked).unwrap();
    let lease = resolve_cargo_target(&worktree, &linked.join("cargo-target"), "fc-link").unwrap();
    assert_eq!(lease.slot_index, 0);
    drop(lease);

    let primary = tmp.path().join("other/cargo-target");
    std::fs::create_dir_all(tmp.path().join("other")).unwrap();
    std::os::unix::fs::symlink(&real, &primary).unwrap();
    let err = resolve_cargo_target(&worktree, &primary, "fc-slot").unwrap_err();
    assert!(err.contains("symlink"), "{err}");
}

#[cfg(unix)]
#[test]
fn run_step_records_lock_wait_sample_only_for_cargo_steps() {
    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let lease =
        resolve_cargo_target(&worktree, &tmp.path().join("c/cargo-target"), "fc-rec").unwrap();
    let state = tmp.path().join("state");
    // SAFETY-free: the variable is read only by `state_dir` inside this test process.
    std::env::set_var("PLANGRAPH_STATE_DIR", &state);

    let bin = tmp.path().join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    let fake = bin.join("cargo");
    std::fs::write(
        &fake,
        "#!/bin/sh\necho 'Blocking waiting for file lock on build directory' >&2\nsleep 0.2\necho '   Compiling x' >&2\n",
    )
    .unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
    let mut command = Command::new(&fake);
    let status = run_step(&mut command, fake.to_str().unwrap(), &lease).unwrap();
    assert!(status.success());
    let summary = cargo_pool::summarize_lock_waits(&state).unwrap();
    let row = summary.values().next().expect("one host row");
    assert_eq!((row.builds, row.blocking_events), (1, 1));
    assert!(row.lock_wait_ms >= 150, "{row:?}");

    let mut other = Command::new("true");
    run_step(&mut other, "true", &lease).unwrap();
    let after = cargo_pool::summarize_lock_waits(&state).unwrap();
    assert_eq!(after.values().next().unwrap().builds, 1);
}
