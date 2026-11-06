//! Exclusive lock on a one-byte lock file (plan rev 6.4, R3a).
//!
//! The lock protects longer-running critical sections such as the slot table
//! rewrite. It is advisory and always non-blocking: `try_lock` makes exactly
//! one attempt and reports `None` when somebody else holds the lock, while
//! `lock_with_retry` repeats non-blocking attempts with a jittered 5-50 ms
//! sleep between them until the deadline. A blocking lock call is never made,
//! so no worker can hang on a stale holder.
//!
//! Creating the lock file is idempotent; the file itself is never deleted,
//! only locked and unlocked. The guard unlocks on drop.

use std::fs::{File, OpenOptions};
use std::path::Path;
use std::time::{Duration, Instant};

#[cfg(test)]
mod tests;

/// Lock plumbing: OS errors and the retry deadline passing.
#[derive(Debug, thiserror::Error)]
pub enum LockError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("lock not acquired before the deadline")]
    Timeout,
}

/// Holds the exclusive lock; the file unlocks when the guard drops.
#[derive(Debug)]
pub struct LockGuard {
    file: File,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        unlock(&self.file);
    }
}

/// One non-blocking attempt to take the exclusive lock on `path`.
///
/// `Ok(Some(_))` = acquired, `Ok(None)` = held by somebody else right now.
/// The lock file is created on first use; creating it again is a no-op.
pub fn try_lock(path: &Path) -> Result<Option<LockGuard>, LockError> {
    // Never truncate: the file is only ever a lock anchor.
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)?;
    if lock_exclusive_nb(&file)? {
        Ok(Some(LockGuard { file }))
    } else {
        Ok(None)
    }
}

/// Keep trying `try_lock` until the deadline, sleeping 5-50 ms (jittered via
/// `rng`) between attempts. Never blocks in a lock call: each attempt is
/// non-blocking, and `LockError::Timeout` means the deadline passed while the
/// lock stayed held.
pub fn lock_with_retry(
    path: &Path,
    deadline: Duration,
    rng: &mut impl rand::Rng,
) -> Result<LockGuard, LockError> {
    let end = Instant::now() + deadline;
    loop {
        if let Some(guard) = try_lock(path)? {
            return Ok(guard);
        }
        let now = Instant::now();
        if now >= end {
            return Err(LockError::Timeout);
        }
        let jitter = Duration::from_millis(rng.random_range(5u64..=50u64));
        std::thread::sleep(jitter.min(end - now));
    }
}

/// Exclusive, non-blocking lock. `Ok(false)` = held by somebody else.
#[cfg(unix)]
fn lock_exclusive_nb(file: &File) -> Result<bool, std::io::Error> {
    use std::os::unix::io::AsRawFd;
    // SAFETY: flock on a valid fd; the file outlives the lock.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc == 0 {
        return Ok(true);
    }
    let err = std::io::Error::last_os_error();
    if err.kind() == std::io::ErrorKind::WouldBlock {
        Ok(false)
    } else {
        Err(err)
    }
}

#[cfg(unix)]
fn unlock(file: &File) {
    use std::os::unix::io::AsRawFd;
    // SAFETY: flock on a valid fd; errors are best-effort on drop.
    unsafe {
        libc::flock(file.as_raw_fd(), libc::LOCK_UN);
    }
}

/// Exclusive, non-blocking lock via `LockFileEx`. `Ok(false)` = held by
/// somebody else (ERROR_LOCK_VIOLATION with FAIL_IMMEDIATELY).
#[cfg(windows)]
fn lock_exclusive_nb(file: &File) -> Result<bool, std::io::Error> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::ERROR_LOCK_VIOLATION;
    use windows_sys::Win32::Storage::FileSystem::{
        LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
    };
    use windows_sys::Win32::System::IO::OVERLAPPED;

    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    // SAFETY: valid handle, stack-overlapped outlives the call, one byte.
    let ok = unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    };
    if ok != 0 {
        return Ok(true);
    }
    let err = std::io::Error::last_os_error();
    if err.raw_os_error() == Some(ERROR_LOCK_VIOLATION as i32) {
        Ok(false)
    } else {
        Err(err)
    }
}

#[cfg(windows)]
fn unlock(file: &File) {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::UnlockFileEx;
    use windows_sys::Win32::System::IO::OVERLAPPED;

    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    // SAFETY: valid handle, stack-overlapped outlives the call, one byte.
    unsafe {
        UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped);
    }
}
