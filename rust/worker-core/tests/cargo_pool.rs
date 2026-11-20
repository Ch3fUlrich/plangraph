#![cfg(unix)]
use pg_worker_core::{
    cargo_pool::{
        acquire_slot, configured_slot_count, count_cargo_lock_waits, enforce_pool,
        enforce_pool_cap, existing_pool_slots, pool_slots, reap_stale_leases, record_lock_wait,
        slot_count, summarize_lock_waits, LockWaitSample, LockWaitTracker,
    },
    disk_hygiene::{scan, Report},
};
use std::{
    fs,
    process::Command,
    sync::{Arc, Barrier},
    time::Instant,
};

#[test]
fn slot_count_scales_with_free_disk_and_defaults_to_three() {
    let gib = 1024 * 1024 * 1024;
    assert_eq!(slot_count(100 * gib, 0, 3), 3);
    assert_eq!(slot_count(100 * gib, 15 * gib, 3), 3);
    assert_eq!(slot_count(30 * gib, 15 * gib, 3), 2);
    assert_eq!(slot_count(10 * gib, 15 * gib, 3), 1);
}

#[test]
fn three_parallel_cargo_builds_show_no_file_lock_wait_and_record_telemetry() {
    let tmp = tempfile::tempdir().unwrap();
    // One fixture per build: parallel `cargo` runs on one manifest also wait on its
    // Cargo.lock, which is not the target-directory lock the pool removes.
    let fixtures: Vec<_> = (0..3)
        .map(|i| {
            let dir = tmp.path().join(format!("fixture-{i}"));
            fs::create_dir_all(dir.join("src")).unwrap();
            fs::write(
                dir.join("Cargo.toml"),
                "[package]\nname = \"pool-fixture\"\nversion = \"0.1.0\"\nedition = \"2021\"\n[workspace]\n",
            )
            .unwrap();
            fs::write(dir.join("src/lib.rs"), "pub fn answer() -> u32 { 42 }\n").unwrap();
            dir
        })
        .collect();

    let primary = tmp.path().join("cache/cargo-target");
    let state = tmp.path().join("state");
    fs::create_dir_all(&state).unwrap();
    let slots = pool_slots(&primary, 3).unwrap();
    assert_eq!(slots.len(), 3);

    let barrier = Arc::new(Barrier::new(3));
    let mut handles = Vec::new();
    for (i, crate_dir) in fixtures.iter().enumerate() {
        let barrier = Arc::clone(&barrier);
        let crate_dir = crate_dir.clone();
        let primary = primary.clone();
        let state = state.clone();
        handles.push(std::thread::spawn(move || {
            let run_id = format!("run-parallel-{i}");
            let lease = acquire_slot(&primary, 3, &run_id, Some(&state)).unwrap();
            barrier.wait();
            let started = Instant::now();
            let output = Command::new("cargo")
                .args(["check", "--offline", "--manifest-path"])
                .arg(crate_dir.join("Cargo.toml"))
                .env("CARGO_TARGET_DIR", &lease.path)
                .env("CARGO_HOME", state.join(format!("cargo-home-{i}")))
                .env("CARGO_INCREMENTAL", "0")
                .output()
                .unwrap();
            let elapsed_ms = started.elapsed().as_millis() as u64;
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(output.status.success(), "cargo failed: {stderr}");
            assert!(
                !stderr.contains("Blocking waiting for file lock"),
                "unexpected lock wait in pool build: {stderr}"
            );
            let waits = count_cargo_lock_waits(&stderr);
            let mut tracker = LockWaitTracker::default();
            for line in stderr.lines() {
                tracker.observe(line, 0);
            }
            let (events, lock_wait_ms) = tracker.finish(elapsed_ms);
            assert_eq!((events, lock_wait_ms), (0, 0));
            record_lock_wait(
                &state,
                &LockWaitSample {
                    host: "test-host".into(),
                    run_id,
                    slot: lease.slot_index,
                    build_ms: elapsed_ms.max(1),
                    lock_wait_ms,
                    blocking_events: waits,
                    ts: 1,
                },
            )
            .unwrap();
            (lease.slot_index, lease.path.clone(), waits)
        }));
    }

    let mut seen_slots = Vec::new();
    for handle in handles {
        let (idx, path, waits) = handle.join().unwrap();
        assert_eq!(waits, 0, "parallel pool slot had cargo lock wait");
        assert!(path.exists());
        seen_slots.push(idx);
    }
    seen_slots.sort();
    assert_eq!(seen_slots, vec![0, 1, 2]);

    let summary = summarize_lock_waits(&state).unwrap();
    assert_eq!(summary["test-host"].builds, 3);
    assert_eq!(summary["test-host"].blocking_events, 0);
}

#[test]
fn pool_reaps_dead_and_terminal_leases_and_bounds_total_size_under_n_times_cap() {
    let tmp = tempfile::tempdir().unwrap();
    let primary = tmp.path().join("cache/cargo-target");
    let state = tmp.path().join("state");
    fs::create_dir_all(state.join("runs/run-done")).unwrap();
    fs::write(
        state.join("runs/run-done/record.json"),
        r#"{"status":"completed"}"#,
    )
    .unwrap();

    let slots = pool_slots(&primary, 3).unwrap();
    for (idx, slot) in slots.iter().enumerate() {
        fs::create_dir_all(slot.join("debug")).unwrap();
        fs::write(slot.join("debug/blob"), vec![0u8; 4096]).unwrap();
        if idx == 0 {
            // Stale lease from a completed run with no lock held.
            let lease_file = slot.with_file_name(format!(
                "{}.lease.json",
                slot.file_name().unwrap().to_str().unwrap()
            ));
            fs::write(
                lease_file,
                r#"{"run_id":"run-done","pid":99999999,"acquired_at":1}"#,
            )
            .unwrap();
        }
    }

    // Hold slot 1 live so cleanup cannot touch it.
    let live = acquire_slot(&primary, 3, "run-live", Some(&state)).unwrap();
    // Slot 0 was reaped because no lock was held and run-done is terminal; so live took slot 0!
    assert_eq!(live.slot_index, 0);

    let reaped = reap_stale_leases(&slots, Some(&state)).unwrap();
    assert_eq!(reaped, 0);

    // Cap each slot to 2048 bytes (total N * cap = 3 * 2048 = 6144 bytes).
    // Slot 0 is live (4096 bytes), slots 1 & 2 are idle (4096 bytes each -> total 12288 > 6144).
    let mut report = Report {
        free_bytes: 50_000_000_000,
        alarm: false,
        dry_run: false,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
    };
    let total_after = enforce_pool_cap(&slots, 2048, true, &mut report).unwrap();
    assert!(
        slots[0].join("debug/blob").exists(),
        "live slot must survive"
    );
    assert!(
        total_after <= 3 * 2048,
        "total {total_after} exceeded N*cap {}",
        3 * 2048
    );
    assert!(scan(&slots[1]).map_or(0, |x| x.0) <= 2048);
    assert!(scan(&slots[2]).map_or(0, |x| x.0) <= 2048);
}

#[test]
fn lock_wait_tracker_measures_real_wait_from_blocking_line_to_next_line() {
    let mut tracker = LockWaitTracker::default();
    tracker.observe("   Compiling a v0.1.0", 10);
    tracker.observe("    Blocking waiting for file lock on build directory", 100);
    tracker.observe(
        "    Blocking waiting for file lock on artifact directory",
        150,
    );
    tracker.observe("   Compiling b v0.1.0", 400);
    tracker.observe("    Blocking waiting for file lock on build directory", 500);
    // A wait still open at the end of the build counts up to its end.
    assert_eq!(tracker.finish(650), (3, 300 + 150));
}

#[test]
fn lock_wait_tracker_reports_zero_without_blocking_lines() {
    let mut tracker = LockWaitTracker::default();
    tracker.observe("   Compiling a v0.1.0", 10);
    tracker.observe("    Finished dev profile", 900);
    assert_eq!(tracker.finish(1000), (0, 0));
}

#[test]
fn pool_discovery_counts_slots_and_enforce_pool_trims_idle_slots_without_slot_locks() {
    let tmp = tempfile::tempdir().unwrap();
    let primary = tmp.path().join("cache/cargo-target");
    for slot in pool_slots(&primary, 3).unwrap() {
        fs::create_dir_all(slot.join("debug")).unwrap();
        fs::write(slot.join("debug/blob"), vec![0u8; 4096]).unwrap();
    }
    // Lease, lock and stray files beside the slots are not slots.
    let leased = acquire_slot(&primary, 3, "run-live", None).unwrap();
    let found = existing_pool_slots(&primary);
    assert_eq!(found, pool_slots(&primary, 3).unwrap());
    assert!(configured_slot_count(&primary) >= 1);

    let mut report = Report::default();
    let total = enforce_pool(&primary, 2048, true, &mut report).unwrap();
    assert!(leased.path.join("debug/blob").exists(), "leased slot kept");
    assert!(total <= 4096 + 2 * 2048, "total {total}");
    assert!(scan(&found[1]).map_or(0, |x| x.0) <= 2048);
    assert!(scan(&found[2]).map_or(0, |x| x.0) <= 2048);
}
