//! The wake file: `<state>/slots.wake`, a content-based "the table changed"
//! signal (plan rev 6.4, R3b-1).
//!
//! The exiting holder calls [`touch_wake`] AFTER [`crate::slots::with_table`]
//! has returned — never while `slots.lock` is held. Every waiting supervisor
//! polls [`wait_for_wake`] with its last seen token (callers use a 1 s
//! timeout) and re-reads the table when the token moves. The token is a fresh
//! random u64 in hex, written by temp write + rename in the same directory:
//! waiters never see a half-written token, and because the token is the
//! content, a coarse mtime granularity can never hide a wake. Portable: no
//! inotify, no `std::os::unix`, no dependency beyond `rand` (already there).

use rand::Rng;
use std::path::Path;
use std::time::{Duration, Instant};

/// How often [`wait_for_wake`] looks at the token.
const POLL: Duration = Duration::from_millis(50);

/// The wake file inside the state directory, and its temp sibling.
const WAKE: &str = "slots.wake";
const WAKE_TMP: &str = "slots.wake.tmp";

/// What [`wait_for_wake`] saw.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WakeResult {
    /// The token changed; carries the new token.
    Changed(String),
    /// The timeout passed without a change.
    Timeout,
}

/// Write a fresh random token into `<state>/slots.wake`: a temp write plus a
/// rename in the same directory (`std::fs::rename` replaces atomically on
/// Linux and Windows alike).
pub fn touch_wake(state_dir: &Path) -> std::io::Result<()> {
    let mut rng = rand::rng();
    let token = format!("{:016x}", rng.random::<u64>());
    let tmp = state_dir.join(WAKE_TMP);
    std::fs::write(&tmp, token.as_bytes())?;
    std::fs::rename(&tmp, state_dir.join(WAKE))?;
    Ok(())
}

/// The current wake token, or [`None`] when the file is missing or unreadable.
pub fn wake_token(state_dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(state_dir.join(WAKE)).ok()?;
    Some(text.trim().to_string())
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
