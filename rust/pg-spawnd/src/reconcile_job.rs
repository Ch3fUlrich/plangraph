//! Exact recorded Job identity bridge for startup recovery.
#[cfg(test)]
#[path = "reconcile_job_tests.rs"]
mod tests;
use crate::reconcile_store::Proc;
use pg_worker_core::{
    cpu::CpuSample,
    watch::{
        windows::{AdoptedJob, NativeJob},
        Child,
    },
};
use std::{io, process::ExitStatus};
pub(crate) struct Group(AdoptedJob<NativeJob>);
pub(crate) fn scope(run: &str, recorded: &str) -> io::Result<()> {
    if !pg_worker_core::run::record::valid_run_id(run) || recorded != format!("Local\\pg-{run}") {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(())
}
impl Group {
    pub(crate) fn open(run: &str, identity: Proc) -> io::Result<Self> {
        let name = identity.group.ok_or(io::ErrorKind::PermissionDenied)?;
        scope(run, &name)?;
        let start = identity
            .start_time
            .filter(|v| *v != 0)
            .ok_or(io::ErrorKind::PermissionDenied)?;
        Ok(Self(AdoptedJob::verify(
            NativeJob::open(&name)?,
            identity.pid,
            start,
        )?))
    }
    pub(crate) fn cpu(&mut self) -> io::Result<CpuSample> {
        self.0.cpu()
    }
}
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
