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
pub(crate) fn orphan_empty(
    row: &crate::reconcile_store::Entry,
) -> Result<bool, crate::reconcile::ReconcileError> {
    use crate::reconcile::ReconcileError;
    let identity = row.identity.as_ref().ok_or(ReconcileError::Unknown)?;
    Group::orphan_empty(&row.run_id, identity).map_err(|_| ReconcileError::Unknown)
}
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
    pub(crate) fn orphan_empty(run: &str, identity: &Proc) -> io::Result<bool> {
        let copy = Proc {
            pid: identity.pid,
            start_time: identity.start_time,
            group: identity.group.clone(),
            pgid: identity.pgid,
        };
        let mut job = Self::open(run, copy)?;
        job.terminate()?;
        job.empty()
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
    pub(crate) fn is_gone(&mut self) -> io::Result<bool> {
        self.0.is_gone()
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
