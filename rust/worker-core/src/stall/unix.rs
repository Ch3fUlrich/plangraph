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
        // SAFETY: plain syscall; errors (e.g. ESRCH when the group is gone) are ignored.
        unsafe {
            libc::killpg(self.pgid, sig);
        }
    }
}

/// The fatal signal that stopped `status`, if any (`WTERMSIG`).
pub(super) fn exit_signal(status: &ExitStatus) -> Option<i32> {
    status.signal()
}
