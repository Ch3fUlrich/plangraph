use super::Result;
use pg_worker_core::cargo_pool;
use pg_worker_core::cargo_pool::{LockWaitSample, LockWaitTracker};
#[cfg(all(test, unix))]
use pg_worker_core::executable_test_support;
use pg_worker_core::stall::spawn_retrying;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const SLOT_WAIT_LIMIT: Duration = Duration::from_secs(30);
const SLOT_RETRY_INTERVAL: Duration = Duration::from_millis(200);

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
    run_step_with_state(command, program, lease, state_dir().as_deref())
}

fn run_step_with_state(
    command: &mut Command,
    program: &str,
    lease: &cargo_pool::SlotLease,
    state: Option<&Path>,
) -> std::io::Result<std::process::ExitStatus> {
    if Path::new(program).file_name().and_then(|n| n.to_str()) != Some("cargo") {
        return spawn_retrying(command)?.wait();
    }
    let started = Instant::now();
    let mut child = spawn_retrying(command.stderr(Stdio::piped()))?;
    let stderr = child.stderr.take().expect("piped stderr");
    let reader = std::thread::spawn(move || {
        observe_stderr(std::io::BufReader::new(stderr), || {
            started.elapsed().as_millis() as u64
        })
    });
    let status = child.wait()?;
    let build_ms = (started.elapsed().as_millis() as u64).max(1);
    let (blocking_events, lock_wait_ms) = reader
        .join()
        .map(|t| t.finish(build_ms))
        .unwrap_or_default();
    if let Some(state) = state {
        let _ = cargo_pool::record_lock_wait(
            state,
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

fn observe_stderr(mut reader: impl BufRead, mut now_ms: impl FnMut() -> u64) -> LockWaitTracker {
    let mut tracker = LockWaitTracker::default();
    let mut line = Vec::new();
    while reader.read_until(b'\n', &mut line).unwrap_or(0) > 0 {
        let _ = std::io::stderr().write_all(&line);
        tracker.observe(&String::from_utf8_lossy(&line), now_ms());
        line.clear();
    }
    tracker
}

pub(super) fn resolve_cargo_target(
    root: &Path,
    primary: &Path,
    run: &str,
) -> Result<cargo_pool::SlotLease> {
    resolve_with_cap(root, primary, run, cargo_pool::configured_cap_bytes())
}

fn resolve_with_cap(
    root: &Path,
    primary: &Path,
    run: &str,
    cap_bytes: u64,
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
    let _ = cargo_pool::enforce_pool(primary, cap_bytes, true, &mut report);
    acquire_slot_with_wait(primary, slots.len(), run, SLOT_WAIT_LIMIT)
}

/// Wait for a real pool lease so concurrent checks still respect the pool size.
fn acquire_slot_with_wait(
    primary: &Path,
    count: usize,
    run: &str,
    timeout: Duration,
) -> Result<cargo_pool::SlotLease> {
    let deadline = Instant::now() + timeout;
    let mut announced = false;
    loop {
        let busy = match cargo_pool::acquire_slot(primary, count, run, None) {
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => error,
            result => return result.map_err(|error| format!("cargo pool: {error}")),
        };
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(format!(
                "cargo pool: {busy}; timed out after {}s waiting for a slot",
                timeout.as_secs_f32()
            ));
        }
        if !announced {
            eprintln!(
                "fast-checks: {busy}; waiting up to {}s for a slot",
                timeout.as_secs_f32()
            );
            announced = true;
        }
        std::thread::sleep(remaining.min(SLOT_RETRY_INTERVAL));
    }
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
fn resolve_cargo_target_waits_when_all_pool_slots_are_busy() {
    use std::{sync::mpsc, thread};

    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let primary = tmp.path().join("cache/cargo-target");
    let count = cargo_pool::configured_slot_count(&primary);
    let mut leases: Vec<_> = (0..count)
        .map(|i| cargo_pool::acquire_slot(&primary, count, &format!("busy-{i}"), None).unwrap())
        .collect();
    let released = leases.last().unwrap().path.clone();
    let (tx, rx) = mpsc::channel();
    thread::scope(|scope| {
        scope.spawn(|| {
            let _ = tx.send(resolve_cargo_target(&worktree, &primary, "waiting-run"));
        });
        let early = rx.recv_timeout(Duration::from_millis(150));
        drop(leases.pop());
        assert!(matches!(early, Err(mpsc::RecvTimeoutError::Timeout)));
        let lease = rx.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
        assert_eq!(lease.path, released);
    });
}

#[cfg(unix)]
#[test]
fn wait_for_pool_slot_times_out_if_every_lease_stays_busy() {
    let tmp = tempfile::tempdir().unwrap();
    let primary = tmp.path().join("cargo-target");
    let _lease = cargo_pool::acquire_slot(&primary, 1, "busy", None).unwrap();
    let timeout = Duration::from_millis(25);
    let start = Instant::now();

    let error = acquire_slot_with_wait(&primary, 1, "waiting-run", timeout).unwrap_err();

    assert!(start.elapsed() >= timeout);
    assert!(error.contains("all 1 cargo pool slots are busy"), "{error}");
    assert!(error.contains("timed out"), "{error}");
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
    let stderr = b"Blocking waiting for file lock on build directory\n   Compiling x\n";
    let mut timestamps = [20, 180].into_iter();
    let elapsed = observe_stderr(stderr.as_slice(), || timestamps.next().unwrap());
    let (events, waited) = elapsed.finish(200);
    assert_eq!(events, 1);
    assert!(waited >= 150);

    let bin = tmp.path().join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    let fake = executable_test_support::write_script(
        &bin,
        "cargo",
        "#!/bin/sh\necho 'Blocking waiting for file lock on build directory' >&2\necho '   Compiling x' >&2\n",
    );
    let mut command = Command::new(&fake);
    let status =
        run_step_with_state(&mut command, fake.to_str().unwrap(), &lease, Some(&state)).unwrap();
    assert!(status.success());
    let summary = cargo_pool::summarize_lock_waits(&state).unwrap();
    assert_eq!(summary.len(), 1);
    let row = summary.values().next().expect("one host row");
    assert_eq!((row.builds, row.blocking_events), (1, 1));

    let mut other = Command::new("true");
    run_step_with_state(&mut other, "true", &lease, Some(&state)).unwrap();
    let after = cargo_pool::summarize_lock_waits(&state).unwrap();
    assert_eq!(after.len(), 1);
    assert_eq!(after.values().next().unwrap().builds, 1);
}

#[cfg(unix)]
#[test]
fn resolve_cargo_target_trims_an_over_cap_idle_slot_before_leasing() {
    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let primary = tmp.path().join("cache/cargo-target");
    let idle = tmp.path().join("cache/cargo-target-pool/slot-1");
    std::fs::create_dir_all(idle.join("debug")).unwrap();
    std::fs::write(idle.join("debug/blob"), vec![0u8; 16384]).unwrap();

    let lease = resolve_with_cap(&worktree, &primary, "fc-trim", 2048).unwrap();
    assert!(!idle.join("debug/blob").exists());
    assert_eq!(lease.path, primary);
}

#[test]
fn jail_binary_uses_the_built_helper_without_a_target_override() {
    let root = std::env::temp_dir();
    // Fast checks supply the resolved build target. Cover both Cargo's default
    // workspace layout and a configured cache with the same helper lookup.
    let suffix = format!("debug/pg-jail{}", std::env::consts::EXE_SUFFIX);
    assert_eq!(
        jail_binary(&root.join("rust/target")),
        root.join("rust/target").join(&suffix)
    );
    assert_eq!(
        jail_binary(&root.join("cache")),
        root.join("cache").join(suffix)
    );
}

#[cfg(unix)]
#[test]
fn executable_fixture_keeps_an_existing_staging_path() {
    let tmp = tempfile::tempdir().unwrap();
    let other = tmp.path().join(".cargo.tmp");
    std::fs::write(&other, "sibling fixture").unwrap();

    let script = executable_test_support::write_script(tmp.path(), "cargo", "#!/bin/sh\nexit 0\n");

    assert_eq!(std::fs::read_to_string(other).unwrap(), "sibling fixture");
    assert!(executable_test_support::spawn(&mut Command::new(script))
        .unwrap()
        .wait()
        .unwrap()
        .success());
}

#[cfg(target_os = "linux")]
#[test]
fn executable_fixture_retries_when_its_write_descriptor_is_open() {
    let tmp = tempfile::tempdir().unwrap();
    let script = executable_test_support::write_script(tmp.path(), "busy", "#!/bin/sh\nexit 0\n");
    let writer = std::fs::OpenOptions::new()
        .write(true)
        .open(&script)
        .unwrap();
    let mut command = Command::new(script);
    assert!(pg_worker_core::stall::is_text_busy(
        &command.spawn().unwrap_err()
    ));

    let mut writer = Some(writer);
    let mut child = pg_worker_core::stall::spawn_retrying_with(
        || command.spawn(),
        |pause| pg_worker_core::stall::release_busy_fixture(&mut writer, pause),
    )
    .unwrap();
    assert!(writer.is_none());
    assert!(child.wait().unwrap().success());
}
