//! One signal to one process of an owned group, sent only to the process that was seen.

use pg_worker_core::run::group_unix::parse_stat;
use std::{fs, path::Path};

/// A process of the owned group as it was seen: the identity a signal must still match.
#[derive(Clone, Debug)]
pub struct Member {
    pub pid: u32,
    pub start_time: u64,
    pub group: u32,
}

fn identity_matches(proc: &Path, member: &Member) -> bool {
    fs::read_to_string(proc.join(member.pid.to_string()).join("stat"))
        .ok()
        .and_then(|text| parse_stat(&text))
        .is_some_and(|(state, group, start)| {
            !matches!(state, 'Z' | 'X') && group == member.group && start == member.start_time
        })
}

/// Pin the process with a pidfd, check it is still the member that was seen, then signal
/// through the pidfd: a pid reused or a process that left the group is never signalled.
pub fn signal_member(member: &Member, signal: i32) -> bool {
    if member.pid <= 1 || member.pid > i32::MAX as u32 {
        return false;
    }
    // SAFETY: pidfd_open takes a pid and flags and returns a descriptor or -1.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, member.pid as libc::c_int, 0) };
    if fd < 0 {
        return false;
    }
    let fd = fd as libc::c_int;
    let sent = identity_matches(Path::new("/proc"), member)
        // SAFETY: pidfd_send_signal takes the descriptor, a signal and a null siginfo.
        && unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                fd,
                signal,
                std::ptr::null::<libc::c_void>(),
                0,
            )
        } == 0;
    // SAFETY: fd is the descriptor opened above.
    unsafe { libc::close(fd) };
    sent
}
