//! Owned fresh process group. Never accepts a caller-provided pid.
use super::Child;
use crate::stall::{killpg, spawn_retrying};
use std::{
    io,
    os::unix::process::CommandExt,
    process::{Command, ExitStatus},
};

pub(crate) struct UnixChild {
    child: std::process::Child,
    pgid: i32,
    #[cfg(target_os = "linux")]
    start: Option<u64>,
}
impl UnixChild {
    pub(crate) fn spawn(cmd: &mut Command) -> io::Result<Self> {
        cmd.process_group(0);
        let child = spawn_retrying(cmd)?;
        let pgid = child.id() as i32;
        Ok(Self {
            child,
            pgid,
            #[cfg(target_os = "linux")]
            start: start_time(pgid),
        })
    }
    fn signal(&mut self, signal: i32) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        if start_time(self.pgid).is_some_and(|now| Some(now) != self.start) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "process identity changed",
            ));
        }
        killpg(self.pgid, signal)
    }
}
#[cfg(target_os = "linux")]
fn start_time(pid: i32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(')')?
        .1
        .split_whitespace()
        .nth(19)?
        .parse()
        .ok()
}
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
        // ESRCH alone proves emptiness; EPERM must not become successful proof.
        let result = unsafe { libc::killpg(self.pgid, 0) };
        if result == 0 {
            return Ok(false);
        }
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(true)
        } else {
            Err(error)
        }
    }
    fn reap(&mut self) -> io::Result<()> {
        self.child.try_wait().map(|_| ())
    }
}
