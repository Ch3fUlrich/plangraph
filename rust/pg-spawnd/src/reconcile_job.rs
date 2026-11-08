//! Exact recorded Job identity bridge for startup recovery.
#[cfg(test)]
#[path = "reconcile_job_tests.rs"]
mod tests;
use crate::reconcile_store::Proc;
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
pub(crate) struct Group(AdoptedJob<LocalJob>);
#[cfg(windows)]
struct LocalJob {
    job: NativeJob,
    _process: Option<std::os::windows::io::OwnedHandle>,
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
    match process(pid) {
        Ok(None) | Ok(Some((_, _, false))) => Ok(true),
        _ => Err(crate::reconcile::ReconcileError::Unknown),
    }
}
#[cfg(windows)]
impl JobApi for LocalJob {
    fn member(&mut self, pid: u32, start: u64) -> io::Result<bool> {
        let Some((handle, actual, live)) = process(pid)? else {
            return Ok(false);
        };
        let member = live && (start == 0 || start == actual) && self.job.member(pid, actual)?;
        self._process = Some(handle);
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
        probe(&mut self.0)
    }
    pub(crate) fn orphan_empty(&mut self) -> io::Result<bool> {
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
        Ok(Some(Self(AdoptedJob::verify(
            Some(LocalJob {
                job,
                _process: None,
            }),
            identity.pid,
            start,
        )?)))
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
