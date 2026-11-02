//! Linux probes: cgroup v2 directories for groups, `/proc` for processes and
//! open fds. Both roots are injectable so tests can use tempdirs.

use super::{GroupLiveness, GroupState, LockState, SeatLocks};
use std::os::unix::fs::MetadataExt;
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
    /// State of the cgroup v2 directory of the run's scope.
    ///
    /// `group` must be the absolute cgroup directory stored in the slot table;
    /// anything else (a bare unit name) cannot be judged and answers
    /// [`GroupState::Unknown`]. The directory is probed with `metadata` first:
    /// only a missing directory is [`GroupState::Gone`], while an unreadable
    /// one (EACCES on a parent, EIO, a file where a directory should be, ...)
    /// is [`GroupState::Unknown`] -- reclaim must never free a live run's slot
    /// because a probe failed.
    ///
    /// Known limitation: members in *child* cgroups leave the parent's
    /// `cgroup.procs` empty, which reads [`GroupState::Empty`] while the run is
    /// alive. Today's `systemd-run --scope` groups are flat, so the assumption
    /// holds; revisit when a nested jail appears.
    fn group_state(&self, group: &str) -> GroupState {
        if !group.starts_with('/') {
            return GroupState::Unknown;
        }
        let dir = self.cgroup_root.join(group.trim_start_matches('/'));
        match std::fs::metadata(&dir) {
            Ok(meta) if meta.is_dir() => match std::fs::read_to_string(dir.join("cgroup.procs")) {
                Ok(procs) => {
                    let members = procs.lines().filter(|line| !line.trim().is_empty()).count();
                    if members == 0 {
                        GroupState::Empty
                    } else {
                        GroupState::Live(members as u32)
                    }
                }
                Err(_) => GroupState::Unknown,
            },
            // Exists but is not a directory: not a group we can read.
            Ok(_) => GroupState::Unknown,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => GroupState::Gone,
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
/// repo-root map is caller-supplied and [`ProcSeatLocks::lock_states`] answers
/// a whole reclaim pass from one `/proc/<pid>/fd` walk.
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

    fn lock_state(&self, path: &Path) -> LockState {
        let paths = [path.to_path_buf()];
        scan_locks(&self.proc_root, &paths)[0]
    }

    fn lock_states(&self, paths: &[PathBuf]) -> Vec<LockState> {
        scan_locks(&self.proc_root, paths)
    }
}

/// Identity and probe state of one wanted lock file.
struct Wanted {
    /// `(dev, ino)` of the lock file; `None` when it is already gone (a
    /// vanished lock cannot be held) or cannot be stat'ed at all.
    id: Option<(u64, u64)>,
    /// Owner uid of the lock file, for the "this process may hold it" rule.
    uid: Option<u32>,
    state: LockState,
}

/// Answer a whole reclaim pass with ONE `/proc` walk: the identity `(dev,
/// ino)` of each lock file is matched against every `/proc/<pid>/fd` target,
/// so a symlinked ancestor or a ` (deleted)` suffix never hides a holder (the
/// fd metadata follows the kernel's open file). Fail safe: a probe that
/// errors with anything but `NotFound` answers [`LockState::Unknown`] -- but
/// only for processes that may hold the lock (same uid as the lock file), so
/// an unreadable root-owned pid 1 does not poison every scan. A
/// [`LockState::Held`] match anywhere wins over [`LockState::Unknown`].
fn scan_locks(proc_root: &Path, paths: &[PathBuf]) -> Vec<LockState> {
    let mut wanted: Vec<Wanted> = paths
        .iter()
        .map(|path| match std::fs::metadata(path) {
            Ok(meta) => Wanted {
                id: Some((meta.dev(), meta.ino())),
                uid: Some(meta.uid()),
                state: LockState::Free,
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Wanted {
                id: None,
                uid: None,
                state: LockState::Free,
            },
            // The lock cannot even be stat'ed: unknown (fail safe).
            Err(_) => Wanted {
                id: None,
                uid: None,
                state: LockState::Unknown,
            },
        })
        .collect();
    if wanted.iter().all(|want| want.id.is_none()) {
        // No lock file to match: nothing can hold one.
        return wanted.iter().map(|want| want.state).collect();
    }
    let Ok(procs) = std::fs::read_dir(proc_root) else {
        // Unreadable /proc: unknown for every lock still on disk.
        mark_unknown(&mut wanted, None);
        return wanted.iter().map(|want| want.state).collect();
    };
    for proc in procs {
        let proc = match proc {
            Ok(proc) => proc,
            // The listing itself broke: unknown (fail safe) for all.
            Err(_) => {
                mark_unknown(&mut wanted, None);
                break;
            }
        };
        let name = proc.file_name();
        let numeric = name.to_string_lossy();
        if numeric.is_empty() || !numeric.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let pid_dir = proc.path();
        let pid_uid = match std::fs::metadata(&pid_dir) {
            Ok(meta) => Some(meta.uid()),
            // The process vanished mid-scan: not a holder.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            // Cannot tell who owns it: unknown (fail safe) for all.
            Err(_) => {
                mark_unknown(&mut wanted, None);
                continue;
            }
        };
        let fds = match std::fs::read_dir(pid_dir.join("fd")) {
            Ok(fds) => fds,
            // The process vanished mid-scan: not a holder.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            // Unreadable fd dir: unknown only for locks this process may hold.
            Err(_) => {
                mark_unknown(&mut wanted, pid_uid);
                continue;
            }
        };
        for fd in fds {
            let fd = match fd {
                Ok(fd) => fd,
                Err(_) => {
                    mark_unknown(&mut wanted, pid_uid);
                    continue;
                }
            };
            match std::fs::metadata(fd.path()) {
                // Follows the fd link itself, so even a target the kernel
                // labels ` (deleted)` resolves to the open file's identity.
                Ok(meta) => {
                    for want in &mut wanted {
                        if want.id == Some((meta.dev(), meta.ino())) {
                            want.state = LockState::Held;
                        }
                    }
                }
                // The fd (or its target) vanished: not a holder.
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                // Unreadable fd: unknown only for locks this process may hold.
                Err(_) => mark_unknown(&mut wanted, pid_uid),
            }
        }
    }
    wanted.iter().map(|want| want.state).collect()
}

/// Mark every lock this process may hold [`LockState::Unknown`]: `pid_uid` is
/// the uid of the `/proc/<pid>` directory, `None` when even that is unknown
/// (then every lock counts). A [`LockState::Held`] match already seen wins.
fn mark_unknown(wanted: &mut [Wanted], pid_uid: Option<u32>) {
    for want in wanted {
        if want.state == LockState::Held || want.id.is_none() {
            continue;
        }
        if pid_uid.is_none() || want.uid == pid_uid {
            want.state = LockState::Unknown;
        }
    }
}
