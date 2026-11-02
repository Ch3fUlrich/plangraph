//! Tests for the `slots.wake` content-token wake file (R3b-1). Hermetic:
//! tempdirs only; the longest sleep is 20 ms and one 100 ms timeout.

use super::wake::{touch_wake, wait_for_wake, wake_token, WakeResult};
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
    assert!(
        !dir.path().join("slots.wake.tmp").exists(),
        "the temp file never survives the rename"
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
