//! Linux probes: cgroup v2 directories for groups, `/proc` for processes and
//! open fds. Both roots are injectable so tests can use tempdirs.

use super::{GroupLiveness, GroupState, SeatLocks};
use std::path::{Path, PathBuf};

/// Group liveness from the cgroup v2 directory of the run's scope and process
/// identity from `/proc/<pid>/stat` (`starttime`, field 22).
pub struct LinuxLiveness {
    proc_root: PathBuf,
    cgroup_root: PathBuf,
}

impl LinuxLiveness {
    /// Probe the host: `/proc` and cgroup paths as stored in the table.
    pub fn host() -> Self {
        Self::new("/proc", "/")
    }

    /// Injectable roots for tests: `group` paths resolve under `cgroup_root`
    /// (a root of `/` reproduces the absolute cgroup directory of production).
    pub fn new(proc_root: impl Into<PathBuf>, cgroup_root: impl Into<PathBuf>) -> Self {
        LinuxLiveness {
            proc_root: proc_root.into(),
            cgroup_root: cgroup_root.into(),
        }
    }
}

impl GroupLiveness for LinuxLiveness {
    fn group_state(&self, group: &str) -> GroupState {
        let dir = self.cgroup_root.join(group.trim_start_matches('/'));
        if !dir.is_dir() {
            return GroupState::Gone;
        }
        match std::fs::read_to_string(dir.join("cgroup.procs")) {
            Ok(procs) => {
                let members = procs.lines().filter(|line| !line.trim().is_empty()).count();
                if members == 0 {
                    GroupState::Empty
                } else {
                    GroupState::Live(members as u32)
                }
            }
            Err(_) => GroupState::Unknown,
        }
    }

    fn process_alive(&self, pid: u32, start_time: u64) -> bool {
        read_start_time(&self.proc_root, pid) == Some(start_time)
    }
}

/// `starttime` (field 22 of `/proc/<pid>/stat`), parsed after the last `)` so
/// a comm containing spaces or parentheses is safe.
pub(super) fn read_start_time(proc_root: &Path, pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(proc_root.join(pid.to_string()).join("stat")).ok()?;
    let rest = stat.rsplit_once(')')?.1;
    // After the comm: state (field 3) ... starttime (field 22) is the 20th
    // whitespace-separated token of the remainder.
    rest.split_whitespace().nth(19)?.parse().ok()
}

/// The `.git/worktrees/seat-NN/index.lock` files of freed seats: the
/// repo-root map is caller-supplied and `held_by_live_process` scans
/// `/proc/<pid>/fd` links for the path.
pub struct ProcSeatLocks<F> {
    proc_root: PathBuf,
    map: F,
}

impl<F> ProcSeatLocks<F> {
    /// Injectable `/proc` root and the caller-supplied `repo_id` -> repo root
    /// map (R4/R5 own seat worktrees).
    pub fn new(proc_root: impl Into<PathBuf>, map: F) -> Self {
        ProcSeatLocks {
            proc_root: proc_root.into(),
            map,
        }
    }
}

impl<F: Fn(&str) -> Option<PathBuf>> SeatLocks for ProcSeatLocks<F> {
    fn index_lock(&self, repo_id: &str, seat_slot: u32) -> Option<PathBuf> {
        let root = (self.map)(repo_id)?;
        Some(super::seat_index_lock(&root, seat_slot))
    }

    fn held_by_live_process(&self, path: &Path) -> bool {
        let Ok(procs) = std::fs::read_dir(&self.proc_root) else {
            return false;
        };
        for proc in procs.flatten() {
            let name = proc.file_name();
            let numeric = name.to_string_lossy();
            if numeric.is_empty() || !numeric.bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            let Ok(fds) = std::fs::read_dir(proc.path().join("fd")) else {
                continue;
            };
            for fd in fds.flatten() {
                if std::fs::read_link(fd.path()).is_ok_and(|target| target == path) {
                    return true;
                }
            }
        }
        false
    }
}
