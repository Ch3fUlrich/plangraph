//! Identity-owned adoption bridge. Startup ownership wiring is deliberately separate.
use crate::reconcile_store::Proc;
use pg_worker_core::watch::Child;
use std::{io, process::ExitStatus};
/// Authority is supplied by the validated inventory/slot backend, never by a
/// signal caller. Implementations must revalidate membership before each signal.
pub(crate) trait Authority: Send {
    fn empty(&mut self) -> io::Result<bool>;
    fn signal(&mut self, force: bool) -> io::Result<()>;
}
pub(crate) struct Adopted<A> {
    authority: A,
    gone: bool,
}
impl<A: Authority> Adopted<A> {
    pub(crate) fn new(authority: A) -> Self {
        Self {
            authority,
            gone: false,
        }
    }
}
impl<A: Authority> Child for Adopted<A> {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn reap(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn empty(&mut self) -> io::Result<bool> {
        if !self.gone {
            self.gone = self.authority.empty()?;
        }
        Ok(self.gone)
    }
    fn terminate(&mut self) -> io::Result<()> {
        if !self.empty()? {
            self.authority.signal(false)?;
        }
        Ok(())
    }
    fn kill(&mut self) -> io::Result<()> {
        if !self.empty()? {
            self.authority.signal(true)?;
        }
        Ok(())
    }
}
/// Conservative no-scope Linux fallback: pin the verified leader with a pidfd.
/// A missing leader is ambiguous, not proof that its descendants are gone.
#[cfg(target_os = "linux")]
pub(crate) struct Leader {
    identity: Proc,
    fd: std::os::fd::OwnedFd,
}
#[cfg(target_os = "linux")]
impl Leader {
    pub(crate) fn open(identity: Proc) -> io::Result<Self> {
        use std::os::fd::FromRawFd;
        Self::verify(&identity)?;
        // SAFETY: pidfd_open takes a PID and zero flags, returning an owned fd.
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, identity.pid, 0) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let result = Self {
            identity,
            fd: unsafe { std::os::fd::OwnedFd::from_raw_fd(fd as i32) },
        };
        Self::verify(&result.identity)?;
        Ok(result)
    }
    fn verify(p: &Proc) -> io::Result<()> {
        let unknown = || io::Error::from(io::ErrorKind::PermissionDenied);
        if p.group.is_some() || p.pgid != Some(p.pid) || p.start_time.is_none() {
            return Err(unknown());
        }
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", p.pid))?;
        let fields: Vec<_> = stat
            .rsplit_once(')')
            .ok_or_else(unknown)?
            .1
            .split_whitespace()
            .collect();
        if fields.get(2).and_then(|s| s.parse::<u32>().ok()) != Some(p.pid)
            || fields.get(19).and_then(|s| s.parse::<u64>().ok()) != p.start_time
        {
            return Err(unknown());
        }
        Ok(())
    }
}
#[cfg(target_os = "linux")]
impl Authority for Leader {
    fn empty(&mut self) -> io::Result<bool> {
        let pgid = self
            .identity
            .pgid
            .ok_or_else(|| io::Error::other("Unknown"))?;
        for entry in std::fs::read_dir("/proc")? {
            let entry = entry?;
            if entry.file_name().to_string_lossy().parse::<u32>().is_err() {
                continue;
            }
            let stat = match std::fs::read_to_string(entry.path().join("stat")) {
                Ok(stat) => stat,
                Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                Err(e) => return Err(e),
            };
            let observed = stat
                .rsplit_once(')')
                .and_then(|(_, rest)| rest.split_whitespace().nth(2)?.parse::<u32>().ok())
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))?;
            if observed == pgid {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn signal(&mut self, force: bool) -> io::Result<()> {
        use std::os::fd::AsRawFd;
        Self::verify(&self.identity)?;
        // SAFETY: signal only the pinned, freshly identity-checked process.
        let result = unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.fd.as_raw_fd(),
                if force { libc::SIGKILL } else { libc::SIGTERM },
                std::ptr::null::<libc::siginfo_t>(),
                0,
            )
        };
        if result < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}
#[cfg(all(test, target_os = "linux"))]
#[path = "reconcile_backend_tests.rs"]
mod tests;
