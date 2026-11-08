//! Exact recorded Job identity bridge for startup recovery.
#[cfg(test)]
#[path = "reconcile_job_tests.rs"]
mod tests;
use crate::reconcile_store::Proc;
#[cfg(windows)]
use pg_worker_core::{
    cpu::CpuSample,
    watch::{
        windows::{AdoptedJob, NativeJob},
        Child,
    },
};
use std::io;
#[cfg(windows)]
use std::process::ExitStatus;
#[cfg(windows)]
pub(crate) struct Group(AdoptedJob<NativeJob>);
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
        let (name, start) = validate(run, &identity)?;
        Ok(Self(AdoptedJob::verify(
            NativeJob::open(name)?,
            identity.pid,
            start,
        )?))
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
impl OrphanJob for AdoptedJob<NativeJob> {
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
) -> Result<bool, crate::reconcile::ReconcileError> {
    let unknown = || crate::reconcile::ReconcileError::Unknown;
    let p = row.identity.as_ref().ok_or_else(unknown)?;
    let job = map_open(Group::open(
        &row.run_id,
        Proc {
            pid: p.pid,
            start_time: p.start_time,
            group: p.group.clone(),
            pgid: p.pgid,
        },
    ))?;
    match job {
        Some(mut job) => map_probe(job.orphan_empty()),
        None => Ok(true),
    }
}

// Only opening a scoped name may interpret NotFound as an absent group.
pub(crate) fn map_open<T>(
    result: io::Result<T>,
) -> Result<Option<T>, crate::reconcile::ReconcileError> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(crate::reconcile::ReconcileError::Unknown),
    }
}
pub(crate) fn map_probe(
    result: io::Result<bool>,
) -> Result<bool, crate::reconcile::ReconcileError> {
    result.map_err(|_| crate::reconcile::ReconcileError::Unknown)
}
