//! Exact recorded Job identity bridge for startup recovery.
#[cfg(test)]
#[path = "reconcile_job_tests.rs"]
mod tests;
use crate::reconcile_store::Proc;
#[cfg(windows)]
use pg_worker_core::watch::windows as job_api;
#[cfg(windows)]
use pg_worker_core::{
    cpu::CpuSample,
    watch::{
        windows::{AdoptedJob, JobApi, NativeJob},
        Child,
    },
};
use std::io;
#[cfg(windows)]
use std::process::ExitStatus;
#[cfg(windows)]
pub(crate) struct Group(
    AdoptedJob<LocalJob>,
    Option<std::os::windows::io::OwnedHandle>,
);
#[cfg(windows)]
struct LocalJob {
    job: NativeJob,
    process: Option<std::os::windows::io::OwnedHandle>,
}
#[cfg(windows)]
fn process(pid: u32) -> io::Result<Option<(std::os::windows::io::OwnedHandle, u64, bool)>> {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::{Foundation::*, System::Threading::*};
    // SAFETY: query-only handle; successful opens transfer ownership.
    let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if raw.is_null() {
        let error = io::Error::last_os_error();
        return if error.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) {
            Ok(None)
        } else {
            Err(error)
        };
    }
    let handle = unsafe { OwnedHandle::from_raw_handle(raw) };
    let (mut created, mut exit, mut kernel, mut user) = (
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
    );
    // SAFETY: owned process handle and writable output buffers.
    if unsafe {
        GetProcessTimes(
            handle.as_raw_handle(),
            &mut created,
            &mut exit,
            &mut kernel,
            &mut user,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(Some((
        handle,
        u64::from(created.dwHighDateTime) << 32 | u64::from(created.dwLowDateTime),
        exit.dwHighDateTime == 0 && exit.dwLowDateTime == 0,
    )))
}
#[cfg(windows)]
pub(crate) fn absent(pid: u32) -> Result<bool, crate::reconcile::ReconcileError> {
    absent_result(process(pid).map(|p| p.is_some_and(|(_, _, live)| live)))
}
#[cfg(any(windows, test))]
fn absent_result(live: io::Result<bool>) -> Result<bool, crate::reconcile::ReconcileError> {
    match live {
        Ok(false) => Ok(true),
        _ => Err(crate::reconcile::ReconcileError::Unknown),
    }
}
#[cfg(windows)]
impl JobApi for LocalJob {
    fn member(&mut self, pid: u32, start: u64) -> io::Result<bool> {
        if let Some(handle) = &self.process {
            return process_live(handle);
        }
        let Some((handle, actual, live)) = process(pid)? else {
            return Ok(false);
        };
        let member = live && (start == 0 || start == actual) && self.job.member(pid, actual)?;
        self.process = Some(handle);
        Ok(member)
    }
    fn active(&mut self) -> io::Result<bool> {
        self.job.active()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.job.terminate()
    }
    fn times(&mut self) -> io::Result<(i64, i64)> {
        self.job.times()
    }
}
#[cfg(windows)]
fn process_live(handle: &std::os::windows::io::OwnedHandle) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::Threading::GetExitCodeProcess;
    let mut code = 0;
    // SAFETY: retained query handle and writable output buffer.
    if unsafe { GetExitCodeProcess(handle.as_raw_handle(), &mut code) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(code == 259)
}
pub(crate) fn scope(run: &str, recorded: &str) -> io::Result<()> {
    if !pg_worker_core::run::record::valid_run_id(run) || recorded != format!("Local\\pg-{run}") {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(())
}
fn validate<'a>(run: &str, identity: &'a Proc) -> io::Result<(&'a str, u64)> {
    if identity.pid == 0 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    let name = identity
        .group
        .as_deref()
        .ok_or(io::ErrorKind::PermissionDenied)?;
    scope(run, name)?;
    if identity.start_time == Some(0) {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    let start = identity.start_time.unwrap_or(0);
    Ok((name, start))
}
#[cfg(windows)]
impl Group {
    pub(crate) fn probe(&mut self) -> io::Result<bool> {
        if self.1.as_ref().map(process_live).transpose()? == Some(false) {
            return exited_probe(self.0.empty());
        }
        probe(&mut self.0)
    }
    pub(crate) fn orphan_empty(&mut self) -> io::Result<bool> {
        // Both paths check emptiness before any termination authority is used.
        self.probe()?;
        orphan_empty(&mut self.0)
    }
    pub(crate) fn open(run: &str, identity: Proc) -> io::Result<Self> {
        Self::acquire(run, identity)?.ok_or_else(|| io::ErrorKind::NotFound.into())
    }
    pub(crate) fn acquire(run: &str, identity: Proc) -> io::Result<Option<Self>> {
        let (name, start) = validate(run, &identity)?;
        let Some(job) = NativeJob::open(name)? else {
            return Ok(None);
        };
        let mut api = LocalJob { job, process: None };
        // Verify accepts a non-member as gone; acquisition must also prove empty.
        let member = api.member(identity.pid, start)?;
        acquire_decision(
            member,
            api.process.as_ref().map(process_live).transpose()?,
            || api.active().map(|active| !active),
        )?;
        let retained = api
            .process
            .as_ref()
            .map(|handle| handle.try_clone())
            .transpose()?;
        Ok(Some(Self(
            acquire_verified(api, identity.pid, start)?,
            retained,
        )))
    }
    pub(crate) fn cpu(&mut self) -> io::Result<CpuSample> {
        self.0.cpu()
    }
}
#[cfg(windows)]
impl Child for Group {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn reap(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn empty(&mut self) -> io::Result<bool> {
        self.0.empty()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.0.terminate()
    }
    fn kill(&mut self) -> io::Result<()> {
        self.0.terminate()
    }
}

// Probes and cleanup share the handle that passed verification.
#[cfg(any(windows, test))]
trait OrphanJob {
    fn gone(&mut self) -> io::Result<bool>;
    fn empty(&mut self) -> io::Result<bool>;
    fn terminate(&mut self) -> io::Result<()>;
}
#[cfg(windows)]
impl OrphanJob for AdoptedJob<LocalJob> {
    fn gone(&mut self) -> io::Result<bool> {
        self.is_gone()
    }
    fn empty(&mut self) -> io::Result<bool> {
        self.empty()
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.terminate()
    }
}
#[cfg(any(windows, test))]
fn probe(job: &mut impl OrphanJob) -> io::Result<bool> {
    if !job.gone()? {
        return Ok(false);
    }
    if !job.empty()? {
        return Err(io::ErrorKind::WouldBlock.into());
    }
    Ok(true)
}
#[cfg(any(windows, test))]
fn orphan_empty(job: &mut impl OrphanJob) -> io::Result<bool> {
    if !probe(job)? {
        return Ok(false);
    }
    job.terminate()?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while !job.empty()? {
        if std::time::Instant::now() >= deadline {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    Ok(true)
}

#[cfg(any(windows, test))]
fn exited_probe(empty: io::Result<bool>) -> io::Result<bool> {
    if empty? {
        Ok(true)
    } else {
        Err(io::ErrorKind::WouldBlock.into())
    }
}

#[cfg(any(windows, test))]
fn acquire_decision(
    member: bool,
    live: Option<bool>,
    empty: impl FnOnce() -> io::Result<bool>,
) -> io::Result<()> {
    if !member {
        if live == Some(true) {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        exited_probe(empty())?;
    }
    Ok(())
}

#[cfg(windows)]
fn acquire_verified<A: job_api::JobApi>(
    api: A,
    pid: u32,
    start: u64,
) -> io::Result<job_api::AdoptedJob<A>> {
    let mut job = job_api::AdoptedJob::verify(Some(api), pid, start)?;
    if job.is_gone()? {
        exited_probe(job.empty())?;
    }
    Ok(job)
}

#[cfg(windows)]
pub(crate) fn settle_probe(
    row: &crate::reconcile_store::Entry,
    authority: &mut Option<Option<Group>>,
) -> Result<bool, crate::reconcile::ReconcileError> {
    let unknown = || crate::reconcile::ReconcileError::Unknown;
    let p = row.identity.as_ref().ok_or_else(unknown)?;
    if authority.is_none() {
        *authority = Some(
            Group::acquire(
                &row.run_id,
                Proc {
                    pid: p.pid,
                    start_time: p.start_time,
                    group: p.group.clone(),
                    pgid: p.pgid,
                },
            )
            .map_err(|_| unknown())?,
        );
    }
    match authority.as_mut().unwrap() {
        Some(job) => map_probe(job.orphan_empty()),
        None => absent(p.pid),
    }
}

// Only opening a scoped name may interpret NotFound as an absent group.
#[cfg(test)]
pub(crate) fn map_open<T>(
    result: io::Result<T>,
) -> Result<Option<T>, crate::reconcile::ReconcileError> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(crate::reconcile::ReconcileError::Unknown),
    }
}
#[cfg(any(windows, test))]
pub(crate) fn map_probe(
    result: io::Result<bool>,
) -> Result<bool, crate::reconcile::ReconcileError> {
    result.map_err(|_| crate::reconcile::ReconcileError::Unknown)
}
