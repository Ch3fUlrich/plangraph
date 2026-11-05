//! Unix side of [`crate::stall`]: the supervised child leads its own process
//! group, and a kill goes to the whole group as SIGTERM first and SIGKILL
//! after the grace period.

use std::io;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus};

/// The supervised child's process group (the pgid is the child's own pid).
pub(super) struct Group {
    pgid: i32,
}

impl Group {
    /// Put `cmd` in its own process group and spawn it.
    pub(super) fn spawn(cmd: &mut Command) -> io::Result<(Group, Child)> {
        cmd.process_group(0);
        let child = super::spawn_retrying(cmd)?;
        Ok((
            Group {
                pgid: child.id() as i32,
            },
            child,
        ))
    }

    /// SIGTERM the whole group; a group that is already gone is ignored.
    pub(super) fn terminate(&self) {
        self.signal_group(libc::SIGTERM);
    }

    /// SIGKILL the whole group; a group that is already gone is ignored.
    pub(super) fn kill(&self) {
        self.signal_group(libc::SIGKILL);
    }

    fn signal_group(&self, sig: i32) {
        let _ = killpg(self.pgid, sig);
    }
}

#[rustfmt::skip]
pub(crate) fn killpg(pgid: i32, signal: i32) -> io::Result<()> {
    match raw_killpg(pgid, signal) { Err(e) if e.raw_os_error() == Some(libc::ESRCH) => Ok(()), result => result }
}

pub(crate) fn group_empty(pgid: i32) -> io::Result<bool> {
    match raw_killpg(pgid, 0) {
        Ok(()) => Ok(false),
        Err(e) if e.raw_os_error() == Some(libc::ESRCH) => Ok(true),
        Err(e) => Err(e),
    }
}

#[rustfmt::skip]
fn raw_killpg(pgid: i32, signal: i32) -> io::Result<()> {
    if pgid <= 1 || pgid == unsafe { libc::getpgrp() } {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "unsafe process group"));
    }
    if unsafe { libc::killpg(pgid, signal) } == 0 { return Ok(()); }
    let error = io::Error::last_os_error();
    Err(error)
}

/// The fatal signal that stopped `status`, if any (`WTERMSIG`).
pub(super) fn exit_signal(status: &ExitStatus) -> Option<i32> {
    status.signal()
}
