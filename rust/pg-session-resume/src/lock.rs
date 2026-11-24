//! One tick at a time: a kernel-held lock (flock) on a stable file. The kernel drops
//! it when the owner dies, so there is no stale-lock cleanup and no window in which a
//! second tick could see a half-written lock file.

use std::fs::{File, OpenOptions, TryLockError};
use std::io::Write;
use std::path::Path;

pub struct Lock {
    _file: File, // held for its lifetime: closing it releases the kernel lock
}

impl Lock {
    /// None when another live process holds the lock.
    pub fn acquire(path: &Path) -> Option<Lock> {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)
            .ok()?;
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) | Err(TryLockError::Error(_)) => return None,
        }
        // For humans only; the lock is the kernel's, not this text.
        let _ = file
            .set_len(0)
            .and_then(|_| write!(file, "{}", std::process::id()));
        Some(Lock { _file: file })
    }
}
