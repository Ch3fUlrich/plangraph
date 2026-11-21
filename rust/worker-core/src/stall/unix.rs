//! Unix side of [`crate::stall`]: the supervised child leads its own process
//! group, and a kill goes to the whole group as SIGTERM first and SIGKILL
//! after the grace period.

use std::io;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus};

/// The supervised child's process group (the pgid is the child's own pid).
pub(super) struct Group {
    pgid: i32,
    #[cfg(target_os = "linux")]
    session: Option<crate::run::group_unix::UnixGroup>,
}

impl Group {
    /// Put `cmd` in its own process group and spawn it.
    pub(super) fn spawn(cmd: &mut Command) -> io::Result<(Group, Child)> {
        cmd.process_group(0);
        Self::spawn_group(cmd, false)
    }

    pub(super) fn spawn_session(cmd: &mut Command) -> io::Result<(Group, Child)> {
        #[cfg(target_os = "linux")]
        crate::run::group_unix::own_session(cmd);
        #[cfg(not(target_os = "linux"))]
        cmd.process_group(0);
        Self::spawn_group(cmd, true)
    }

    fn spawn_group(cmd: &mut Command, _isolated: bool) -> io::Result<(Group, Child)> {
        let child = super::spawn_retrying(cmd)?;
        Ok((
            Group {
                pgid: child.id() as i32,
                #[cfg(target_os = "linux")]
                session: _isolated.then(|| {
                    use crate::run::{
                        group::Identity,
                        group_unix::{proc_start_time, Roots, UnixGroup},
                    };
                    let roots = Roots::default();
                    UnixGroup {
                        identity: Identity {
                            pid: child.id(),
                            start_time: proc_start_time(&roots.proc, child.id()),
                            group: None,
                        },
                        roots,
                    }
                }),
            },
            child,
        ))
    }

    /// SIGTERM the whole group; a group that is already gone is ignored.
    pub(super) fn terminate(&self) {
        #[cfg(target_os = "linux")]
        if let Some(session) = &self.session {
            crate::run::group::RunGroup::terminate(session);
            return;
        }
        self.signal_group(libc::SIGTERM);
    }

    /// SIGKILL the whole group; a group that is already gone is ignored.
    pub(super) fn kill(&self) {
        #[cfg(target_os = "linux")]
        if let Some(session) = &self.session {
            crate::run::group::RunGroup::kill(session);
            return;
        }
        self.signal_group(libc::SIGKILL);
    }

    pub(super) fn finish(&self) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        if let Some(session) = &self.session {
            use crate::run::group::{stop_group, StopOutcome};
            use std::time::Duration;
            return match stop_group(session, Duration::from_millis(250), Duration::from_secs(2)) {
                StopOutcome::Empty => Ok(()),
                StopOutcome::Survivors(pids) => Err(io::Error::other(format!(
                    "process group cleanup incomplete: {} survivor(s)",
                    pids.len()
                ))),
            };
        }
        self.terminate();
        self.kill();
        Ok(())
    }

    fn signal_group(&self, sig: i32) {
        let _ = killpg(self.pgid, sig);
    }
}

pub(crate) fn killpg(pgid: i32, signal: i32) -> io::Result<()> {
    if pgid <= 1 || pgid == unsafe { libc::getpgrp() } {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    match unsafe { libc::killpg(pgid, signal) } {
        0 => Ok(()),
        _ => {
            let error = io::Error::last_os_error();
            if signal != 0 && error.raw_os_error() == Some(libc::ESRCH) {
                Ok(())
            } else {
                Err(error)
            }
        }
    }
}

/// The fatal signal that stopped `status`, if any (`WTERMSIG`).
pub(super) fn exit_signal(status: &ExitStatus) -> Option<i32> {
    status.signal()
}
