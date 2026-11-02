//! Tests for the `slots.wake` content-token wake file (R3b-1). Hermetic:
//! tempdirs only; the longest sleep is 20 ms and one 100 ms timeout.

use super::wake::{touch_wake, wait_for_wake, wake_token, WakeResult};
use std::sync::{Arc, Barrier};
use std::time::{Duration, Instant};

#[test]
fn touch_wake_creates_the_file_with_a_hex_token() {
    let dir = tempfile::tempdir().unwrap();
    assert_eq!(wake_token(dir.path()), None, "no file, no token");
    touch_wake(dir.path()).unwrap();
    let token = wake_token(dir.path()).unwrap();
    assert_eq!(token.len(), 16, "one u64 in hex");
    assert!(
        token.chars().all(|c| c.is_ascii_hexdigit()),
        "hex digits only, got {token:?}"
    );
    assert!(dir.path().join("slots.wake").exists());
    let leftovers: Vec<String> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with("slots.wake.tmp"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "no per-touch temp survives the rename: {leftovers:?}"
    );
}

#[test]
fn a_second_touch_changes_the_token() {
    let dir = tempfile::tempdir().unwrap();
    touch_wake(dir.path()).unwrap();
    let first = wake_token(dir.path()).unwrap();
    touch_wake(dir.path()).unwrap();
    let second = wake_token(dir.path()).unwrap();
    assert_ne!(first, second, "each touch writes a fresh token");
}

#[test]
fn two_threads_touching_at_once_both_succeed() {
    let dir = tempfile::tempdir().unwrap();
    let barrier = Arc::new(Barrier::new(2));
    // Many rounds of two concurrent touches: a shared temp name races on
    // every overlap, a per-touch temp never does.
    for _ in 0..25 {
        let handles: Vec<_> = (0..2)
            .map(|_| {
                let barrier = Arc::clone(&barrier);
                let path = dir.path().to_path_buf();
                std::thread::spawn(move || {
                    barrier.wait();
                    touch_wake(&path).unwrap();
                    wake_token(&path).unwrap()
                })
            })
            .collect();
        let tokens: Vec<String> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        let final_token = wake_token(dir.path()).unwrap();
        assert!(
            tokens.contains(&final_token),
            "the final token {final_token:?} is one of the two {tokens:?}"
        );
    }
    let leftovers: Vec<String> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with("slots.wake.tmp"))
        .collect();
    assert!(leftovers.is_empty(), "no temp survives: {leftovers:?}");
}

#[test]
fn wait_for_wake_returns_changed_after_a_touch_from_another_thread() {
    let dir = tempfile::tempdir().unwrap();
    touch_wake(dir.path()).unwrap();
    let first = wake_token(dir.path()).unwrap();
    let path = dir.path().to_path_buf();
    let toucher = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(20));
        touch_wake(&path).unwrap();
    });
    let got = wait_for_wake(dir.path(), Some(&first), Duration::from_secs(2));
    match got {
        WakeResult::Changed(token) => assert_ne!(token, first, "the new token is the news"),
        other => panic!("expected Changed, got {other:?}"),
    }
    toucher.join().unwrap();
}

#[test]
fn wait_for_wake_without_a_last_seen_reports_the_token_as_news() {
    let dir = tempfile::tempdir().unwrap();
    touch_wake(dir.path()).unwrap();
    let token = wake_token(dir.path()).unwrap();
    // `last = None` means any token is news, even one written before the
    // wait started.
    let got = wait_for_wake(dir.path(), None, Duration::from_millis(100));
    assert_eq!(got, WakeResult::Changed(token));
}

#[test]
fn wait_for_wake_times_out_without_a_touch() {
    let dir = tempfile::tempdir().unwrap();
    touch_wake(dir.path()).unwrap();
    let token = wake_token(dir.path()).unwrap();
    // The token stands still: only the timeout ends the wait.
    let start = Instant::now();
    let got = wait_for_wake(dir.path(), Some(&token), Duration::from_millis(100));
    assert_eq!(got, WakeResult::Timeout);
    assert!(
        start.elapsed() >= Duration::from_millis(100),
        "waits it out"
    );
    // With no file at all there is never any news either.
    let empty = tempfile::tempdir().unwrap();
    let got = wait_for_wake(empty.path(), None, Duration::from_millis(100));
    assert_eq!(got, WakeResult::Timeout);
}

/// Windows only: `touch_wake` must replace `slots.wake` while a waiter keeps
/// opening and reading it — share mode `READ|WRITE|DELETE` on the reader plus
/// the sharing-violation rename retry on the writer.
#[cfg(windows)]
#[test]
fn touch_wake_replaces_the_file_while_a_waiter_reads() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let dir = tempfile::tempdir().unwrap();
    touch_wake(dir.path()).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let waiter = {
        let stop = Arc::clone(&stop);
        let path = dir.path().to_path_buf();
        std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                let _ = wake_token(&path);
            }
        })
    };
    for _ in 0..50 {
        touch_wake(dir.path()).unwrap();
    }
    stop.store(true, Ordering::Relaxed);
    waiter.join().unwrap();
}
