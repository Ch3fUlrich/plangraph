//! The wake file: `<state>/slots.wake`, a content-based "the table changed"
//! signal (plan rev 6.4, R3b-1).
//!
//! The exiting holder calls [`touch_wake`] AFTER [`crate::slots::with_table`]
//! has returned — never while `slots.lock` is held. Every waiting supervisor
//! polls [`wait_for_wake`] with its last seen token (callers use a 1 s
//! timeout) and re-reads the table when the token moves. The token is a fresh
//! random u64 in hex, written by a per-touch temp (`slots.wake.tmp.<token>`)
//! plus a rename in the same directory: waiters never see a half-written
//! token, and because the token is the content, a coarse mtime granularity
//! can never hide a wake. Portable: no inotify, no `std::os::unix`, and
//! `std::os::windows` only for the Windows share mode below; no dependency
//! beyond `rand` (already there).

use rand::Rng;
use std::path::Path;
use std::time::{Duration, Instant};

/// How often [`wait_for_wake`] looks at the token.
const POLL: Duration = Duration::from_millis(50);

/// The wake file inside the state directory. Every touch writes its own
/// `slots.wake.tmp.<token>` sibling and removes it again on failure, so two
/// concurrent touches never clobber one shared temp buffer.
const WAKE: &str = "slots.wake";
const WAKE_TMP: &str = "slots.wake.tmp.";

/// What [`wait_for_wake`] saw.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WakeResult {
    /// The token changed; carries the new token.
    Changed(String),
    /// The timeout passed without a change.
    Timeout,
}

/// Write a fresh random token into `<state>/slots.wake`: a per-touch
/// `slots.wake.tmp.<token>` write plus a rename in the same directory. The
/// temp is removed when the write or the rename fails. On Linux the rename
/// replaces atomically. On Windows `MoveFileEx(REPLACE_EXISTING)` can fail
/// with a sharing violation while a waiter has `slots.wake` open (std's
/// reader sets no `FILE_SHARE_DELETE`), so readers there open the file with
/// share mode `READ|WRITE|DELETE` and the writer retries that one rename
/// error up to 10 times with a 5-20 ms jittered sleep.
pub fn touch_wake(state_dir: &Path) -> std::io::Result<()> {
    let mut rng = rand::rng();
    let token = format!("{:016x}", rng.random::<u64>());
    let tmp = state_dir.join(format!("{WAKE_TMP}{token}"));
    std::fs::write(&tmp, token.as_bytes()).map_err(|err| unlink(&tmp, err))?;
    rename_replace(&tmp, &state_dir.join(WAKE)).map_err(|err| unlink(&tmp, err))?;
    Ok(())
}

/// Remove a half-written temp and keep the original error (best effort).
fn unlink(tmp: &Path, err: std::io::Error) -> std::io::Error {
    let _ = std::fs::remove_file(tmp);
    err
}

/// Replace `to` with `from`. On Windows the rename is
/// `MoveFileEx(REPLACE_EXISTING)`, which fails with a sharing violation while
/// a waiter has the file open; retry that one error ([`is_sharing_violation`])
/// up to 10 times with a 5-20 ms jittered sleep. On POSIX the plain rename
/// replaces atomically as it is.
fn rename_replace(from: &Path, to: &Path) -> std::io::Result<()> {
    #[cfg(not(windows))]
    {
        std::fs::rename(from, to)
    }
    #[cfg(windows)]
    {
        let mut retries = 0u32;
        loop {
            match std::fs::rename(from, to) {
                Ok(()) => return Ok(()),
                Err(err) if retries < 10 && is_sharing_violation(&err) => {
                    retries += 1;
                    let mut rng = rand::rng();
                    std::thread::sleep(Duration::from_millis(rng.random_range(5..=20)));
                }
                Err(err) => return Err(err),
            }
        }
    }
}

/// The one rename error worth retrying (Windows): `ERROR_SHARING_VIOLATION`,
/// raised while a waiter holds `slots.wake` open.
#[cfg(windows)]
fn is_sharing_violation(err: &std::io::Error) -> bool {
    const ERROR_SHARING_VIOLATION: i32 = 32;
    err.raw_os_error() == Some(ERROR_SHARING_VIOLATION)
}

/// The current wake token, or [`None`] when the file is missing or unreadable.
pub fn wake_token(state_dir: &Path) -> Option<String> {
    let text = read_wake(&state_dir.join(WAKE)).ok()?;
    Some(text.trim().to_string())
}

/// Read the wake file. On Windows a waiter holds it open with share mode
/// `READ|WRITE|DELETE` ([`std::os::windows::fs::OpenOptionsExt`]), so a
/// concurrent [`touch_wake`] rename can replace the file while the read is
/// still in flight.
#[cfg(windows)]
fn read_wake(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    use std::os::windows::fs::OpenOptionsExt;
    /// FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    const SHARE_RW_DELETE: u32 = 0x1 | 0x2 | 0x4;
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(SHARE_RW_DELETE)
        .open(path)?;
    let mut text = String::new();
    file.read_to_string(&mut text)?;
    Ok(text)
}

#[cfg(not(windows))]
fn read_wake(path: &Path) -> std::io::Result<String> {
    std::fs::read_to_string(path)
}

/// Wait until the wake token differs from `last`, or `timeout` passes
/// ([`WakeResult::Timeout`]); callers use 1 s and loop. Polls every 50 ms —
/// no inotify crate, portable everywhere. `last = None` means "any token is
/// news"; a missing file is never a change.
pub fn wait_for_wake(state_dir: &Path, last: Option<&str>, timeout: Duration) -> WakeResult {
    let start = Instant::now();
    loop {
        if let Some(token) = wake_token(state_dir) {
            if last != Some(token.as_str()) {
                return WakeResult::Changed(token);
            }
        }
        let Some(remaining) = timeout.checked_sub(start.elapsed()) else {
            return WakeResult::Timeout;
        };
        if remaining.is_zero() {
            return WakeResult::Timeout;
        }
        std::thread::sleep(POLL.min(remaining));
    }
}
