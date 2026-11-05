//! Owned fresh process group. Never accepts a caller-provided pid.
use super::Child;
use crate::stall::{group_empty, killpg, spawn_retrying};
use std::{
    io,
    os::unix::process::CommandExt,
    process::{Command, ExitStatus},
};

pub(crate) struct UnixChild {
    child: std::process::Child,
    pgid: i32,
    gone: bool,
}
impl UnixChild {
    pub(crate) fn spawn(cmd: &mut Command) -> io::Result<Self> {
        cmd.process_group(0);
        let child = spawn_retrying(cmd)?;
        let pgid = child.id() as i32;
        Ok(Self {
            child,
            pgid,
            gone: false,
        })
    }
    fn signal(&mut self, signal: i32) -> io::Result<()> {
        if self.empty()? {
            return Ok(());
        }
        killpg(self.pgid, signal)
    }
}

#[cfg(test)]
#[path = "watch_process_tests.rs"]
mod process_tests;
impl Child for UnixChild {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.signal(libc::SIGTERM)
    }
    fn kill(&mut self) -> io::Result<()> {
        self.signal(libc::SIGKILL)
    }
    fn empty(&mut self) -> io::Result<bool> {
        // Continuous ownership survives leader reaping; never revive an absent group.
        self.gone = self.gone || group_empty(self.pgid)?;
        Ok(self.gone)
    }
    fn reap(&mut self) -> io::Result<()> {
        self.child.try_wait().map(|_| ())
    }
}
