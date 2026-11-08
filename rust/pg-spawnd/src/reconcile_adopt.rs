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
    pid: u32,
}
impl Group {
    pub(crate) fn open(run: &str, identity: Proc) -> io::Result<Self> {
        if let Some(path) = &identity.group {
            let path = scope(run, path)?;
            Ok(Self {
                child: Box::new(Adopted::new(Scope(path.clone()))),
                scope: Some(path),
                pid: identity.pid,
            })
        } else {
            Ok(Self {
                pid: identity.pid,
                child: Box::new(Adopted::new(Leader::open(identity)?)),
                scope: None,
            })
        }
    }
    pub(crate) fn cpu(&mut self) -> io::Result<CpuSample> {
        match &self.scope {
            Some(path) => cpu(path),
            None => ticks_sample(proc_ticks(self.pid)?),
        }
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
    if pids.is_empty() {
        return Err(unknown());
    }
    let mut ticks = 0u64;
    for pid in pids {
        let value = match proc_ticks(pid) {
            Ok(value) => value,
            Err(e) if vanished(&e) => continue,
            Err(e) => return Err(e),
        };
        ticks = ticks.checked_add(value).ok_or_else(unknown)?;
    }
    ticks_sample(ticks)
}
fn proc_ticks(pid: u32) -> io::Result<u64> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let fields: Vec<_> = text
        .rsplit_once(')')
        .ok_or_else(unknown)?
        .1
        .split_whitespace()
        .collect();
    let mut ticks = 0u64;
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
    Ok(ticks)
}
fn ticks_sample(ticks: u64) -> io::Result<CpuSample> {
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if hz <= 0 {
        return Err(unknown());
    }
    Ok(CpuSample {
        total_ms: ticks.checked_mul(1000).ok_or_else(unknown)? / hz as u64,
    })
}
fn members(path: &Path, out: &mut Vec<u32>) -> io::Result<()> {
    match std::fs::read_to_string(path.join("cgroup.procs")) {
        Ok(text) => {
            for pid in text.split_whitespace() {
                out.push(pid.parse().map_err(|_| unknown())?);
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    for entry in std::fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            if let Err(e) = members(&entry.path(), out) {
                if !vanished(&e) {
                    return Err(e);
                }
            }
        }
    }
    Ok(())
}
fn vanished(e: &io::Error) -> bool {
    e.kind() == io::ErrorKind::NotFound || e.raw_os_error() == Some(libc::ESRCH)
}
#[cfg(test)]
thread_local! {
    pub(crate) static TEST_ROOT: std::cell::RefCell<Option<PathBuf>> = const { std::cell::RefCell::new(None) };
}
fn cgroup_root() -> PathBuf {
    #[cfg(test)]
    if let Some(root) = TEST_ROOT.with(|root| root.borrow().clone()) {
        return root;
    }
    PathBuf::from("/sys/fs/cgroup")
}
fn unknown() -> io::Error {
    io::Error::from(io::ErrorKind::PermissionDenied)
}
pub(crate) fn scope(run: &str, recorded: &str) -> io::Result<PathBuf> {
    if !pg_worker_core::run::record::valid_run_id(run) {
        return Err(unknown());
    }
    let path = Path::new(recorded);
    let prefix = cgroup_root().join(format!("user.slice/user-{}.slice", unsafe {
        libc::geteuid()
    }));
    if !path.starts_with(&prefix)
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
        pids.sort_unstable();
        pids.dedup();
        if pids.is_empty() && !self.empty()? {
            return Err(unknown());
        }
        for pid in pids {
            let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
            if fd < 0 {
                let error = io::Error::last_os_error();
                if vanished(&error) {
                    continue;
                }
                return Err(error);
            }
            let fd = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd as i32) };
            let membership = match std::fs::read_to_string(format!("/proc/{pid}/cgroup")) {
                Ok(text) => text,
                Err(e) if vanished(&e) => continue,
                Err(e) => return Err(e),
            };
            let relative = self.0.strip_prefix(cgroup_root()).map_err(|_| unknown())?;
            let member = membership
                .lines()
                .filter_map(|s| s.splitn(3, ':').nth(2))
                .any(|s| Path::new(s.trim_start_matches('/')).starts_with(relative));
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
                let error = io::Error::last_os_error();
                if !vanished(&error) {
                    return Err(error);
                }
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
