//! Execution-group liveness and seat lock probes for stale reclaim (R3b).
//!
//! Slot lifetime follows the execution GROUP: the systemd scope on Linux, the
//! Job Object on Windows (plan rev 6.4). [`GroupLiveness`] answers whether a
//! group still has members and whether a `(pid, start_time)` process is alive;
//! `pid` and `start_time` only identify the group -- a dead supervisor, or a
//! reused pid with another start time, does NOT free a slot while the group
//! still has members. [`SeatLocks`] finds the leftover
//! `.git/worktrees/seat-NN/index.lock` of a freed seat and answers [`LockState`]
//! -- `Held`, `Free` or `Unknown` -- never a bare "not held" when the probe
//! simply cannot see the holders.
//!
//! `start_time` is an opaque per-host process identity (Linux: `starttime`,
//! field 22 of `/proc/<pid>/stat`; Windows: the process creation FILETIME),
//! compared only for equality. The platform probes are cfg-gated ([`linux`] on
//! Linux, [`windows`] on Windows) with injectable roots so tests can point
//! them at tempdirs; everything else in `slots` stays portable (D-3705).

use std::path::{Path, PathBuf};

#[cfg(target_os = "linux")]
pub(crate) mod linux;
#[cfg(all(test, target_os = "linux"))]
mod tests;
#[cfg(windows)]
mod windows;

#[cfg(target_os = "linux")]
pub use linux::{LinuxLiveness, ProcSeatLocks};
#[cfg(windows)]
pub use windows::{FileSeatLocks, JobLiveness};

/// What the execution group behind a slot looks like right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GroupState {
    /// The group exists and has this many member processes.
    Live(u32),
    /// The group exists but has no members left.
    Empty,
    /// The group is gone.
    Gone,
    /// The state could not be determined; reclaim must fail safe and keep.
    Unknown,
}

/// Liveness of execution groups and of the processes that identify them.
pub trait GroupLiveness {
    /// State of the run's confinement group: cgroup scope directory (Linux) or
    /// Job Object (Windows). The `group` string is exactly what the slot table
    /// stores.
    fn group_state(&self, group: &str) -> GroupState;

    /// Is exactly this process (pid plus start time) still alive? A reused pid
    /// with another start time answers `false`.
    fn process_alive(&self, pid: u32, start_time: u64) -> bool;
}

/// What a seat `index.lock` file looks like right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockState {
    /// A live process holds the lock file.
    Held,
    /// No live process holds the lock file.
    Free,
    /// The state could not be determined; reclaim must fail safe and keep
    /// the file (never delete a lock another process may hold).
    Unknown,
}

/// The `.git/worktrees/seat-NN/index.lock` files of seat slots.
pub trait SeatLocks {
    /// Lock file of a seat slot, or `None` when the repo is unknown to the
    /// caller-supplied map (R4/R5 own seat worktrees).
    fn index_lock(&self, repo_id: &str, seat_slot: u32) -> Option<PathBuf>;

    /// [`LockState`] of one lock file.
    fn lock_state(&self, path: &Path) -> LockState;

    /// [`LockState`] of many lock files, in the order of `paths`. The default
    /// maps [`Self::lock_state`]; a probe that can answer a whole reclaim pass
    /// from one system walk overrides it.
    fn lock_states(&self, paths: &[PathBuf]) -> Vec<LockState> {
        paths.iter().map(|path| self.lock_state(path)).collect()
    }
}

/// `<repo_root>/.git/worktrees/seat-NN/index.lock` for a seat slot:
/// `seat-NN` is the two-digit zero-padded seat number.
#[cfg(any(target_os = "linux", windows))]
fn seat_index_lock(repo_root: &Path, seat_slot: u32) -> PathBuf {
    repo_root
        .join(".git")
        .join("worktrees")
        .join(format!("seat-{seat_slot:02}"))
        .join("index.lock")
}
