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
    let start = identity
        .start_time
        .filter(|v| *v != 0)
        .ok_or(io::ErrorKind::PermissionDenied)?;
    Ok((name, start))
}
#[cfg(windows)]
impl Group {
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
fn orphan_empty(job: &mut impl OrphanJob) -> io::Result<bool> {
    if !job.gone()? {
        return Ok(false);
    }
    if !job.empty()? {
        return Err(io::ErrorKind::WouldBlock.into());
    }
    job.terminate()?;
    if !job.empty()? {
        return Err(io::ErrorKind::WouldBlock.into());
    }
    Ok(true)
}
