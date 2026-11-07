//! Recorded scope authority, independent of the original leader's lifetime.
#[cfg(test)]
#[path = "reconcile_adopt_tests.rs"]
mod tests;
use super::{Adopted, Authority, Leader};
use crate::reconcile_store::Proc;
use pg_worker_core::{cpu::CpuSample, watch::Child};
use std::{
    io,
    path::{Path, PathBuf},
};

pub(crate) struct Group {
    child: Box<dyn Child + Send>,
    scope: Option<PathBuf>,
}
impl Group {
    pub(crate) fn open(run: &str, identity: Proc) -> io::Result<Self> {
        if let Some(path) = &identity.group {
            let path = scope(run, path)?;
            Ok(Self {
                child: Box::new(Adopted::new(Scope(path.clone()))),
                scope: Some(path),
            })
        } else {
            Ok(Self {
                child: Box::new(Adopted::new(Leader::open(identity)?)),
                scope: None,
            })
        }
    }
    pub(crate) fn cpu(&mut self) -> io::Result<CpuSample> {
        let path = self.scope.as_ref().ok_or_else(unknown)?;
        cpu(path)
    }
}
fn cpu(path: &Path) -> io::Result<CpuSample> {
    let usage = std::fs::read_to_string(path.join("cpu.stat"))
        .ok()
        .and_then(|text| {
            text.lines()
                .find_map(|line| line.strip_prefix("usage_usec "))
                .and_then(|v| v.parse::<u64>().ok())
        });
    if let Some(total) = usage {
        return Ok(CpuSample {
            total_ms: total / 1000,
        });
    }
    let mut pids = Vec::new();
    members(path, &mut pids)?;
    pids.sort_unstable();
    pids.dedup();
    let mut ticks = 0u64;
    for pid in pids {
        let text = std::fs::read_to_string(format!("/proc/{pid}/stat"))?;
        let fields: Vec<_> = text
            .rsplit_once(')')
            .ok_or_else(unknown)?
            .1
            .split_whitespace()
            .collect();
        for index in [11, 12] {
            ticks = ticks
                .checked_add(
                    fields
                        .get(index)
                        .ok_or_else(unknown)?
                        .parse::<u64>()
                        .map_err(|_| unknown())?,
                )
                .ok_or_else(unknown)?;
        }
    }
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if hz <= 0 {
        return Err(unknown());
    }
    Ok(CpuSample {
        total_ms: ticks.checked_mul(1000).ok_or_else(unknown)? / hz as u64,
    })
}
fn members(path: &Path, out: &mut Vec<u32>) -> io::Result<()> {
    for pid in std::fs::read_to_string(path.join("cgroup.procs"))?.split_whitespace() {
        out.push(pid.parse().map_err(|_| unknown())?);
    }
    for entry in std::fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            members(&entry.path(), out)?;
        }
    }
    Ok(())
}
fn unknown() -> io::Error {
    io::Error::from(io::ErrorKind::PermissionDenied)
}
pub(crate) fn scope(run: &str, recorded: &str) -> io::Result<PathBuf> {
    if !pg_worker_core::run::record::valid_run_id(run) {
        return Err(unknown());
    }
    let path = Path::new(recorded);
    let prefix = format!("/sys/fs/cgroup/user.slice/user-{}.slice/", unsafe {
        libc::geteuid()
    });
    if !recorded.starts_with(&prefix)
        || path.file_name().and_then(|s| s.to_str()) != Some(&format!("pg-{run}.scope"))
    {
        return Err(unknown());
    }
    for component in path.components() {
        if !matches!(
            component,
            std::path::Component::RootDir | std::path::Component::Normal(_)
        ) {
            return Err(unknown());
        }
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        match std::fs::symlink_metadata(&current) {
            Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => break,
            _ => return Err(unknown()),
        }
    }
    Ok(path.into())
}
struct Scope(PathBuf);
impl Authority for Scope {
    fn empty(&mut self) -> io::Result<bool> {
        match std::fs::read_to_string(self.0.join("cgroup.events")) {
            Ok(text) => match text.lines().find_map(|s| s.strip_prefix("populated ")) {
                Some("0") => Ok(true),
                Some("1") => Ok(false),
                _ => Err(unknown()),
            },
            Err(e) if e.kind() == io::ErrorKind::NotFound && !self.0.try_exists()? => Ok(true),
            Err(e) => Err(e),
        }
    }
    fn signal(&mut self, force: bool) -> io::Result<()> {
        use std::os::fd::{AsRawFd, FromRawFd};
        let mut pids = Vec::new();
        members(&self.0, &mut pids)?;
        for pid in pids {
            let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            let fd = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd as i32) };
            let membership = std::fs::read_to_string(format!("/proc/{pid}/cgroup"))?;
            let relative = self
                .0
                .strip_prefix("/sys/fs/cgroup")
                .map_err(|_| unknown())?;
            let member = membership
                .lines()
                .filter_map(|s| s.splitn(3, ':').nth(2))
                .any(|s| Path::new(s).starts_with(relative));
            if !member {
                return Err(unknown());
            }
            if unsafe {
                libc::syscall(
                    libc::SYS_pidfd_send_signal,
                    fd.as_raw_fd(),
                    if force { libc::SIGKILL } else { libc::SIGTERM },
                    std::ptr::null::<libc::siginfo_t>(),
                    0,
                )
            } < 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }
}
impl Child for Group {
    fn try_wait(&mut self) -> io::Result<Option<std::process::ExitStatus>> {
        Ok(None)
    }
    fn reap(&mut self) -> io::Result<Option<std::process::ExitStatus>> {
        Ok(None)
    }
    fn empty(&mut self) -> io::Result<bool> {
        self.child.empty()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.child.terminate()
    }
    fn kill(&mut self) -> io::Result<()> {
        self.child.kill()
    }
}
